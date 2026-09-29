#!/usr/bin/env python3
"""Import-edge oracle: ground-truth file→file import edges for Python, TS/JS and Go.

The oracle is independent of repowise (it never imports ``repowise.*``) so the
graph can be graded against it:

- **Python**: AST-walks every import and resolves it the way
  ``importlib.util.find_spec`` would against a ``sys.path`` of (1) the
  importer's own root — its directory when it is not in a package (script
  semantics), else the directory above its outermost ``__init__.py`` — then
  (2) the repo root and every project root declared by a pyproject.toml /
  setup.cfg / setup.py (the manifest dir, ``src/`` layouts, setuptools
  ``package-dir`` / ``packages.find.where``, hatch/poetry/pdm/maturin package
  dirs). Regular packages own their name (no fall-through to later roots),
  namespace packages merge across roots. A stdlib name only resolves locally
  through the script directory, since the stdlib precedes site-packages.
  ``from pkg import name`` yields ``pkg/name.py`` when that submodule exists,
  else ``pkg/__init__.py``.
- **TS/JS**: ``ts_resolve.mjs`` — ``ts.resolveModuleName`` with each file's
  nearest tsconfig, package.json ``imports``, and workspace packages from
  pnpm-workspace.yaml / package.json ``workspaces`` mapped to their sources.
- **Go**: ``go list -e -json ./...`` per module (every go.mod); an import of an
  in-repo package yields an edge to each of that package's non-test files
  (build-tag-excluded files included: the graph is not per-platform).

Setup: the TS pass needs node and the ``typescript`` npm package. The script
finds it from its own directory upward (``npm ci`` at the repo root provides
it), or pass ``--ts-path /path/to/node_modules/typescript`` (or set
``REPOWISE_ORACLE_TS``). A one-off install works too::

    npm install --prefix /tmp/oracle-ts typescript
    --ts-path /tmp/oracle-ts/node_modules/typescript

The Go pass needs the ``go`` toolchain (and network or a warm module cache,
since ``go list`` loads the module graph); without ``go`` it is skipped with a
warning.

Usage::

    python scripts/kg_validate/oracles/imports.py edges REPO [-o edges.json]
    python scripts/kg_validate/oracles/imports.py compare REPO \
        [--graph REPO/.repowise/knowledge-graph.json] [--details out.json] [--json]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections import defaultdict
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
TS_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
LANG_BY_EXT = {".py": "python", ".go": "go", **dict.fromkeys(TS_EXTS, "typescript")}
# Edge types a repowise graph uses for "this file imports that file".
# knowledge-graph.json retypes a test file's imports as `tested_by` (still
# test → imported file); the full graph export has the dynamic/type kinds.
DEFAULT_EDGE_TYPES = (
    "imports",
    "tested_by",
    "dynamic_imports",
    "type_use",
    "dynamic_uses",
    "framework",
)
# `unresolved` markers for a source whose imports the oracle could not read.
SYNTAX_ERROR = "<syntax error>"
NOT_LISTED = "<not in go list>"
OUT_OF_SCOPE = {SYNTAX_ERROR, NOT_LISTED}
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".repowise"}


def lang_of(path: str) -> str | None:
    return LANG_BY_EXT.get(PurePosixPath(path).suffix)


def tracked_files(repo: Path) -> list[str]:
    """Repo-relative posix paths: ``git ls-files`` when a git repo, else a walk."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, check=True
        ).stdout.decode()
        files = [f for f in out.split("\0") if f]
        if files:
            return [f for f in files if (repo / f).is_file()]
    except (OSError, subprocess.CalledProcessError):
        pass
    found = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        rel = Path(dirpath).relative_to(repo).as_posix()
        found += [f if rel == "." else f"{rel}/{f}" for f in filenames]
    return sorted(found)


# --------------------------------------------------------------------- Python


def _manifest_roots(repo: Path, manifest: str) -> list[str]:
    """Import roots one pyproject.toml / setup.cfg / setup.py declares."""
    base = PurePosixPath(manifest).parent
    rels: list[str] = [".", "src"]
    text = (repo / manifest).read_text(encoding="utf-8", errors="ignore")
    name = PurePosixPath(manifest).name
    if name == "pyproject.toml":
        try:
            tool = tomllib.loads(text).get("tool", {})
        except tomllib.TOMLDecodeError:
            tool = {}
        st = tool.get("setuptools", {})
        pkg_dir = st.get("package-dir", {})
        if isinstance(pkg_dir, dict) and isinstance(pkg_dir.get(""), str):
            rels.append(pkg_dir[""])
        find = (st.get("packages") or {}) if isinstance(st.get("packages"), dict) else {}
        rels += (find.get("find") or {}).get("where", [])
        wheel = tool.get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
        rels += [str(PurePosixPath(p).parent) for p in wheel.get("packages", [])]
        rels += [p["from"] for p in tool.get("poetry", {}).get("packages", []) if "from" in p]
        pdm_dir = tool.get("pdm", {}).get("build", {}).get("package-dir")
        maturin_dir = tool.get("maturin", {}).get("python-source")
        rels += [d for d in (pdm_dir, maturin_dir) if isinstance(d, str)]
    else:
        # setup.cfg `package_dir = =src`, `where = src`; setup.py
        # `package_dir={"": "src"}`, `find_packages(where="src")`.
        rels += re.findall(r"package_dir\s*=\s*\n?\s*=\s*([\w./-]+)", text)
        rels += re.findall(r"""package_dir\s*=\s*\{\s*['"]{2}\s*:\s*['"]([\w./-]+)['"]""", text)
        rels += re.findall(r"""where\s*=\s*\[?\s*['"]?([\w./-]+)""", text)
    roots = []
    for r in rels:
        p = (base / r).as_posix()
        p = os.path.normpath(p).replace(os.sep, "/")
        if p not in roots and not p.startswith(".."):
            roots.append(p)
    return roots


class PythonResolver:
    def __init__(self, repo: Path, files: list[str]):
        self.repo = repo
        self.files = set(files)
        self.dirs = {str(PurePosixPath(f).parent) for f in files}
        for d in list(self.dirs):
            while d not in (".", ""):
                d = str(PurePosixPath(d).parent)
                self.dirs.add(d)
        roots = ["."]
        for f in files:
            if PurePosixPath(f).name in ("pyproject.toml", "setup.cfg", "setup.py"):
                roots += [r for r in _manifest_roots(repo, f) if r not in roots]
        self.shared_roots = [r for r in roots if r == "." or r in self.dirs]
        self.stdlib = set(sys.stdlib_module_names) | set(sys.builtin_module_names)

    def _join(self, root: str, *parts: str) -> str:
        return str(PurePosixPath(root, *parts)) if root != "." else "/".join(parts)

    def own_root(self, file: str) -> tuple[str, bool]:
        """The importer's sys.path entry and whether it is inside a package."""
        d = str(PurePosixPath(file).parent)
        in_pkg = False
        while self._join(d, "__init__.py") in self.files:
            in_pkg = True
            if d in (".", ""):
                break
            d = str(PurePosixPath(d).parent)
        return (d or "."), in_pkg

    def _in_root(self, root: str, parts: list[str]) -> tuple[str | None, bool]:
        """(file, owned): the module file under one root, and whether the
        top-level name is a regular package/module there (no fall-through)."""
        top = parts[0]
        owned = (
            self._join(root, top + ".py") in self.files
            or self._join(root, top, "__init__.py") in self.files
        )
        if not owned and self._join(root, top) not in self.dirs:
            return None, False
        for cand in (
            self._join(root, *parts[:-1], parts[-1] + ".py"),
            self._join(root, *parts, "__init__.py"),
        ):
            if cand in self.files:
                return cand, owned
        return None, owned

    def resolve_absolute(self, dotted: str, roots: list[str]) -> str | None:
        parts = dotted.split(".")
        for root in roots:
            hit, owned = self._in_root(root, parts)
            if hit or owned:
                return hit
        return None

    def roots_for(self, file: str, top: str) -> list[str]:
        own, in_pkg = self.own_root(file)
        if top in self.stdlib:
            # stdlib precedes every installed/editable root; only a script's
            # own directory (sys.path[0]) can shadow it.
            return [] if in_pkg else [own]
        return [own] + [r for r in self.shared_roots if r != own]

    def resolve_relative(self, file: str, level: int, module: str | None) -> str | None:
        base = PurePosixPath(file).parent
        for _ in range(level - 1):
            base = base.parent
        parts = (module or "").split(".") if module else []
        b = str(base)
        if parts:
            for cand in (
                self._join(b, *parts[:-1], parts[-1] + ".py"),
                self._join(b, *parts, "__init__.py"),
            ):
                if cand in self.files:
                    return cand
            return None
        cand = self._join(b, "__init__.py")
        return cand if cand in self.files else None

    def _lookup(self, file: str, level: int, dotted: str, roots: list[str]) -> str | None:
        if level:
            return self.resolve_relative(file, level, dotted or None)
        return self.resolve_absolute(dotted, roots)

    def imports_of(self, file: str) -> tuple[set[str], list[str]]:
        try:
            tree = ast.parse((self.repo / file).read_bytes())
        except (SyntaxError, ValueError):
            return set(), [SYNTAX_ERROR]
        out: set[str] = set()
        unresolved: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots = self.roots_for(file, alias.name.split(".")[0])
                    hit = self.resolve_absolute(alias.name, roots)
                    if hit:
                        out.add(hit)
            elif isinstance(node, ast.ImportFrom):
                if node.module == "__future__":
                    continue
                mod = node.module or ""
                roots = [] if node.level else self.roots_for(file, mod.split(".")[0])
                pkg = self._lookup(file, node.level, mod, roots)
                hits = [
                    self._lookup(file, node.level, f"{mod}.{a.name}" if mod else a.name, roots)
                    if a.name != "*"
                    else None
                    for a in node.names
                ]
                out.update(h for h in hits if h)
                if pkg and not all(hits):
                    out.add(pkg)
                if node.level and not pkg and not any(hits):
                    unresolved.append("." * node.level + (node.module or ""))
        out.discard(file)
        return out, unresolved


def python_edges(repo: Path, files: list[str]) -> tuple[set[tuple[str, str]], dict]:
    resolver = PythonResolver(repo, files)
    edges: set[tuple[str, str]] = set()
    unresolved: dict[str, list[str]] = {}
    for f in files:
        if f.endswith(".py"):
            dsts, unres = resolver.imports_of(f)
            edges |= {(f, d) for d in dsts}
            if unres:
                unresolved[f] = unres
    return edges, unresolved


# ------------------------------------------------------------------------- TS


def ts_edges(repo: Path, files: list[str], ts_path: str | None) -> tuple[set, dict]:
    node = shutil.which("node")
    if node is None:
        print("warning: node not installed; TS/JS edges skipped", file=sys.stderr)
        return set(), {}
    cmd = [node, str(HERE / "ts_resolve.mjs"), "--root", str(repo)]
    if ts_path:
        cmd += ["--ts-path", ts_path]
    res = subprocess.run(cmd, input=json.dumps(files), capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ts_resolve.mjs failed: {res.stderr.strip()[-2000:]}")
    data = json.loads(res.stdout)
    return {tuple(e) for e in data["edges"]}, data["unresolved"]


# ------------------------------------------------------------------------- Go

_GO_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_GO_IMPORT = re.compile(r'\bimport\s*(?:\(([^)]*)\)|(?:[\w.]+\s+)?"([^"]+)")')
_GO_SPEC = re.compile(r'"([^"]+)"')
_GO_PACKAGE = re.compile(r"^\s*package\s+(\w+)", re.M)


def _go_file_imports(text: str) -> set[str]:
    text = _GO_COMMENT.sub("", text)
    # Imports precede every other declaration; stop at the first func/type/var.
    head = re.split(r"^\s*(?:func|type|var|const)\b", text, maxsplit=1, flags=re.M)[0]
    out = set()
    for block, single in _GO_IMPORT.findall(head):
        out |= set(_GO_SPEC.findall(block)) if block else {single}
    return out


def go_edges(repo: Path, files: list[str]) -> tuple[set, dict]:
    mods = [f for f in files if PurePosixPath(f).name == "go.mod"]
    if not mods:
        return set(), {}
    go = shutil.which("go")
    if go is None:
        print("warning: go not installed; Go files left out of scope", file=sys.stderr)
        return set(), {f: [NOT_LISTED] for f in files if f.endswith(".go")}
    # go list loads the module graph, so it may fetch dependency metadata into
    # the module cache (never into the repo). Each go.mod is listed on its own.
    env = {**os.environ, "GOTOOLCHAIN": "local", "GOWORK": "off", "CGO_ENABLED": "0"}
    tracked = set(files)
    pkg_files: dict[str, list[str]] = {}  # import path → non-test files
    sources: set[str] = set()
    for mod in mods:
        mod_dir = repo / PurePosixPath(mod).parent
        res = subprocess.run(
            [go, "list", "-e", "-json", "./..."],
            cwd=mod_dir,
            env=env,
            capture_output=True,
            text=True,
        )
        dec, s, i = json.JSONDecoder(), res.stdout, 0
        while i < len(s):
            while i < len(s) and s[i].isspace():
                i += 1
            if i >= len(s):
                break
            pkg, i = dec.raw_decode(s, i)
            d = Path(pkg.get("Dir", ""))
            try:
                rel_dir = d.relative_to(repo).as_posix()
            except ValueError:
                continue
            prefix = "" if rel_dir == "." else f"{rel_dir}/"
            own = [prefix + n for n in pkg.get("GoFiles", []) + pkg.get("CgoFiles", [])]
            # Build-tag-excluded files of the same package count too.
            for n in pkg.get("IgnoredGoFiles", []):
                p = prefix + n
                if p in tracked and not n.endswith("_test.go"):
                    m = _GO_PACKAGE.search(
                        _GO_COMMENT.sub("", (repo / p).read_text(errors="ignore"))
                    )
                    if m and m.group(1) == pkg.get("Name"):
                        own.append(p)
            own = [p for p in own if p in tracked]
            pkg_files.setdefault(pkg["ImportPath"], []).extend(own)
            tests = [prefix + n for n in pkg.get("TestGoFiles", []) + pkg.get("XTestGoFiles", [])]
            sources |= set(own) | {p for p in tests if p in tracked}
    edges = set()
    for src in sorted(sources):
        for imp in _go_file_imports((repo / src).read_text(errors="ignore")):
            for dst in pkg_files.get(imp, ()):
                if dst != src:
                    edges.add((src, dst))
    # Files `./...` skips (`_`/`.`-prefixed dirs, testdata) have no package
    # data to resolve against.
    skipped = [f for f in files if f.endswith(".go") and f not in sources]
    return edges, {f: [NOT_LISTED] for f in skipped}


# ------------------------------------------------------------------ assembly


def oracle_edges(repo: Path, langs=("python", "typescript", "go"), ts_path=None) -> dict:
    """{"edges": {(src, dst)}, "sources": {files parsed}, "unresolved": {...}}."""
    repo = repo.resolve()
    files = tracked_files(repo)
    edges: set[tuple[str, str]] = set()
    unresolved: dict = {}
    if "python" in langs:
        e, u = python_edges(repo, files)
        edges |= e
        unresolved.update(u)
    if "typescript" in langs:
        e, u = ts_edges(repo, files, ts_path or os.environ.get("REPOWISE_ORACLE_TS"))
        edges |= e
        unresolved.update(u)
    if "go" in langs:
        e, u = go_edges(repo, files)
        edges |= e
        unresolved.update(u)
    # A file this interpreter can't parse (e.g. newer-Python syntax), or that
    # `go list` never saw, has unknown imports: leave it out of scope rather
    # than grade it as importless.
    unparsed = {f for f, u in unresolved.items() if OUT_OF_SCOPE.intersection(u)}
    sources = {
        f for f in files if lang_of(f) in langs and "node_modules/" not in f and f not in unparsed
    }
    return {"edges": edges, "sources": sources, "unresolved": unresolved}


# ---------------------------------------------------------------- comparison


def load_graph_edges(path: Path, edge_types=DEFAULT_EDGE_TYPES, include_hints=False):
    """(edges, file_nodes) from a knowledge-graph.json or a full graph export.

    Edges to ``external:`` targets and non-file nodes are dropped. Edges with a
    ``hint`` (convention passes such as Go ``same_package``) are not imports
    and are dropped unless ``include_hints``.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if "graph" in data:  # full graph export: ids are paths
        g = data["graph"]
        files = {n["id"] for n in g["nodes"] if n.get("node_type") == "file"}
        raw = [(e["source"], e["target"], e.get("edge_type"), e.get("hint")) for e in g["edges"]]
    else:  # knowledge-graph.json: ids are "file:<path>"
        files = {
            n["filePath"]
            for n in data.get("nodes", [])
            if n.get("type") == "file" and n.get("filePath")
        }

        def strip(i: str) -> str:
            return i[5:] if i.startswith("file:") else i

        raw = [
            (strip(e["source"]), strip(e["target"]), e.get("type"), e.get("hint"))
            for e in data.get("edges", [])
        ]
    edges = {
        (s, t)
        for s, t, typ, hint in raw
        if typ in edge_types and s in files and t in files and (include_hints or not hint)
    }
    return edges, files


def compare(
    oracle: dict,
    graph_edges: set,
    graph_files: set,
    sample: int = 25,
    exclude_src: str | None = None,
) -> dict:
    """Per-language precision/recall of graph edges against the oracle.

    Scope: source files the oracle parsed that the graph also indexed (minus
    ``exclude_src`` matches). Oracle edges whose target the graph did not index
    are a file-set gap, not an edge-resolution error: ``recall`` leaves them
    out, ``recall_all`` counts them as misses.
    """
    scope = oracle["sources"] & graph_files
    if exclude_src:
        scope = {f for f in scope if not re.search(exclude_src, f)}
    per: dict[str, dict] = defaultdict(lambda: {"tp": [], "fp": [], "fn": [], "dst_not_indexed": 0})
    truth = set()
    for s, d in oracle["edges"]:
        if s not in scope:
            continue
        if d not in graph_files:
            per[lang_of(s)]["dst_not_indexed"] += 1
        else:
            truth.add((s, d))
    got = {(s, d) for s, d in graph_edges if s in scope}
    for e in truth | got:
        key = "tp" if e in truth and e in got else "fn" if e in truth else "fp"
        per[lang_of(e[0])][key].append(e)
    out = {}
    for lang, b in sorted(per.items()):
        tp, fp, fn = len(b["tp"]), len(b["fp"]), len(b["fn"])
        out[lang] = {
            "sources": sum(1 for f in scope if lang_of(f) == lang),
            "oracle_edges": tp + fn,
            "graph_edges": tp + fp,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None,
            "recall_all": round(tp / (tp + fn + b["dst_not_indexed"]), 4)
            if tp + fn + b["dst_not_indexed"]
            else None,
            "dst_not_indexed": b["dst_not_indexed"],
            "fp_sample": [list(e) for e in sorted(b["fp"])[:sample]],
            "fn_sample": [list(e) for e in sorted(b["fn"])[:sample]],
        }
    return out


def _pct(x):
    return "n/a" if x is None else f"{x:.1%}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("edges", "compare"):
        p = sub.add_parser(name)
        p.add_argument("repo", type=Path)
        p.add_argument("--langs", default="python,typescript,go")
        p.add_argument("--ts-path", help="directory of the `typescript` npm package")
        if name == "edges":
            p.add_argument("-o", "--out", type=Path)
        else:
            p.add_argument(
                "--graph",
                type=Path,
                help="knowledge-graph.json or graph export "
                "(default REPO/.repowise/knowledge-graph.json)",
            )
            p.add_argument("--edge-types", default=",".join(DEFAULT_EDGE_TYPES))
            p.add_argument("--include-hints", action="store_true")
            p.add_argument(
                "--exclude-src",
                metavar="REGEX",
                help="leave matching source files out of the score (e.g. tests)",
            )
            p.add_argument("--details", type=Path, help="write FP/FN samples as JSON")
            p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    langs = tuple(args.langs.split(","))
    oracle = oracle_edges(args.repo, langs, args.ts_path)

    if args.cmd == "edges":
        doc = {
            "edges": [{"src": s, "dst": d} for s, d in sorted(oracle["edges"])],
            "unresolved": oracle["unresolved"],
        }
        text = json.dumps(doc, indent=1)
        if args.out:
            args.out.write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
        return 0

    graph_path = args.graph or args.repo / ".repowise" / "knowledge-graph.json"
    g_edges, g_files = load_graph_edges(
        graph_path, tuple(args.edge_types.split(",")), args.include_hints
    )
    result = compare(oracle, g_edges, g_files, exclude_src=args.exclude_src)
    if args.details:
        args.details.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    if args.json:
        print(
            json.dumps(
                {
                    k: {m: v for m, v in r.items() if not m.endswith("_sample")}
                    for k, r in result.items()
                },
                indent=1,
            )
        )
        return 0
    print(
        "| lang | sources | oracle | graph | TP | FP | FN | precision | recall "
        "| dst not indexed | recall incl. |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for lang, r in result.items():
        print(
            f"| {lang} | {r['sources']} | {r['oracle_edges']} | {r['graph_edges']} | {r['tp']} "
            f"| {r['fp']} | {r['fn']} | {_pct(r['precision'])} | {_pct(r['recall'])} "
            f"| {r['dst_not_indexed']} | {_pct(r['recall_all'])} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
