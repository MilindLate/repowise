"""Unit tests for the package oracle (scripts/kg_validate/oracles/packages.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "kg_validate"))

import precision  # noqa: E402
from oracles import packages  # noqa: E402


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_declared_members_across_ecosystems(tmp_path):
    _write(
        tmp_path,
        "pnpm-workspace.yaml",
        "packages:\n  - 'packages/*'\n  - \"!packages/skip\"\n  - e2e-tests/*\n"
        "catalog:\n  react: ^19\n",
    )
    for rel in ("packages/core", "packages/skip", "e2e-tests/smoke"):
        _write(tmp_path, f"{rel}/package.json", "{}")
    _write(tmp_path, "packages/empty/README.md")  # no manifest: not a member
    _write(tmp_path, "Cargo.toml", "[workspace]\nmembers = ['crates/*']\n")
    _write(tmp_path, "crates/cli/Cargo.toml", "[package]\nname = 'cli'\n")
    _write(tmp_path, "crates/cli_integration_tests/Cargo.toml", "[package]\nname = 't'\n")
    _write(tmp_path, "pyproject.toml", "[tool.uv.workspace]\nmembers = ['py/*']\n")
    _write(tmp_path, "py/sdk/pyproject.toml", "[project]\nname = 'sdk'\n")
    _write(tmp_path, "go.work", "go 1.22\nuse (\n  ./svc // api\n)\n")
    _write(tmp_path, "svc/go.mod", "module x/svc\n")

    assert packages.declared_members(tmp_path) == {"packages/core", "crates/cli", "py/sdk", "svc"}


def test_npm_workspaces_object_form(tmp_path):
    _write(tmp_path, "package.json", json.dumps({"workspaces": {"packages": ["libs/*"]}}))
    _write(tmp_path, "libs/a/package.json", "{}")
    _write(tmp_path, "libs/examples/package.json", "{}")
    assert packages.declared_members(tmp_path) == {"libs/a"}


def test_score_and_no_truth():
    s = packages.score(["a", "b", "c"], {"a", "b", "d"})
    assert (round(s["precision"], 3), round(s["recall"], 3)) == (0.667, 0.667)
    assert s["fp_sample"] == ["c"] and s["fn_sample"] == ["d"]
    # A repo with no workspace declaration has nothing to grade against.
    empty = packages.score(["a"], set())
    assert empty["precision"] is None and empty["recall"] is None


def test_packages_family_in_compare_table():
    assert "packages" in precision.FAMILIES
    res = {"repo": "r", "families": {"packages": {"precision": 0.9, "recall": 1.0}}}
    base = {"repo": "r", "families": {"packages": {"precision": 0.5, "recall": 1.0}}}
    rows = {(r.family, r.metric): r for r in precision.compare([res], {"r": base})}
    assert rows[("packages", "precision")].delta == 0.9 - 0.5
    assert not rows[("packages", "precision")].regressed
