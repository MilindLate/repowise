#!/usr/bin/env python3
"""Call-edge oracle: grade a repowise call graph on sampled call sites.

Independent of repowise (it never imports ``repowise.*``): it reads the
index's ``graph_edges`` / ``graph_nodes`` rows from ``wiki.db`` and asks a
type-aware resolver where each sampled call really goes.

- **Python**: jedi ``goto`` (following imports; a called variable is
  inferred), run in a separate interpreter (see Setup) so jedi never becomes
  a repowise dependency. Project roots are the import oracle's
  (``imports.PythonResolver``).
- **TS/JS**: ``ts_calls.mjs`` — LanguageService ``getDefinitionAtPosition``
  per nearest tsconfig, with ``ts_resolve.mjs`` resolving modules.

Two deterministic (seeded) samples per repo:

- **Precision**: ``sample`` call claims at confidence >= ``HIGH`` and
  ``sample // 2`` below it. A claim is one ``calls`` edge at one of its
  ``call_lines``. The oracle resolves the call sites on that line (those
  whose callee name is the claimed callee's, else all of them): TP when one
  resolves to the claimed callee, FP when all resolve elsewhere (another
  in-repo symbol or outside the repo), else ``unknown`` — never FP.
- **Caller recall**: ``sample`` call sites whose callee name is declared
  somewhere in the repo (the frame; calls to names the repo never declares
  cannot reach an in-repo definition). A site the oracle resolves to an
  in-repo symbol repowise indexed is a hit when repowise has an edge from
  that file and line to it; ``unknown`` and ``external`` sites, and
  definitions repowise has no node for (``not_indexed``), are not misses.

A definition matches a repowise node when the file and name agree and the
definition line falls inside the node's span. Only callees are graded: which
enclosing symbol repowise names as the caller is not.

Setup: jedi runs under ``REPOWISE_ORACLE_PY`` when set (a python, or a
command line, with ``requirements-oracles.txt`` installed); else under
``uv run --with-requirements requirements-oracles.txt`` when uv is
installed; else under this python. Without jedi the Python half is reported
unavailable, as the TS half is without node + ``typescript`` (found as in
imports.py).

Usage::

    python scripts/kg_validate/oracles/calls.py REPO [--db REPO/.repowise/wiki.db] \
        [--sample 200] [--seed 0] [--details out.json]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import random
import shlex
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from imports import PythonResolver, lang_of, tracked_files  # noqa: E402

LANGS = ("python", "typescript")
HIGH = 0.9  # the gated confidence tier (thresholds.toml [family.calls])
SAMPLE = 200
UNKNOWN = {"s": "unknown", "d": []}
REQUIREMENTS = HERE.parent / "requirements-oracles.txt"


@dataclass(frozen=True, order=True)
class Site:
    file: str
    line: int  # line of the callee name
    start_line: int  # line the call expression starts on (repowise's call line)
    col: int  # column of the callee name, in the resolver's units
    name: str


# ------------------------------------------------------------------ Python


def _py_sites(repo: Path, file: str, names: set[str]) -> list[Site]:
    src = (repo / file).read_bytes()
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return []
    lines = src.split(b"\n")

    def site(start: ast.AST, line: int, byte_col: int, name: str) -> Site:
        # ast columns are UTF-8 byte offsets; jedi counts characters.
        col = len(lines[line - 1][:byte_col].decode("utf-8", "replace"))
        return Site(file, line, start.lineno, col, name)

    def callee(start: ast.AST, func: ast.AST) -> Site | None:
        if isinstance(func, ast.Name):
            return site(start, func.lineno, func.col_offset, func.id)
        if isinstance(func, ast.Attribute):
            line, end = func.end_lineno, func.end_col_offset
            return site(start, line, end - len(func.attr.encode()), func.attr)
        return None

    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            out.append(callee(node, node.func))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            # A bare decorator is applied by a call too.
            out += [callee(d, d) for d in node.decorator_list if not isinstance(d, ast.Call)]
    return [s for s in out if s]


def python_sites(repo: Path, files: list[str]) -> tuple[dict[str, list[Site]], set[str]]:
    names: set[str] = set()
    return {f: _py_sites(repo, f, names) for f in files}, names


def _jedi_worker() -> None:
    """``calls.py jedi``: resolve stdin queries with jedi (the REPOWISE_ORACLE_PY side)."""
    import jedi

    req = json.load(sys.stdin)
    repo = Path(req["repo"]).resolve()
    tracked = set(req["files"])
    project = jedi.Project(repo, added_sys_path=[str(repo / r) for r in req["roots"] if r != "."])
    env = jedi.create_environment(sys.executable, safe=False)
    scripts: dict[str, jedi.Script] = {}

    def place(name) -> tuple[str, list] | None:
        if name.module_path is None:
            return ("external", []) if name.in_builtin_module() else None
        try:
            rel = Path(name.module_path).resolve().relative_to(repo).as_posix()
        except ValueError:
            return ("external", [])
        if rel not in tracked:
            return ("external", [])
        return ("defs", [[rel, name.line, name.name]])

    def resolve(file: str, line: int, col: int) -> dict:
        if file not in scripts:
            scripts[file] = jedi.Script(path=str(repo / file), project=project, environment=env)
        script = scripts[file]
        found = script.goto(line, col, follow_imports=True)
        defs, unknown, external = [], not found, False
        for name in found:
            if name.type == "statement":  # a called variable: what it holds
                held = [n for n in script.infer(line, col) if n.type in ("function", "class")]
                unknown |= not held
            elif name.type in ("function", "class"):
                held = [name]
            else:  # a parameter, an unfollowed import, an instance: no static target
                held, unknown = [], True
            for n in held:
                where = place(n)
                if where is None:
                    unknown = True
                elif where[0] == "external":
                    external = True
                else:
                    defs += where[1]
        if defs:
            return {"s": "defs", "d": defs}
        return {"s": "external" if external and not unknown else "unknown", "d": []}

    out = []
    for file, line, col in req["queries"]:
        try:
            out.append(resolve(file, line, col))
        except Exception:  # jedi gives up on some code; that site is unknown
            out.append(UNKNOWN)
    json.dump(out, sys.stdout)


def _oracle_py() -> list[str]:
    """The interpreter command jedi runs under (see Setup in the module docstring)."""
    if os.environ.get("REPOWISE_ORACLE_PY"):
        return shlex.split(os.environ["REPOWISE_ORACLE_PY"])
    uv = shutil.which("uv")
    if uv:
        # A clean interpreter of this version, not this venv: packages visible
        # to jedi would turn some unknown third-party calls into external ones.
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        run = ["run", "--quiet", "--no-project", "--isolated", "--python", version]
        return [uv, *run, "--with-requirements", str(REQUIREMENTS), "python"]
    return [sys.executable]


def python_defs(
    repo: Path, all_files: list[str], queries: list[tuple[str, int, int]]
) -> list[dict]:
    if not queries:
        return []
    req = {
        "repo": str(repo),
        "files": all_files,
        "roots": PythonResolver(repo, all_files).shared_roots,
        "queries": queries,
    }
    res = subprocess.run(
        [*_oracle_py(), str(Path(__file__).resolve()), "jedi"],
        input=json.dumps(req),
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"jedi worker failed: {res.stderr.strip()[-2000:]}")
    return json.loads(res.stdout)


# ---------------------------------------------------------------------- TS


def _ts_call(repo: Path, req: dict, ts_path: str | None) -> dict:
    cmd = [shutil.which("node") or "node", str(HERE / "ts_calls.mjs"), "--root", str(repo)]
    if ts_path:
        cmd += ["--ts-path", ts_path]
    res = subprocess.run(cmd, input=json.dumps(req), capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ts_calls.mjs failed: {res.stderr.strip()[-2000:]}")
    return json.loads(res.stdout)


def ts_sites(repo, all_files, files, ts_path) -> tuple[dict[str, list[Site]], set[str]]:
    data = _ts_call(repo, {"files": all_files, "sites": files}, ts_path)
    sites = {f: [Site(f, *s) for s in ss] for f, ss in data["sites"].items()}
    return sites, set(data["names"])


def ts_defs(repo, all_files, queries, ts_path) -> list[dict]:
    if not queries:
        return []
    return _ts_call(repo, {"files": all_files, "queries": queries}, ts_path)["defs"]


# ------------------------------------------------------------ availability


def unavailable(ts_path: str | None = None) -> dict[str, str]:
    """``{language: reason}`` for each language whose resolver cannot run here."""
    out = {}
    py = subprocess.run([*_oracle_py(), "-c", "import jedi"], capture_output=True)
    if py.returncode != 0:
        out["python"] = (
            "jedi not importable (install uv, or set REPOWISE_ORACLE_PY to a python "
            "with scripts/kg_validate/requirements-oracles.txt installed)"
        )
    if shutil.which("node") is None:
        out["typescript"] = "node not installed"
    else:
        try:
            _ts_call(HERE, {"files": []}, ts_path)
        except RuntimeError:
            out["typescript"] = "typescript package not found (set REPOWISE_ORACLE_TS)"
    return out


# ------------------------------------------------------------- repowise side


@dataclass(frozen=True)
class Node:
    id: str
    file: str
    name: str
    start: int
    end: int


def load_graph(db_path: Path) -> tuple[set[str], dict[str, Node], list[tuple]]:
    """(indexed files, symbol nodes by id, call claims ``(file, line, callee_id, conf)``)."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        files = {
            r[0] for r in db.execute("SELECT node_id FROM graph_nodes WHERE node_type = 'file'")
        }
        nodes = {
            r[0]: Node(*r)
            for r in db.execute(
                "SELECT node_id, file_path, name, start_line, end_line FROM graph_nodes "
                "WHERE node_type != 'file' AND file_path IS NOT NULL AND name IS NOT NULL"
            )
        }
        rows = db.execute(
            "SELECT e.source_node_id, e.target_node_id, e.confidence, e.call_lines_json "
            "FROM graph_edges e WHERE e.edge_type = 'calls'"
        ).fetchall()
    claims = []
    for src, dst, conf, lines in rows:
        if src in nodes and dst in nodes:
            claims += [(nodes[src].file, int(ln), dst, conf) for ln in json.loads(lines or "[]")]
    return files, nodes, sorted(set(claims))


# ----------------------------------------------------------------- measure


def _sample(rng: random.Random, population: list, k: int) -> list:
    return rng.sample(population, min(k, len(population)))


def measure(
    repo: Path,
    db_path: Path,
    *,
    sample: int = SAMPLE,
    seed: int = 0,
    ts_path: str | None = None,
    details: dict | None = None,
) -> dict:
    """Sampled precision and caller-recall counts for one indexed repo.

    ``claims``: ``{confidence: {tp, fp, unknown}}`` over the precision sample
    (``no_site`` counts the unknown claims whose line holds no call at all).
    ``sites``: ``{hit: {confidence: n}, fn, unknown, external, not_indexed}``
    over the recall sample, a hit keyed by its strongest edge's confidence.
    ``details``, when given, is filled with the judged items.
    """
    repo = repo.resolve()
    ts_path = ts_path or os.environ.get("REPOWISE_ORACLE_TS")
    missing = unavailable(ts_path)
    all_files = tracked_files(repo)
    indexed, nodes, claims = load_graph(db_path)
    by_lang: dict[str, list[str]] = defaultdict(list)
    for f in all_files:
        lang = lang_of(f)
        if lang in LANGS and lang not in missing and f in indexed and "node_modules/" not in f:
            by_lang[lang].append(f)

    sites: dict[str, list[Site]] = {}
    names: dict[str, set[str]] = {}
    if by_lang["python"]:
        s, names["python"] = python_sites(repo, by_lang["python"])
        sites.update(s)
    if by_lang["typescript"]:
        s, names["typescript"] = ts_sites(repo, all_files, by_lang["typescript"], ts_path)
        sites.update(s)
    at_line: dict[tuple[str, int], list[Site]] = defaultdict(list)
    for ss in sites.values():
        for s in ss:
            at_line[(s.file, s.start_line)].append(s)
            if s.line != s.start_line:
                at_line[(s.file, s.line)].append(s)

    rng = random.Random(seed)
    claims = [c for c in claims if c[0] in sites]
    high = [c for c in claims if c[3] >= HIGH]
    low = [c for c in claims if c[3] < HIGH]
    picked_claims = _sample(rng, high, sample) + _sample(rng, low, sample // 2)
    frame = sorted(s for ss in sites.values() for s in ss if s.name in names[lang_of(s.file)])
    picked_sites = _sample(rng, frame, sample)

    def candidates(file: str, line: int, callee: str) -> list[Site]:
        on_line = at_line.get((file, line), [])
        return [s for s in on_line if s.name == nodes[callee].name] or on_line

    wanted = {s for c in picked_claims for s in candidates(c[0], c[1], c[2])}
    wanted |= set(picked_sites)
    resolved: dict[Site, dict] = {}
    for lang in LANGS:
        qs = sorted(s for s in wanted if lang_of(s.file) == lang)
        queries = [(s.file, s.line, s.col) for s in qs]
        if lang == "python":
            out = python_defs(repo, all_files, queries)
        else:
            out = ts_defs(repo, all_files, queries, ts_path)
        resolved.update(zip(qs, out, strict=True))

    by_file_name: dict[tuple[str, str], list[Node]] = defaultdict(list)
    for n in nodes.values():
        by_file_name[(n.file, n.name)].append(n)

    def matches(site: Site) -> set[str]:
        return {
            n.id
            for f, line, name in resolved[site]["d"]
            for n in by_file_name.get((f, name), ())
            if n.start <= line <= n.end
        }

    claim_counts: dict[str, dict[str, int]] = {}
    no_site = 0
    judged_claims = []
    for file, line, callee, conf in picked_claims:
        cands = candidates(file, line, callee)
        if any(callee in matches(s) for s in cands):
            verdict = "tp"
        elif cands and all(resolved[s]["s"] != "unknown" for s in cands):
            verdict = "fp"
        else:
            verdict = "unknown"
            no_site += not cands
        bucket = claim_counts.setdefault(str(conf), {"tp": 0, "fp": 0, "unknown": 0})
        bucket[verdict] += 1
        judged_claims.append(
            {
                "file": file,
                "line": line,
                "callee": callee,
                "confidence": conf,
                "verdict": verdict,
                "oracle": [resolved[s] for s in cands],
            }
        )

    edges_at: dict[tuple[str, int], dict[str, float]] = defaultdict(dict)
    for file, line, callee, conf in claims:
        best = edges_at[(file, line)]
        best[callee] = max(conf, best.get(callee, 0.0))
    site_counts = {"hit": {}, "fn": 0, "unknown": 0, "external": 0, "not_indexed": 0}
    judged_sites = []
    for s in picked_sites:
        status = resolved[s]["s"]
        confs = []
        if status == "defs":
            targets = matches(s)
            got = {**edges_at.get((s.file, s.start_line), {}), **edges_at.get((s.file, s.line), {})}
            confs = [c for t, c in got.items() if t in targets]
            status = "hit" if confs else ("fn" if targets else "not_indexed")
        if status == "hit":
            key = str(max(confs))
            site_counts["hit"][key] = site_counts["hit"].get(key, 0) + 1
        else:
            site_counts[status] += 1
        judged_sites.append({**vars(s), "verdict": status, "oracle": resolved[s]})

    if details is not None:
        details.update(claims=judged_claims, sites=judged_sites)
    out = {
        "claims": claim_counts,
        "no_site": no_site,
        "sites": site_counts,
        "population": {"claims": len(claims), "claims_high": len(high), "frame": len(frame)},
        "sample": sample,
        "seed": seed,
    }
    if missing:
        out["unavailable"] = missing
    return out


def main(argv=None) -> int:
    if (argv or sys.argv[1:])[:1] == ["jedi"]:
        _jedi_worker()
        return 0
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("repo", type=Path)
    ap.add_argument("--db", type=Path, help="default REPO/.repowise/wiki.db")
    ap.add_argument("--sample", type=int, default=SAMPLE)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--ts-path", help="directory of the `typescript` npm package")
    ap.add_argument("--details", type=Path, help="write every judged claim and site as JSON")
    args = ap.parse_args(argv)
    details: dict = {}
    result = measure(
        args.repo,
        args.db or args.repo / ".repowise" / "wiki.db",
        sample=args.sample,
        seed=args.seed,
        ts_path=args.ts_path,
        details=details,
    )
    if args.details:
        args.details.write_text(json.dumps(details, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
