#!/usr/bin/env python3
"""Entry-point oracle: entry files declared by manifests and language rules.

Truth comes from what a build tool or runtime would actually start:

- package.json ``bin`` (string or map), ``main``, ``exports["."]``
  (conditions flattened, ``types`` skipped). A declared ``dist|build|lib|out``
  JS path missing from the checkout maps to ``src/`` sources.
- pyproject ``[project.scripts]``, ``[project.gui-scripts]`` and
  ``[tool.poetry.scripts]``, resolved to the module file.
- Go: files with ``func main()`` in a ``package main``.
- JVM: ``public static void main(`` / Kotlin top-level ``fun main(`` /
  ``@SpringBootApplication``.
- Next.js (a package depending on ``next``): ``app/**/page.*`` and
  ``route.*`` (``_private`` folders skipped), every ``pages/**`` module.

Entries under test, fixture and example trees are dropped, declared or
not: they are not the project's entry points (and a ranker must not
surface them).

A human gold set per repo lives in ``labels/<repo>/entry_points.json`` (see
labels/README.md). P@5 is scored against the gold set when one exists, else
against the manifest set; recall is always against the manifest set.

Never import repowise here: this grades repowise.

Usage:
    python scripts/kg_validate/oracles/entry_points.py <checkout> --repo NAME \\
        [--predicted ranked.json] [--labels-dir DIR]
Prints truth rows, or metric rows for score.py when --predicted is given.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from pathlib import Path, PurePosixPath

LABELS_DIR = Path(__file__).resolve().parent.parent / "labels"
PRUNE_DIRS = {".git", "node_modules", "vendor", ".venv", "venv", "__pycache__", ".next", ".tox"}
# Entries under these directory names are tests, fixtures or samples.
NON_PRODUCT_SEGMENTS = {"test", "tests", "testdata", "__tests__", "fixtures", "example", "examples"}
JS_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".mdx")
_DIST_RE = re.compile(r"^(?:dist|build|lib|out)/(.+)\.(?:m|c)?js$")
_GO_MAIN_PKG = re.compile(r"^package\s+main\b", re.M)
_GO_FUNC_MAIN = re.compile(r"^func\s+main\s*\(\s*\)", re.M)
_JAVA_MAIN = re.compile(r"public\s+static\s+void\s+main\s*\(")
_KT_MAIN = re.compile(r"^\s*fun\s+main\s*\(", re.M)
_SPRING = re.compile(r"@SpringBootApplication\b")


def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_product(rel: str) -> bool:
    return not NON_PRODUCT_SEGMENTS.intersection(PurePosixPath(rel).parts[:-1])


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


# --- package.json ------------------------------------------------------------


def _export_targets(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [t for v in value for t in _export_targets(v)]
    if isinstance(value, dict):
        return [t for k, v in value.items() if k != "types" for t in _export_targets(v)]
    return []


def _resolve_js(pkg_dir: Path, root: Path, target: str) -> str | None:
    spec = target.removeprefix("./")
    if "*" in spec or spec.endswith(".d.ts"):
        return None
    path = pkg_dir / spec
    if path.is_file():
        return _rel(root, path)
    for ext in JS_EXTS:  # extensionless "main": "index"
        if (pkg_dir / (spec + ext)).is_file():
            return _rel(root, pkg_dir / (spec + ext))
    m = _DIST_RE.match(spec)
    if m:  # built output not in the checkout: point at the sources
        for ext in (".ts", ".tsx", ".mts", ".js"):
            src = pkg_dir / "src" / (m.group(1) + ext)
            if src.is_file():
                return _rel(root, src)
    return None


def from_package_json(root: Path, manifest: Path) -> dict[str, str]:
    try:
        data = json.loads(_read(manifest))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    targets: list[tuple[str, str]] = []
    bin_ = data.get("bin")
    if isinstance(bin_, str):
        targets.append(("package.json bin", bin_))
    elif isinstance(bin_, dict):
        targets += [("package.json bin", v) for v in bin_.values() if isinstance(v, str)]
    if isinstance(data.get("main"), str):
        targets.append(("package.json main", data["main"]))
    exports = data.get("exports")
    if isinstance(exports, dict) and any(k.startswith(".") for k in exports):
        exports = exports.get(".")
    targets += [("package.json exports", t) for t in _export_targets(exports)]
    out = {}
    for reason, target in targets:
        rel = _resolve_js(manifest.parent, root, target)
        if rel:
            out.setdefault(rel, reason)
    return out


# --- pyproject ---------------------------------------------------------------


def _module_file(roots: list[tuple[str, Path]], root: Path, module: str) -> str | None:
    """Resolve a dotted module through ``(package prefix, directory)`` roots."""
    for prefix, base in roots:
        if prefix and module != prefix and not module.startswith(prefix + "."):
            continue
        parts = module[len(prefix) :].lstrip(".").split(".") if prefix else module.split(".")
        parts = [p for p in parts if p]
        for cand in (
            base.joinpath(*parts).with_suffix(".py") if parts else None,
            base.joinpath(*parts, "__init__.py"),
        ):
            if cand is not None and cand.is_file():
                return _rel(root, cand)
    return None


def from_pyproject(root: Path, manifest: Path) -> dict[str, str]:
    try:
        data = tomllib.loads(_read(manifest))
    except tomllib.TOMLDecodeError:
        return {}
    project = data.get("project", {})
    poetry = data.get("tool", {}).get("poetry", {})
    refs: list[str] = []
    for table in (
        project.get("scripts", {}),
        project.get("gui-scripts", {}),
        poetry.get("scripts", {}),
    ):
        for value in table.values():
            if isinstance(value, dict):  # poetry {reference = "...", type = "console"}
                value = value.get("reference") or value.get("callable")
            if isinstance(value, str):
                refs.append(value)
    base = manifest.parent
    tool = data.get("tool", {})
    setuptools = tool.get("setuptools", {})
    dirs = [base, base / "src"]
    dirs += [
        base / p["from"] for p in poetry.get("packages", []) if isinstance(p, dict) and "from" in p
    ]
    find = setuptools.get("packages", {})
    if isinstance(find, dict):
        dirs += [base / w for w in find.get("find", {}).get("where", [])]
    wheel = tool.get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    dirs += [(base / p).parent for p in wheel.get("packages", [])]
    roots = [("", d) for d in dirs]
    # setuptools package-dir: {"": "src"} or {"pkg.sub": "path/to/pkg/sub"}
    roots += [(k, base / v) for k, v in setuptools.get("package-dir", {}).items()]
    out = {}
    for ref in refs:
        rel = _module_file(roots, root, ref.split(":")[0].strip())
        if rel:
            out.setdefault(rel, "pyproject scripts")
    return out


# --- source-scanned rules ----------------------------------------------------


def _next_routes(root: Path, pkg_dir: Path) -> dict[str, str]:
    out = {}
    for base in (pkg_dir, pkg_dir / "src"):
        app, pages = base / "app", base / "pages"
        if app.is_dir():
            for path in _walk(app):
                rel_in_app = path.relative_to(app).parts
                if any(p.startswith("_") for p in rel_in_app[:-1]):
                    continue
                stem, ext = os.path.splitext(path.name)
                if stem in ("page", "route") and ext in JS_EXTS:
                    out[_rel(root, path)] = "next app route"
        if pages.is_dir():
            for path in _walk(pages):
                if path.suffix in JS_EXTS and not re.search(r"\.(test|spec)\.", path.name):
                    out[_rel(root, path)] = "next pages route"
    return out


def manifest_entry_points(root: str | Path) -> dict[str, str]:
    """All oracle entry points under ``root`` as ``{repo-relative path: reason}``."""
    root = Path(root).resolve()
    out: dict[str, str] = {}
    for path in _walk(root):
        name, rel = path.name, _rel(root, path)
        if name == "package.json":
            for k, v in from_package_json(root, path).items():
                out.setdefault(k, v)
            try:
                data = json.loads(_read(path))
            except json.JSONDecodeError:
                continue
            deps = (
                {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                if isinstance(data, dict)
                else {}
            )
            if "next" in deps:
                out.update(_next_routes(root, path.parent))
        elif name == "pyproject.toml":
            for k, v in from_pyproject(root, path).items():
                out.setdefault(k, v)
        elif name.endswith(".go") and not name.endswith("_test.go"):
            text = _read(path)
            if _GO_MAIN_PKG.search(text) and _GO_FUNC_MAIN.search(text):
                out[rel] = "go package main"
        elif name.endswith((".java", ".kt")):
            text = _read(path)
            if _SPRING.search(text):
                out[rel] = "spring boot application"
            elif _JAVA_MAIN.search(text) or (name.endswith(".kt") and _KT_MAIN.search(text)):
                out[rel] = "jvm main"
    return {k: v for k, v in out.items() if _is_product(k)}


# --- gold set and metrics ----------------------------------------------------


def load_gold(repo: str, labels_dir: str | Path = LABELS_DIR) -> list[str] | None:
    """The human gold set from ``labels/<repo>/entry_points.json``, or None."""
    path = Path(labels_dir) / repo / "entry_points.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    entries = data["entry_points"] if isinstance(data, dict) else data
    return [e["path"] if isinstance(e, dict) else e for e in entries]


def score(
    predicted: list[str], manifest: set[str], gold: set[str] | None = None, k: int = 5
) -> dict:
    """P@k of the ranked prediction and recall of the whole prediction.

    P@k divides by min(k, len(predicted)): a short list is not penalised for
    brevity (the recall floor covers that). Recall is against ``manifest``.
    """
    top = predicted[:k]
    truth = gold if gold is not None else manifest
    return {
        f"p_at_{k}": (sum(p in truth for p in top) / len(top)) if top else None,
        "recall": (len(set(predicted) & manifest) / len(manifest)) if manifest else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Entry-point oracle (manifests + gold set).")
    ap.add_argument("checkout", help="repo checkout to scan")
    ap.add_argument("--repo", required=True, help="repo name (labels/<repo>/)")
    ap.add_argument("--predicted", help="JSON list of the tool's ranked entry-point paths")
    ap.add_argument("--labels-dir", default=str(LABELS_DIR))
    args = ap.parse_args(argv)
    manifest = manifest_entry_points(args.checkout)
    if not args.predicted:
        for key, reason in sorted(manifest.items()):
            print(
                json.dumps(
                    {"repo": args.repo, "family": "entry_points", "key": key, "reason": reason}
                )
            )
        return 0
    predicted = json.loads(Path(args.predicted).read_text())
    gold = load_gold(args.repo, args.labels_dir)
    metrics = score(predicted, set(manifest), set(gold) if gold is not None else None)
    for metric, value in metrics.items():
        if value is not None:
            print(
                json.dumps(
                    {"repo": args.repo, "family": "entry_points", "metric": metric, "value": value}
                )
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
