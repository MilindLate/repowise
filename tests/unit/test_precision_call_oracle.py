"""Unit tests for the call-edge oracle (scripts/kg_validate/oracles/calls.py)
and its ``calls`` family in precision.py.

The Python case needs jedi, which the oracle runs outside this venv (uv, or
REPOWISE_ORACLE_PY); the TS case needs node and the `typescript` npm package
(``npm ci`` at the repo root, or REPOWISE_ORACLE_TS). Each is skipped when
its resolver is unavailable here.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
KG_VALIDATE = REPO_ROOT / "scripts" / "kg_validate"
sys.path.insert(0, str(KG_VALIDATE))
sys.path.insert(0, str(KG_VALIDATE / "oracles"))

import calls as oracle  # noqa: E402
import precision  # noqa: E402

from tests.unit.test_precision_import_oracle import TS_DIR, _write  # noqa: E402

MISSING = oracle.unavailable(TS_DIR)


def _index(root: Path, files: list[str], nodes: list[tuple], edges: list[tuple]) -> Path:
    """A wiki.db with just the graph rows the oracle reads.

    ``nodes``: (id, file, name, start, end); ``edges``: (src, dst, conf, lines).
    """
    db_path = root / "wiki.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE graph_nodes (node_id TEXT, node_type TEXT, file_path TEXT, "
            "name TEXT, start_line INTEGER, end_line INTEGER)"
        )
        db.execute(
            "CREATE TABLE graph_edges (source_node_id TEXT, target_node_id TEXT, "
            "edge_type TEXT, confidence REAL, call_lines_json TEXT)"
        )
        db.executemany(
            "INSERT INTO graph_nodes VALUES (?, 'file', ?, NULL, 0, 0)", [(f, f) for f in files]
        )
        db.executemany("INSERT INTO graph_nodes VALUES (?, 'symbol', ?, ?, ?, ?)", nodes)
        db.executemany(
            "INSERT INTO graph_edges VALUES (?, ?, 'calls', ?, ?)",
            [(s, d, c, json.dumps(lines)) for s, d, c, lines in edges],
        )
    return db_path


# Each fixture's graph claims, per call line: a right callee (TP), a wrong
# method on the same line (FP), a stdlib/lib call claimed as an in-repo
# function (FP), and a claim on a line whose only call the oracle cannot
# resolve (unknown, not FP). Its recall frame has two hits, a method call and
# a local function call the graph missed (FN), the external call, and (Python)
# a nested function the graph has no node for.
EXPECTED_CLAIMS = {
    "0.95": {"tp": 1, "fp": 0, "unknown": 1},
    "0.9": {"tp": 1, "fp": 1, "unknown": 0},
    "0.5": {"tp": 0, "fp": 1, "unknown": 0},
}

PY_REPO = {
    "pkg/__init__.py": "",
    "pkg/a.py": (
        "from pkg.b import Thing, helper\n"
        "from os.path import join\n"
        "\n"
        "\n"
        "def local():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def caller():\n"
        "    helper()\n"  # 10
        "    Thing().run()\n"  # 11
        '    join("a", "b")\n'  # 12
        "    local()\n"  # 13
        "    mystery()\n"  # 14
        "\n"
        "    def inner():\n"
        "        pass\n"
        "\n"
        "    inner()\n"  # 19
    ),
    "pkg/b.py": (
        "def helper():\n"
        "    pass\n"
        "\n"
        "\n"
        "def join(*parts):\n"
        "    pass\n"
        "\n"
        "\n"
        "class Thing:\n"
        "    def run(self):\n"
        "        pass\n"
        "\n"
        "\n"
        "class Other:\n"
        "    def run(self):\n"
        "        pass\n"
    ),
}
PY_NODES = [
    ("pkg/a.py::local", "pkg/a.py", "local", 5, 6),
    ("pkg/a.py::caller", "pkg/a.py", "caller", 9, 19),
    ("pkg/b.py::helper", "pkg/b.py", "helper", 1, 2),
    ("pkg/b.py::join", "pkg/b.py", "join", 5, 6),
    ("pkg/b.py::Thing", "pkg/b.py", "Thing", 9, 11),
    ("pkg/b.py::Thing::run", "pkg/b.py", "run", 10, 11),
    ("pkg/b.py::Other", "pkg/b.py", "Other", 14, 16),
    ("pkg/b.py::Other::run", "pkg/b.py", "run", 15, 16),
]
PY_EDGES = [
    ("pkg/a.py::caller", "pkg/b.py::helper", 0.95, [10, 14]),
    ("pkg/a.py::caller", "pkg/b.py::Thing", 0.9, [11]),
    ("pkg/a.py::caller", "pkg/b.py::Other::run", 0.9, [11]),
    ("pkg/a.py::caller", "pkg/b.py::join", 0.5, [12]),
]


@pytest.mark.skipif("python" in MISSING, reason=MISSING.get("python", ""))
def test_python_claims_and_caller_recall(tmp_path):
    repo = _write(tmp_path / "repo", PY_REPO)
    db = _index(tmp_path, list(PY_REPO), PY_NODES, PY_EDGES)
    details: dict = {}
    result = oracle.measure(repo, db, sample=50, details=details)
    assert result["claims"] == EXPECTED_CLAIMS
    assert result["no_site"] == 0
    assert result["sites"] == {
        "hit": {"0.95": 1, "0.9": 1},
        "fn": 2,  # Thing().run (graph says Other.run), local()
        "unknown": 0,
        "external": 1,  # os.path.join
        "not_indexed": 1,  # inner()
    }
    assert result["population"] == {"claims": 5, "claims_high": 4, "frame": 6}
    verdicts = {(s["line"], s["name"]): s["verdict"] for s in details["sites"]}
    assert verdicts[(11, "run")] == "fn" and verdicts[(13, "local")] == "fn"
    # Seeded: the same sample every run.
    small = [oracle.measure(repo, db, sample=2, seed=7) for _ in range(2)]
    assert small[0] == small[1]
    assert sum(sum(c.values()) for c in small[0]["claims"].values()) == 3  # 2 high + 1 low


def test_python_without_jedi_is_unavailable(tmp_path, monkeypatch):
    if importlib.util.find_spec("jedi") is not None:
        pytest.skip("jedi is importable by this python")
    monkeypatch.setenv("REPOWISE_ORACLE_PY", sys.executable)
    repo = _write(tmp_path / "repo", PY_REPO)
    db = _index(tmp_path, list(PY_REPO), PY_NODES, PY_EDGES)
    result = oracle.measure(repo, db, ts_path=TS_DIR)
    assert "jedi" in result["unavailable"]["python"]
    assert result["claims"] == {} and result["population"]["claims"] == 0


TS_REPO = {
    "tsconfig.json": json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}),
    "src/b.ts": (
        "export function helper() {}\n"
        "export class Thing {\n"
        "  run() {}\n"
        "}\n"
        "export class Other {\n"
        "  run() {}\n"
        "}\n"
        "export function max() {}\n"
    ),
    "src/a.ts": (
        'import { helper, Thing } from "@/b";\n'
        'import { missing } from "not-installed";\n'
        "\n"
        "function local() {}\n"
        "\n"
        "export function caller() {\n"
        "  helper();\n"  # 7
        "  new Thing().run();\n"  # 8
        "  Math.max(1, 2);\n"  # 9
        "  local();\n"  # 10
        "  missing();\n"  # 11
        "}\n"
    ),
}
TS_NODES = [
    ("src/a.ts::local", "src/a.ts", "local", 4, 4),
    ("src/a.ts::caller", "src/a.ts", "caller", 6, 12),
    ("src/b.ts::helper", "src/b.ts", "helper", 1, 1),
    ("src/b.ts::Thing", "src/b.ts", "Thing", 2, 4),
    ("src/b.ts::Thing::run", "src/b.ts", "run", 3, 3),
    ("src/b.ts::Other", "src/b.ts", "Other", 5, 7),
    ("src/b.ts::Other::run", "src/b.ts", "run", 6, 6),
    ("src/b.ts::max", "src/b.ts", "max", 8, 8),
]
TS_EDGES = [
    ("src/a.ts::caller", "src/b.ts::helper", 0.95, [7, 11]),
    ("src/a.ts::caller", "src/b.ts::Thing", 0.9, [8]),
    ("src/a.ts::caller", "src/b.ts::Other::run", 0.9, [8]),
    ("src/a.ts::caller", "src/b.ts::max", 0.5, [9]),
]


@pytest.mark.skipif("typescript" in MISSING, reason=MISSING.get("typescript", ""))
def test_ts_claims_and_caller_recall_through_tsconfig_paths(tmp_path):
    repo = _write(tmp_path / "repo", TS_REPO)
    db = _index(tmp_path, [f for f in TS_REPO if f.endswith(".ts")], TS_NODES, TS_EDGES)
    result = oracle.measure(repo, db, sample=50, ts_path=TS_DIR)
    assert result["claims"] == EXPECTED_CLAIMS
    assert result["sites"] == {
        "hit": {"0.95": 1, "0.9": 1},
        "fn": 2,  # new Thing().run (graph says Other.run), local()
        "unknown": 0,
        "external": 1,  # Math.max, a TS lib declaration
        "not_indexed": 0,
    }


# --- precision.py: the calls family -------------------------------------------

CALLS = {
    "claims": {
        "0.95": {"tp": 9, "fp": 1, "unknown": 2},
        "0.5": {"tp": 1, "fp": 5, "unknown": 0},
    },
    "no_site": 0,
    "sites": {"hit": {"0.9": 3, "0.5": 1}, "fn": 4, "unknown": 2, "external": 5, "not_indexed": 1},
}


def _res(repo: str, split: str = "dev") -> dict:
    return {"repo": repo, "split": split, "families": {"calls": CALLS}}


def test_family_metrics_calls_precision_is_high_tier_only():
    m = precision.family_metrics(_res("a"))["calls"]
    assert m["precision"] == 0.9  # 9 / (9 + 1); the 0.5 tier is left out
    assert m["recall"] == 0.5  # 4 hits at any confidence / (4 + 4 misses)
    assert m["unknown_rate"] == pytest.approx(4 / 34)


def test_calls_floors_use_labels_for_precision_and_truth_for_recall():
    thresholds = {
        "family": {
            "calls": {"min_confidence": 0.9, "precision": 0.95, "min_findings": 12},
            "calls.callers": {"recall": 0.5},
        }
    }
    checks = precision.evaluate_results([_res("a")], thresholds)
    got = {(c.threshold, c.metric): (c.value, c.status) for c in checks}
    assert got[("calls", "precision")] == (0.9, "fail")
    assert got[("calls", "findings")] == (12, "pass")  # unknown claims count, unjudged
    assert got[("calls.callers", "recall")] == (0.5, "pass")


def test_unknown_rate_rising_is_a_regression():
    worse = {**CALLS, "sites": {**CALLS["sites"], "unknown": 12}}
    rows = precision.compare([{"repo": "a", "families": {"calls": worse}}], {"a": _res("a")})
    flagged = {r.metric for r in rows if r.regressed}
    assert flagged == {"unknown_rate"}


def test_aggregate_sums_call_counts():
    agg = precision.aggregate([_res("x", "heldout"), _res("y", "heldout")])
    calls = agg["families"]["calls"]
    assert calls["claims"]["0.95"] == {"tp": 18, "fp": 2, "unknown": 4}
    assert calls["sites"]["hit"] == {"0.9": 6, "0.5": 2}
    assert calls["sites"]["fn"] == 8
    assert precision.family_metrics(agg)["calls"]["precision"] == 0.9
