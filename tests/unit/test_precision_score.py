"""Unit tests for the precision scorer (scripts/kg_validate/score.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "kg_validate"))

import score  # noqa: E402


@pytest.mark.parametrize(
    ("k", "n", "lo", "hi"),
    [
        # Newcombe (1998) Table I, method 3, and the textbook 8/10 case.
        (81, 263, 0.2553, 0.3662),
        (15, 148, 0.0624, 0.1605),
        (0, 20, 0.0, 0.1611),
        (8, 10, 0.4902, 0.9433),
    ],
)
def test_wilson_ci_known_values(k, n, lo, hi):
    got = score.wilson_ci(k, n)
    assert got[0] == pytest.approx(lo, abs=1e-4)
    assert got[1] == pytest.approx(hi, abs=1e-4)


def test_wilson_ci_empty():
    assert score.wilson_ci(0, 0) is None


def test_ece_toy_set():
    # Bin [0.9,1.0]: conf 0.9 x4, 3 correct -> |0.75-0.9| = 0.15, weight 4/6
    # Bin [0.2,0.3): conf 0.2 x2, 0 correct -> |0-0.2| = 0.2,  weight 2/6
    pairs = [(0.9, True)] * 3 + [(0.9, False)] + [(0.2, False)] * 2
    assert score.ece(pairs) == pytest.approx(0.15 * 4 / 6 + 0.2 * 2 / 6)


def test_ece_perfect_and_confidence_one():
    assert score.ece([(1.0, True)] * 5) == pytest.approx(0.0)
    assert score.ece([(0.5, True), (0.5, False)]) == pytest.approx(0.0)
    assert score.ece([]) is None


def test_labels_override_truth_and_unsure_ignored():
    scored = score.judge(
        findings=[
            {"repo": "r", "family": "imports", "key": "a>b"},
            {"repo": "r", "family": "imports", "key": "a>c"},
            {"repo": "r", "family": "imports", "key": "a>d"},
        ],
        truth=[{"repo": "r", "family": "imports", "key": "a>b"}],
        labels=[
            {"repo": "r", "finding_key": "a>c", "label": "tp"},
            {"repo": "r", "family": "imports", "finding_key": "a>d", "label": "unsure"},
        ],
    )
    verdicts = {f.key: f.correct for f in scored.findings}
    assert verdicts == {"a>b": True, "a>c": True, "a>d": False}


def test_group_table_precision_recall_and_tiers():
    findings = [
        {"repo": "r1", "family": "dead_code", "key": "x", "tier": "high", "confidence": 0.9},
        {"repo": "r1", "family": "dead_code", "key": "y", "tier": "high", "confidence": 0.9},
        {"repo": "r1", "family": "dead_code", "key": "z", "tier": "review", "confidence": 0.5},
        {"repo": "r2", "family": "dead_code", "key": "x", "tier": "high", "confidence": 0.9},
    ]
    truth = [
        {"repo": "r1", "family": "dead_code", "key": "x"},
        {"repo": "r1", "family": "dead_code", "key": "q"},
        {"repo": "r2", "family": "dead_code", "key": "x"},
    ]
    groups = {
        (g.repo, g.family, g.tier): g for g in score.group_table(score.judge(findings, truth, []))
    }
    r1 = groups[("r1", "dead_code", "*")]
    assert (r1.findings, r1.judged, r1.tp) == (3, 3, 1)
    assert r1.recall == pytest.approx(0.5)
    high = groups[("r1", "dead_code", "high")]
    assert high.precision == pytest.approx(0.5) and high.recall is None
    pooled = groups[("*", "dead_code", "*")]
    assert pooled.precision == pytest.approx(0.5)
    assert pooled.recall == pytest.approx(2 / 3)  # x@r1, x@r2 found; q@r1 missed


def _scored(n_tp, n_fp, family="dead_code", tier="safe_to_delete", repo="r"):
    findings = [
        {"repo": repo, "family": family, "key": f"k{i}", "tier": tier} for i in range(n_tp + n_fp)
    ]
    labels = [{"repo": repo, "finding_key": f"k{i}", "label": i < n_tp} for i in range(n_tp + n_fp)]
    return score.judge(findings, [], labels)


def _status(checks, metric, threshold=None):
    return [
        c.status
        for c in checks
        if c.metric == metric and (threshold is None or c.threshold == threshold)
    ]


THRESH = {
    "family": {
        "dead_code.safe_to_delete": {
            "precision": 0.95,
            "precision_ci_low": 0.90,
            "min_n": 60,
            "min_findings": 60,
        }
    }
}


def test_threshold_pass():
    checks = score.evaluate(_scored(100, 0), THRESH)
    assert {c.status for c in checks} == {"pass"}


def test_threshold_fails_on_ci_low_even_when_point_estimate_passes():
    # 58/60 = 96.7% point, but Wilson low ~ 88.6% < 90%.
    checks = score.evaluate(_scored(58, 2), THRESH)
    assert _status(checks, "precision") == ["pass"]
    assert _status(checks, "precision_ci_low") == ["fail"]


def test_threshold_min_n_and_count_floor():
    checks = score.evaluate(_scored(20, 0), THRESH)
    assert _status(checks, "judged_n") == ["fail"]
    assert _status(checks, "findings") == ["fail"]
    assert _status(checks, "precision") == ["pass"]


def test_count_floor_trips_when_family_emits_nothing():
    # Truth exists for the family but the tool emitted zero findings.
    scored = score.judge([], [{"repo": "r", "family": "imports", "key": "a>b"}], [])
    th = {"family": {"imports": {"precision": 0.98, "recall": 0.9, "min_findings": 1}}}
    checks = score.evaluate(scored, th)
    assert _status(checks, "recall") == ["fail"]
    assert _status(checks, "findings") == ["fail"]
    assert _status(checks, "precision") == ["unmeasured"]


def test_recall_floor_and_absent_family_skipped():
    findings = [{"repo": "r", "family": "imports", "key": "a>b"}]
    truth = [{"repo": "r", "family": "imports", "key": k} for k in ("a>b", "a>c", "a>d")]
    th = {"family": {"imports": {"precision": 0.98, "recall": 0.9}, "calls": {"precision": 0.95}}}
    checks = score.evaluate(score.judge(findings, truth, []), th)
    assert _status(checks, "precision") == ["pass"]
    assert _status(checks, "recall") == ["fail"]
    assert all(c.threshold != "calls" for c in checks)


def test_min_confidence_filter_and_ece_bound():
    findings = [
        {"repo": "r", "family": "calls", "key": "hi", "confidence": 0.97},
        {"repo": "r", "family": "calls", "key": "lo", "confidence": 0.3},
    ]
    labels = [
        {"repo": "r", "finding_key": "hi", "label": True},
        {"repo": "r", "finding_key": "lo", "label": False},
    ]
    th = {
        "calibration": {"ece_max": 0.05},
        "family": {"calls": {"min_confidence": 0.9, "precision": 0.95}},
    }
    checks = score.evaluate(score.judge(findings, [], labels), th)
    assert _status(checks, "precision") == ["pass"]  # the 0.3 miss is filtered out
    assert _status(checks, "ece") == ["pass"]  # |1-0.97| = 0.03


def test_per_tier_and_per_repo_and_metrics():
    findings = [
        {"repo": "a", "family": "health", "key": "1", "tier": "long_method"},
        {"repo": "a", "family": "health", "key": "2", "tier": "god_class"},
    ]
    labels = [
        {"repo": "a", "finding_key": "1", "label": "correct"},
        {"repo": "a", "finding_key": "2", "label": "wrong"},
    ]
    metrics = [
        {"repo": "a", "family": "identity", "metric": "b3_precision", "value": 0.995},
        {"repo": "a", "family": "identity", "metric": "person_count_error", "value": 0.2},
    ]
    th = {
        "family": {
            "health": {"per_tier": True, "precision": 0.85, "precision_per_repo": 0.4},
            "identity": {
                "metrics": {
                    "b3_precision": {"min": 0.99},
                    "person_count_error": {"max": 0.05},
                    "b3_recall": {"min": 0.9},
                }
            },
        }
    }
    checks = score.evaluate(score.judge(findings, [], labels, metrics), th)
    by = {(c.threshold, c.repo, c.metric): c.status for c in checks}
    assert by[("health.god_class", "*", "precision")] == "fail"
    assert by[("health.long_method", "*", "precision")] == "pass"
    assert by[("health", "a", "precision")] == "pass"
    assert by[("identity", "a", "b3_precision")] == "pass"
    assert by[("identity", "a", "person_count_error")] == "fail"
    assert by[("identity", "a", "b3_recall")] == "unmeasured"


def test_shipped_thresholds_parse_with_card_floors():
    th = score.load_thresholds()
    fam = th["family"]
    assert fam["dead_code.safe_to_delete"]["precision_ci_low"] == 0.90
    assert fam["dead_code.safe_to_delete"]["min_n"] == 60
    assert fam["entry_points"]["metrics"]["p_at_5"]["min"] == 0.95
    assert fam["identity"]["metrics"]["person_count_error"]["max"] == 0.05
    assert th["calibration"]["ece_max"] == 0.05


def test_cli_writes_json_and_markdown_and_exit_code(tmp_path):
    findings = tmp_path / "f.jsonl"
    findings.write_text(
        "\n".join(json.dumps({"family": "imports", "key": k}) for k in ("a>b", "a>x")) + "\n"
    )
    truth = tmp_path / "t.json"
    truth.write_text(json.dumps([{"family": "imports", "key": "a>b"}]))
    out_json, out_md = tmp_path / "o.json", tmp_path / "o.md"
    rc = score.main(
        [
            "--repo", "r",
            "--findings", str(findings),
            "--truth", str(truth),
            "--json", str(out_json),
            "--md", str(out_md),
        ]
    )  # fmt: skip
    assert rc == 1  # precision 50% < 98%
    report = json.loads(out_json.read_text())
    assert report["passed"] is False
    assert "| r | imports | * | 2 | 2 | 50.0% |" in out_md.read_text()


def test_cli_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        score.main(["--help"])
    assert exc.value.code == 0
