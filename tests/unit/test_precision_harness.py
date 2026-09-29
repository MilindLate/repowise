"""Unit tests for the precision run (scripts/kg_validate/precision.py and
run.py --precision helpers): split filtering, compare table, regression and
new-floor-failure detection. No network, no indexing."""

from __future__ import annotations

import sqlite3
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
KG_VALIDATE = REPO_ROOT / "scripts" / "kg_validate"
sys.path.insert(0, str(KG_VALIDATE))

import precision  # noqa: E402
import run  # noqa: E402

MATRIX = {
    "a": {"split": "dev", "ci": "fast"},
    "b": {"split": "heldout", "ci": "nightly"},
    "c": {"split": "dev", "ci": "nightly"},
}


def _res(repo, *, tp=90, fp=10, fn=10, p5=0.8, b3p=1.0, err=0.1, split="dev"):
    return {
        "repo": repo,
        "split": split,
        "families": {
            "imports": {"python": {"tp": tp, "fp": fp, "fn": fn, "dst_not_indexed": 0}},
            "entry_points": {"p_at_5": p5, "recall": 1.0},
            "identity": {"b3_precision": b3p, "b3_recall": 1.0, "person_count_error": err},
        },
    }


# --- split filtering ---------------------------------------------------------


def test_select_repos_by_split():
    assert precision.select_repos(MATRIX, "dev") == ["a", "c"]
    assert precision.select_repos(MATRIX, "heldout") == ["b"]
    assert precision.select_repos(MATRIX, "all") == ["a", "b", "c"]
    assert precision.select_repos(MATRIX, "dev", ["c"]) == ["c"]
    assert precision.select_repos(MATRIX, "dev", ci="fast") == ["a"]
    assert precision.select_repos(MATRIX, "all", ci="nightly") == ["b", "c"]


def test_select_repos_rejects_repo_outside_split():
    with pytest.raises(ValueError, match="heldout"):
        precision.select_repos(MATRIX, "heldout", ["a"])


def test_matrix_entries_are_split_and_pinned():
    with open(KG_VALIDATE / "matrix.toml", "rb") as fh:
        matrix = tomllib.load(fh)
    for name, spec in matrix.items():
        assert spec["split"] in ("dev", "heldout"), name
        assert spec["ci"] in ("fast", "nightly"), name
        assert len(spec["sha"]) == 40 and int(spec["sha"], 16) >= 0, name
    heldout = set(precision.select_repos(matrix, "heldout"))
    assert heldout == {"mlflow", "cal-com", "rich", "bubbletea", "spring-petclinic"}
    fast = {n for n, s in matrix.items() if s["ci"] == "fast"}
    assert fast <= set(precision.select_repos(matrix, "dev"))


# --- metrics and compare -----------------------------------------------------


def test_family_metrics_pools_import_languages():
    res = _res("a")
    res["families"]["imports"]["go"] = {"tp": 10, "fp": 0, "fn": 30, "dst_not_indexed": 5}
    m = precision.family_metrics(res)
    assert m["imports.python"] == {"precision": 0.9, "recall": 0.9}
    assert m["imports.go"] == {"precision": 1.0, "recall": 0.25}
    assert m["imports"]["precision"] == pytest.approx(100 / 110)
    assert m["imports"]["recall"] == pytest.approx(100 / 140)
    assert m["identity"]["person_count_error"] == 0.1


def _row(rows, fam, metric):
    return next(r for r in rows if r.family == fam and r.metric == metric)


def test_compare_flags_regressions_beyond_two_points():
    base = {"a": _res("a", tp=90, fp=10, p5=0.8, err=0.10)}
    # precision 90% -> 86.5% (-3.5pp), P@5 exactly -2pp, count error +3pp (worse)
    now = _res("a", tp=90, fp=14, p5=0.78, err=0.13)
    rows = precision.compare([now], base)
    assert _row(rows, "imports", "precision").regressed
    assert not _row(rows, "entry_points", "p_at_5").regressed  # 2pp is tolerated
    assert _row(rows, "identity", "person_count_error").regressed


def test_compare_lower_error_and_higher_rates_are_improvements():
    base = {"a": _res("a", tp=50, fp=50, err=0.5)}
    rows = precision.compare([_res("a", tp=90, fp=10, err=0.0)], base)
    assert not any(r.regressed for r in rows)
    assert _row(rows, "imports", "precision").delta == pytest.approx(0.4)


def test_compare_without_baseline_marks_new_and_never_regresses():
    rows = precision.compare([_res("a")], {})
    assert rows and all(r.baseline is None and not r.regressed for r in rows)
    table = precision.render_compare(rows)
    assert "| a | imports | precision | - | 90.0% | - | new |" in table


def test_render_compare_shows_delta_and_flag():
    rows = precision.compare([_res("a", tp=80, fp=20)], {"a": _res("a")})
    line = next(ln for ln in precision.render_compare(rows).splitlines() if "imports | prec" in ln)
    assert line == "| a | imports | precision | 90.0% | 80.0% | -10.0pp | REGRESSED |"


# --- floors ------------------------------------------------------------------

THRESHOLDS = {
    "family": {
        "imports": {"precision": 0.95, "recall": 0.8},
        "entry_points": {"metrics": {"p_at_5": {"min": 0.9}}},
        "identity": {"metrics": {"person_count_error": {"max": 0.2}}},
    }
}


def test_evaluate_results_applies_floors():
    checks = precision.evaluate_results([_res("a")], THRESHOLDS)
    status = {(c.threshold, c.repo, c.metric): c.status for c in checks}
    assert status[("imports", "*", "precision")] == "fail"  # 90% < 95%
    assert status[("imports", "*", "recall")] == "pass"
    assert status[("entry_points", "a", "p_at_5")] == "fail"
    assert status[("identity", "a", "person_count_error")] == "pass"


def test_known_floor_failures_are_not_new():
    before = precision.evaluate_results([_res("a")], THRESHOLDS)
    now = precision.evaluate_results([_res("a", err=0.3)], THRESHOLDS)
    new = precision.new_failures(now, before)
    assert [(c.threshold, c.metric) for c in new] == [("identity", "person_count_error")]
    text = precision.render_checks(now, new)
    assert "| entry_points | a | p_at_5 | 0.8 | >= 0.9 | known |" in text
    assert "| identity | a | person_count_error | 0.3 | <= 0.2 | NEW |" in text


def test_min_findings_floor_sees_synthetic_import_rows():
    checks = precision.evaluate_results(
        [_res("a", tp=3, fp=0, fn=0)], {"family": {"imports": {"min_findings": 5}}}
    )
    assert [(c.metric, c.value, c.status) for c in checks] == [("findings", 3, "fail")]


# --- held-out aggregate ------------------------------------------------------


def test_aggregate_hides_repo_names():
    agg = precision.aggregate(
        [_res("x", tp=10, fp=0, p5=1.0, split="heldout"), _res("y", tp=0, fp=10, p5=0.5)]
    )
    assert agg["repo"] == precision.HELDOUT and agg["repos"] == 2
    assert agg["families"]["imports"]["python"]["tp"] == 10
    assert agg["families"]["imports"]["python"]["fp"] == 10
    assert agg["families"]["entry_points"]["p_at_5"] == 0.75
    out = precision.render_compare(precision.compare([agg], {}))
    out += precision.render_checks(precision.evaluate_results([agg], THRESHOLDS), [])
    assert "| x |" not in out and "| y |" not in out
    assert "| heldout |" in out


# --- repowise side: merged identities from the index DB ----------------------


def test_predicted_identities_groups_by_owner_key(tmp_path):
    db = tmp_path / "wiki.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE git_commits (author_name TEXT, author_email TEXT)")
        conn.executemany(
            "INSERT INTO git_commits VALUES (?, ?)",
            [
                ("Jane Doe", "jane@example.com"),
                ("Jane Doe", "123+jdoe@users.noreply.github.com"),
                ("Jane Doe", "jane@example.com"),
                ("Bob", "Bob@Example.com"),
            ],
        )
    clusters = sorted(run.predicted_identities(db))
    assert clusters == [
        ["123+jdoe@users.noreply.github.com", "jane@example.com"],
        ["bob@example.com"],
    ]
