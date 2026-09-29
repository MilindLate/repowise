#!/usr/bin/env python3
"""Package oracle: workspace members declared by the repo's root manifests.

Truth is what the package manager itself would build:

- ``pnpm-workspace.yaml`` ``packages`` (``!`` negations subtracted); without
  it, ``package.json`` ``workspaces`` (list or ``{"packages": [...]}``).
- ``Cargo.toml`` ``[workspace] members`` minus ``exclude``.
- ``pyproject.toml`` ``[tool.uv.workspace] members`` minus ``exclude``.
- ``go.work`` ``use`` directives.

A member counts only when its directory holds that ecosystem's manifest.
Members under test, fixture, example or benchmark trees are dropped: the
Packages table must not list them, declared or not. A repo with no
declaration has no truth and is not scored.

Never import repowise here: this grades repowise.

Usage:
    python scripts/kg_validate/oracles/packages.py <checkout>
Prints the declared member directories, one per line.
"""

from __future__ import annotations

import glob
import json
import re
import sys
import tomllib
from pathlib import Path, PurePosixPath

NON_PRODUCT_SEGMENTS = {
    "test",
    "tests",
    "__tests__",
    "e2e",
    "fixtures",
    "__fixtures__",
    "testdata",
    "example",
    "examples",
    "demo",
    "demos",
    "sample",
    "samples",
    "bench",
    "benches",
    "benchmarks",
}


def _is_product(rel: str) -> bool:
    # Whole words of each segment, so ``e2e-tests`` is a test tree and
    # ``latest`` is not.
    words = {w for seg in PurePosixPath(rel).parts for w in re.split(r"[-_.]", seg.lower())}
    return not (words & NON_PRODUCT_SEGMENTS)


def _expand(root: Path, includes: list, excludes: list) -> set[str]:
    def dirs(patterns: list) -> set[str]:
        out: set[str] = set()
        for pat in patterns:
            if not isinstance(pat, str) or not pat.strip():
                continue
            for hit in glob.glob(pat.strip().rstrip("/"), root_dir=root, recursive=True):
                if (root / hit).is_dir() and "node_modules" not in Path(hit).parts:
                    out.add(Path(hit).as_posix())
        return out

    return {d for d in dirs(includes) - dirs(excludes) if d not in (".", "")}


def _toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _js(root: Path) -> set[str]:
    pnpm = root / "pnpm-workspace.yaml"
    if pnpm.is_file():
        # Only the flat ``packages:`` list is needed; no YAML dependency.
        entries, inside = [], False
        for line in pnpm.read_text(encoding="utf-8").splitlines():
            if re.match(r"^packages\s*:", line):
                inside = True
            elif inside and re.match(r"^\s*-\s*", line):
                entries.append(re.sub(r"^\s*-\s*", "", line).split(" #")[0].strip().strip("'\""))
            elif inside and line.strip() and not line.startswith((" ", "\t", "#")):
                break
        inc = [e for e in entries if not e.startswith("!")]
        exc = [e[1:] for e in entries if e.startswith("!")]
        return _expand(root, inc, exc)
    try:
        ws = json.loads((root / "package.json").read_text(encoding="utf-8")).get("workspaces")
    except (OSError, ValueError, AttributeError):
        return set()
    if isinstance(ws, dict):
        ws = ws.get("packages")
    return _expand(root, ws, []) if isinstance(ws, list) else set()


def _toml_ws(root: Path, manifest: str, *keys: str) -> set[str]:
    data = _toml(root / manifest)
    for k in keys:
        data = data.get(k, {}) if isinstance(data, dict) else {}
    if not isinstance(data, dict) or not isinstance(data.get("members"), list):
        return set()
    return _expand(root, data["members"], data.get("exclude") or [])


def _go_work(root: Path) -> set[str]:
    try:
        text = re.sub(r"//[^\n]*", "", (root / "go.work").read_text(encoding="utf-8"))
    except OSError:
        return set()
    out: set[str] = set()
    for block, single in re.findall(r"^\s*use\s*(?:\(([^)]*)\)|(\S+))", text, re.M):
        for e in block.split() if block else [single]:
            rel = PurePosixPath(e.strip('"`')).as_posix().removeprefix("./")
            if rel not in (".", "") and (root / rel).is_dir():
                out.add(rel)
    return out


def declared_members(root: str | Path) -> set[str]:
    """Product member directories declared by any root workspace manifest."""
    root = Path(root)
    found: set[str] = set()
    for manifest, dirs in (
        ("package.json", _js(root)),
        ("Cargo.toml", _toml_ws(root, "Cargo.toml", "workspace")),
        ("pyproject.toml", _toml_ws(root, "pyproject.toml", "tool", "uv", "workspace")),
        ("go.mod", _go_work(root)),
    ):
        found |= {d for d in dirs if (root / d / manifest).is_file() and _is_product(d)}
    return found


def score(predicted: list[str], truth: set[str]) -> dict:
    """Precision/recall of the predicted package paths against declared members."""
    pred = set(predicted)
    tp = len(pred & truth)
    return {
        "precision": tp / len(pred) if pred and truth else None,
        "recall": tp / len(truth) if truth else None,
        "predicted": len(pred),
        "declared": len(truth),
        "fp_sample": sorted(pred - truth)[:10],
        "fn_sample": sorted(truth - pred)[:10],
    }


if __name__ == "__main__":
    for d in sorted(declared_members(sys.argv[1])):
        print(d)
