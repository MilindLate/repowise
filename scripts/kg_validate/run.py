#!/usr/bin/env python3
"""KG validation harness — index the pinned matrix, check smells, diff baselines.

Usage (from the repo root):

    python scripts/kg_validate/run.py                  # check all matrix repos
    python scripts/kg_validate/run.py chi express      # subset
    python scripts/kg_validate/run.py --skip-index     # reuse existing exports
    python scripts/kg_validate/run.py --update-baselines
    python scripts/kg_validate/run.py --json           # machine-readable
    python scripts/kg_validate/run.py --modules-report /tmp/modules.md

    # precision against the mechanical oracles (imports, entry points, identity)
    python scripts/kg_validate/run.py --precision --split dev \
        --compare scripts/kg_validate/precision_baselines/
    python scripts/kg_validate/run.py --precision --repos click,cobra --families imports
    python scripts/kg_validate/run.py --precision --update-precision-baselines

Environment:
    KG_VALIDATE_DIR   clone/work dir (default /tmp/kg-validate)
    REPOWISE_PY       python used to run the indexer (default: this python)

Each repo is cloned at its pinned SHA from matrix.toml, indexed with
REPOWISE_KG_CURATION=1, and its exported knowledge-graph.json is checked by
kg_checks. The previous run's ``.repowise/`` output is wiped before indexing
— indexing output must never contaminate the next run's input.

Baselines live next to this script in ``baselines/<repo>.json`` and are
committed; the density_regression smell diffs against them.

``--precision`` instead grades each repo against the oracles in ``oracles/``
and the floors in ``thresholds.toml`` (via score.py), and with ``--compare``
prints a before/after table against ``precision_baselines/<repo>.json``. It
exits 1 on a >2pp regression, on a floor that fails now but did not fail on
the baselines, or on a harness error. The held-out split is reported only
as one aggregate. Precision clones are fetched ``--depth`` PRECISION_DEPTH.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
BASELINE_DIR = HERE / "baselines"
PRECISION_BASELINE_DIR = HERE / "precision_baselines"
# History depth for precision clones: covers the git indexer's default
# 500-commit window, so the identity oracle and repowise see the same authors.
PRECISION_DEPTH = 500
WORK_DIR = Path(os.environ.get("KG_VALIDATE_DIR", "/tmp/kg-validate"))
PY = os.environ.get("REPOWISE_PY", sys.executable)
PYTHONPATH = os.pathsep.join(
    str(REPO_ROOT / p) for p in ("packages/core/src", "packages/cli/src", "packages/server/src")
)

sys.path.insert(0, str(HERE))
import precision  # noqa: E402
import score  # noqa: E402
from kg_checks import RepoReport, compute_stats, run_smells  # noqa: E402


def load_matrix() -> dict[str, dict]:
    with open(HERE / "matrix.toml", "rb") as fh:
        return tomllib.load(fh)


def import_support_map() -> dict[str, str]:
    sys.path.insert(0, str(REPO_ROOT / "packages/core/src"))
    from repowise.core.ingestion.languages.registry import REGISTRY

    return REGISTRY.import_support_map()


def ensure_clone(name: str, spec: dict, depth: int | None = None) -> Path:
    """Clone ``spec`` at its pinned SHA; ``depth`` shallow-fetches remote sources."""
    dest = WORK_DIR / name
    source = spec["source"]
    remote = source.startswith(("http://", "https://", "git@"))
    if depth and remote and not (dest / ".git").exists():
        print(f"  fetching {name} at depth {depth} …")
        git = ["git", "-C", str(dest)]
        try:
            subprocess.run(["git", "init", "--quiet", str(dest)], check=True)
            subprocess.run([*git, "remote", "add", "origin", source], check=True)
            subprocess.run(
                [*git, "fetch", "--quiet", f"--depth={depth}", "origin", spec["sha"]], check=True
            )
            subprocess.run([*git, "checkout", "--quiet", "FETCH_HEAD"], check=True)
        except subprocess.CalledProcessError:
            shutil.rmtree(dest, ignore_errors=True)  # never leave a half-fetched clone
            raise
    if not (dest / ".git").exists():
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        if remote:
            src = source
        else:
            # Relative sources resolve against this script's dir first when
            # they name a committed bundle file (fixtures/*.bundle), else
            # against the repo root ("." = repowise itself). git clones
            # bundles like any other repo.
            local = HERE / source
            src = str(local if local.is_file() else REPO_ROOT / source)
        print(f"  cloning {name} from {src} …")
        subprocess.run(["git", "clone", "--quiet", src, str(dest)], check=True)
    head = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if head != spec["sha"]:
        fetched = subprocess.run(
            ["git", "-C", str(dest), "checkout", "--quiet", spec["sha"]], capture_output=True
        )
        if fetched.returncode != 0:
            subprocess.run(
                ["git", "-C", str(dest), "fetch", "--quiet", "origin", spec["sha"]], check=False
            )
            subprocess.run(["git", "-C", str(dest), "checkout", "--quiet", spec["sha"]], check=True)
        print(f"  {name}: checked out pinned {spec['sha'][:10]}")
    return dest


def index_repo(dest: Path) -> None:
    # Wipe previous output: indexing must never read its own prior artifacts.
    shutil.rmtree(dest / ".repowise", ignore_errors=True)
    env = {**os.environ, "REPOWISE_KG_CURATION": "1", "PYTHONPATH": PYTHONPATH}
    code = (
        f"from repowise.cli.main import cli; cli(['init', {str(dest)!r}, '--index-only', '--yes'])"
    )
    res = subprocess.run([PY, "-c", code], env=env, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"index failed for {dest.name}:\n{res.stdout[-2000:]}\n{res.stderr[-2000:]}"
        )


def check_repo(name: str, support: dict[str, str], *, skip_index: bool) -> RepoReport:
    matrix = load_matrix()
    spec = matrix[name]
    dest = ensure_clone(name, spec)
    if not skip_index:
        index_repo(dest)
    kg_path = dest / ".repowise" / "knowledge-graph.json"
    kg = json.loads(kg_path.read_text(encoding="utf-8"))

    stats = compute_stats(kg, support)
    baseline_path = BASELINE_DIR / f"{name}.json"
    baseline = (
        json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else None
    )
    smells = run_smells(kg, stats, baseline)
    report = RepoReport(repo=name, stats=stats, smells=smells)
    # Keep tour paths in the report so baseline diffs show walk changes.
    report.stats["tour_paths"] = [s.get("target_path") for s in kg.get("tour", [])]
    report.stats["entry_points"] = (kg.get("project") or {}).get("entry_points", [])
    layer_names = {lyr.get("id"): lyr.get("name", "") for lyr in kg.get("layers", [])}
    report.modules_detail = [
        {
            "name": m.get("name", ""),
            "path": m.get("path", ""),
            "layer": layer_names.get(m.get("layerId"), m.get("layerId", "")),
            "size": len(m.get("nodeIds", [])),
        }
        for m in kg.get("modules", [])
    ]
    return report


def predicted_identities(db_path: Path) -> list[list[str]]:
    """Repowise's merged author identities: commit emails grouped by the key the
    owner surfaces use (``build_identity_resolver`` over the indexed commits)."""
    from repowise.core.author_identity import build_identity_resolver

    with sqlite3.connect(db_path) as db:
        pairs = db.execute("SELECT DISTINCT author_name, author_email FROM git_commits").fetchall()
    resolve = build_identity_resolver(pairs)
    clusters: dict[str, set[str]] = {}
    for name, email in pairs:
        key = resolve(name, email)
        if key and email:
            clusters.setdefault(key, set()).add(email.strip().lower())
    return [sorted(c) for c in clusters.values()]


def measure_repo(name: str, spec: dict, families: list[str], *, skip_index: bool) -> dict:
    """Index one repo and grade it against the oracles for ``families``."""
    from oracles import entry_points as ep_oracle
    from oracles import identity as id_oracle
    from oracles import imports as imp_oracle

    dest = ensure_clone(name, spec, depth=PRECISION_DEPTH)
    if not skip_index:
        # Identity grades repowise's own merging, so it must not see the answer.
        with id_oracle.hidden_mailmap(dest):
            index_repo(dest)
    kg_path = dest / ".repowise" / "knowledge-graph.json"
    result = {"repo": name, "sha": spec["sha"], "split": spec["split"], "families": {}}
    fams = result["families"]
    if "imports" in families:
        oracle = imp_oracle.oracle_edges(dest)
        g_edges, g_files = imp_oracle.load_graph_edges(kg_path)
        fams["imports"] = precision.import_counts(imp_oracle.compare(oracle, g_edges, g_files))
    if "entry_points" in families:
        kg = json.loads(kg_path.read_text(encoding="utf-8"))
        predicted = (kg.get("project") or {}).get("entry_points") or []
        manifest = set(ep_oracle.manifest_entry_points(dest))
        gold = ep_oracle.load_gold(name)
        fams["entry_points"] = {
            **ep_oracle.score(predicted, manifest, set(gold) if gold is not None else None),
            "predicted_top5": predicted[:5],
            "manifest_count": len(manifest),
            "gold": gold is not None,
        }
    if "identity" in families:
        truth = id_oracle.truth_clusters(dest, id_oracle.load_alias_sets(name))
        predicted_ids = predicted_identities(dest / ".repowise" / "wiki.db")
        fams["identity"] = id_oracle.score(predicted_ids, truth)
    return result


def _load_baselines(base_dir: Path, names: list[str], families: list[str]) -> dict[str, dict]:
    out = {}
    for name in names:
        path = base_dir / f"{name}.json"
        if path.exists():
            base = json.loads(path.read_text(encoding="utf-8"))
            base["families"] = {k: v for k, v in base["families"].items() if k in families}
            out[name] = base
    return out


def precision_main(args, matrix: dict) -> int:
    thresholds = score.load_thresholds()
    families = args.families.split(",")
    unknown = [f for f in families if f not in precision.FAMILIES]
    if unknown:
        raise SystemExit(f"unknown families {unknown}; choose from {precision.FAMILIES}")
    repos = args.precision_repos.split(",") if args.precision_repos else None
    try:
        names = precision.select_repos(matrix, args.split, repos)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    heldout_total = sum(matrix[n]["split"] == "heldout" for n in names)
    results, errors = [], []
    for i, name in enumerate(names, 1):
        heldout = matrix[name]["split"] == "heldout"
        # Held-out repos are only ever reported in aggregate.
        print(f"== {f'held-out repo {i}/{len(names)}' if heldout else name} ==", flush=True)
        try:
            results.append(measure_repo(name, matrix[name], families, skip_index=args.skip_index))
        except Exception as exc:
            errors.append(name)
            print(f"  !! harness error: {type(exc).__name__}" + ("" if heldout else f": {exc}"))

    shown = [r for r in results if r["split"] == "dev"]
    held = [r for r in results if r["split"] == "heldout"]
    if held:
        shown.append(precision.aggregate(held))
        if len(held) < heldout_total:
            print(f"  held-out aggregate covers {len(held)}/{heldout_total} repos")

    base_dir = Path(args.compare) if args.compare else PRECISION_BASELINE_DIR
    baselines = (
        _load_baselines(base_dir, [r["repo"] for r in shown], families) if args.compare else {}
    )
    checks = precision.evaluate_results(shown, thresholds)
    before = precision.evaluate_results(list(baselines.values()), thresholds)
    new = precision.new_failures(checks, before)
    rows = precision.compare(shown, baselines)
    regressed = [r for r in rows if r.regressed]

    print()
    print(precision.render_compare(rows))
    print(precision.render_checks(checks, new))
    if args.as_json:
        report = {
            "results": shown,
            "rows": [{**vars(r), "delta": r.delta, "regressed": r.regressed} for r in rows],
            "checks": [vars(c) for c in checks],
            "new_failures": [vars(c) for c in new],
            "errors": len(errors) if held else errors,
        }
        print(json.dumps(report, indent=1, sort_keys=True))

    if args.update_precision_baselines:
        base_dir.mkdir(parents=True, exist_ok=True)
        for res in shown:
            path = base_dir / f"{res['repo']}.json"
            # A --families subset refreshes only the families it measured.
            if path.exists():
                old = json.loads(path.read_text(encoding="utf-8"))
                res = {**old, **res, "families": {**old.get("families", {}), **res["families"]}}
            path.write_text(json.dumps(res, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"precision baselines written to {base_dir}")

    print(
        f"{len(regressed)} regression(s) > {precision.REGRESSION_PP * 100:g}pp, "
        f"{len(new)} new floor failure(s), {len(errors)} harness error(s)"
    )
    return 1 if regressed or new or errors else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repos", nargs="*", help="subset of matrix repos")
    ap.add_argument("--skip-index", action="store_true")
    ap.add_argument("--update-baselines", action="store_true")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument(
        "--modules-report",
        metavar="PATH",
        help="write a per-repo curated-module inventory (markdown) for human review",
    )
    ap.add_argument(
        "--precision", action="store_true", help="grade against the oracles instead of smells"
    )
    ap.add_argument("--split", choices=precision.SPLITS, default="dev")
    ap.add_argument("--repos", dest="precision_repos", help="comma-separated subset of the split")
    ap.add_argument("--families", default=",".join(precision.FAMILIES))
    ap.add_argument("--compare", metavar="DIR", help="precision baselines to diff against")
    ap.add_argument(
        "--update-precision-baselines",
        action="store_true",
        help="write results to --compare DIR (default precision_baselines/)",
    )
    args = ap.parse_args()

    matrix = load_matrix()
    if args.precision:
        return precision_main(args, matrix)
    # The smell report is per repo, so it never runs on held-out repos.
    dev = precision.select_repos(matrix, "dev")
    names = args.repos or dev
    unknown = [n for n in names if n not in dev]
    if unknown:
        ap.error(f"not a dev repo in matrix.toml: {unknown}")

    support = import_support_map()
    reports: list[RepoReport] = []
    for name in names:
        print(f"== {name} ==")
        try:
            report = check_repo(name, support, skip_index=args.skip_index)
        except Exception as exc:
            report = RepoReport(repo=name, stats={}, smells=[])
            report.smells.append(type("S", (), {})())  # placeholder replaced below
            from kg_checks import Smell

            report.smells = [Smell("FAIL", "harness_error", str(exc)[:500])]
        reports.append(report)
        dom = report.stats.get("dominant_language")
        for lang, b in (report.stats.get("by_language") or {}).items():
            star = "*" if lang == dom else " "
            print(
                f"  {star}{lang:<12} files={b['files']:<5} imports/file={b['edges_per_file']:<6}"
                f" resolution={b['resolution_rate']} orphans={b['orphan_ratio']:.0%}"
                f" [{b['import_support']}]"
            )
        for s in report.smells:
            print(f"  !! {s.severity} {s.code}: {s.message}")
        if not report.smells:
            print("  OK — no smells")
        if args.update_baselines and not any(s.code == "harness_error" for s in report.smells):
            BASELINE_DIR.mkdir(exist_ok=True)
            (BASELINE_DIR / f"{name}.json").write_text(
                json.dumps(report.as_dict(), indent=1, sort_keys=True) + "\n", encoding="utf-8"
            )
            print("  baseline updated")

    if args.as_json:
        print(json.dumps([r.as_dict() for r in reports], indent=1, sort_keys=True))

    if args.modules_report:
        lines = ["# Curated module inventory", ""]
        for r in reports:
            lines.append(f"## {r.repo}")
            if not r.modules_detail:
                lines.extend(["", "_no modules in artifact_", ""])
                continue
            lines.extend(["", "| Module | Path | Layer | Files |", "|---|---|---|---|"])
            for m in sorted(r.modules_detail, key=lambda m: (-m["size"], m["name"])):
                lines.append(
                    f"| {m['name']} | `{m['path'] or '(layer root)'}` | {m['layer']} | {m['size']} |"
                )
            lines.append("")
        Path(args.modules_report).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"modules report written to {args.modules_report}")

    failed = [r.repo for r in reports if r.failed]
    print(
        f"\n{len(reports) - len(failed)}/{len(reports)} clean"
        + (f"; FAILED: {failed}" if failed else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
