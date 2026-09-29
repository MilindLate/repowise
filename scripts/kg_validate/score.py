#!/usr/bin/env python3
"""Precision scorer: findings + ground truth + labels -> precision/recall per
repo x family x confidence tier, with Wilson 95% CIs, ECE, and pass/fail
against ``thresholds.toml``.

Inputs are JSON lists or JSONL files of flat rows. ``repo`` may be omitted
from any row when ``--repo`` supplies it.

- findings  ``{repo, family, key, tier?, confidence?}`` - what the tool emitted.
  ``family`` may already carry a dotted suffix; ``tier`` is the tool's
  confidence tier (e.g. ``safe_to_delete``) and ``confidence`` a 0-1 score.
- truth     ``{repo, family, key}`` - the complete positive set an oracle
  produced. A family with truth rows gets recall; its findings not in the
  set are false positives.
- labels    ``{repo, family?, finding_key, label}`` - human/LLM verdicts on
  individual findings. ``label`` is true/false or tp/fp, correct/wrong,
  yes/no; ``unsure`` is ignored. A label overrides truth membership.
- metrics   ``{repo, family, metric, value}`` - precomputed numbers from
  oracles whose metric is not a proportion (identity B-cubed, P@5, count
  error). Thresholds bound them with ``[family.X.metrics]``.

A finding with neither a label nor truth for its family is "unlabelled": it
counts towards the finding count but not precision.

Every other rate in thresholds.toml is phrased as "fraction of items judged
correct", so one precision column covers it (e.g. confidently-wrong <= 3% is
family ``get_answer.not_confidently_wrong`` with precision >= 0.97).

Usage:
    python scripts/kg_validate/score.py --findings f.jsonl --truth t.jsonl \\
        --labels l.jsonl --json out.json --md out.md
Exit code is 1 when any threshold fails, else 0.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tomllib
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_THRESHOLDS = HERE / "thresholds.toml"
ALL = "*"  # pooled repo / all-tiers marker
Z95 = 1.959963984540054

_TRUE = {"true", "tp", "correct", "yes", "1"}
_FALSE = {"false", "fp", "wrong", "incorrect", "no", "0"}


# --- statistics --------------------------------------------------------------


def wilson_ci(successes: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion; None when n == 0."""
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def ece(pairs: list[tuple[float, bool]], bins: int = 10) -> float | None:
    """Expected calibration error over equal-width confidence bins.

    ``pairs`` are (confidence, correct). Confidence 1.0 falls in the last bin.
    """
    if not pairs:
        return None
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for conf, ok in pairs:
        buckets[min(int(conf * bins), bins - 1)].append((conf, ok))
    total = len(pairs)
    err = 0.0
    for b in buckets:
        if b:
            acc = sum(ok for _, ok in b) / len(b)
            avg_conf = sum(c for c, _ in b) / len(b)
            err += abs(acc - avg_conf) * len(b) / total
    return err


# --- loading -----------------------------------------------------------------


def load_rows(path: str | Path, default_repo: str | None = None) -> list[dict]:
    text = Path(path).read_text()
    stripped = text.lstrip()
    if stripped.startswith("["):
        rows = json.loads(text)
    else:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    for r in rows:
        if "repo" not in r:
            if default_repo is None:
                raise ValueError(f"{path}: row without 'repo' and no --repo given: {r}")
            r["repo"] = default_repo
    return rows


def parse_label(value) -> bool | None:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None  # unsure / unknown verdicts are ignored


# --- scoring -----------------------------------------------------------------


@dataclass
class Finding:
    repo: str
    family: str
    key: str
    tier: str | None = None
    confidence: float | None = None
    correct: bool | None = None  # None = unlabelled


@dataclass
class Group:
    repo: str
    family: str
    tier: str
    findings: int
    judged: int
    tp: int
    precision: float | None
    ci_low: float | None
    ci_high: float | None
    recall: float | None
    truth: int | None
    ece: float | None


@dataclass
class Scored:
    findings: list[Finding]
    truth: dict[tuple[str, str], set[str]]  # (repo, family) -> keys
    metrics: dict[tuple[str, str], dict[str, float]] = field(default_factory=dict)


def judge(findings: list[dict], truth: list[dict], labels: list[dict], metrics=()) -> Scored:
    truth_sets: dict[tuple[str, str], set[str]] = defaultdict(set)
    for t in truth:
        truth_sets[(t["repo"], t["family"])].add(str(t["key"]))
    by_key: dict[tuple, bool] = {}
    for lab in labels:
        verdict = parse_label(lab["label"])
        if verdict is not None:
            by_key[(lab["repo"], lab.get("family"), str(lab["finding_key"]))] = verdict
    out = []
    for f in findings:
        repo, fam, key = f["repo"], f["family"], str(f["key"])
        correct = by_key.get((repo, fam, key), by_key.get((repo, None, key)))
        if correct is None and (repo, fam) in truth_sets:
            correct = key in truth_sets[(repo, fam)]
        conf = f.get("confidence")
        out.append(
            Finding(repo, fam, key, f.get("tier"), None if conf is None else float(conf), correct)
        )
    met: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for m in metrics:
        met[(m["repo"], m["family"])][m["metric"]] = float(m["value"])
    return Scored(out, dict(truth_sets), dict(met))


def summarize(
    rows: list[Finding],
    truth: set[str] | None,
    *,
    repo: str,
    family: str,
    tier: str,
    bins: int = 10,
) -> Group:
    judged = [f for f in rows if f.correct is not None]
    tp = sum(f.correct for f in judged)
    ci = wilson_ci(tp, len(judged))
    recall = None
    if truth:
        found = {f.key for f in rows}
        recall = len(found & truth) / len(truth)
    conf_pairs = [(f.confidence, f.correct) for f in judged if f.confidence is not None]
    return Group(
        repo=repo,
        family=family,
        tier=tier,
        findings=len(rows),
        judged=len(judged),
        tp=tp,
        precision=tp / len(judged) if judged else None,
        ci_low=ci[0] if ci else None,
        ci_high=ci[1] if ci else None,
        recall=recall,
        truth=len(truth) if truth is not None else None,
        ece=ece(conf_pairs, bins),
    )


def _pooled_truth(scored: Scored, family: str, repo: str) -> set[str] | None:
    keys = [
        {f"{r}\0{k}" for k in ks}
        for (r, fam), ks in scored.truth.items()
        if fam == family and repo in (ALL, r)
    ]
    return set().union(*keys) if keys else None


def _keyed(rows: list[Finding], repo: str) -> list[Finding]:
    # Pooled recall compares against repo-qualified truth keys.
    if repo != ALL:
        return rows
    return [
        Finding(f.repo, f.family, f"{f.repo}\0{f.key}", f.tier, f.confidence, f.correct)
        for f in rows
    ]


def group_table(scored: Scored, bins: int = 10) -> list[Group]:
    """Every repo x family x tier cell, plus all-tier and pooled-repo rollups.

    Recall is reported only on all-tier cells: truth sets are per family.
    """
    cells: dict[tuple[str, str, str], list[Finding]] = defaultdict(list)
    for f in scored.findings:
        tiers = [ALL] + ([f.tier] if f.tier else [])
        for repo in (f.repo, ALL):
            for tier in tiers:
                cells[(repo, f.family, tier)].append(f)
    for repo, fam in scored.truth:  # families with truth but zero findings
        for r in (repo, ALL):
            cells.setdefault((r, fam, ALL), [])
    out = []
    for (repo, fam, tier), rows in sorted(cells.items()):
        truth = None
        if tier == ALL:
            truth = (
                scored.truth.get((repo, fam)) if repo != ALL else _pooled_truth(scored, fam, ALL)
            )
        out.append(
            summarize(_keyed(rows, repo), truth, repo=repo, family=fam, tier=tier, bins=bins)
        )
    return out


# --- thresholds --------------------------------------------------------------


def load_thresholds(path: str | Path = DEFAULT_THRESHOLDS) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _select(scored: Scored, name: str, spec: dict) -> tuple[str, str | None, list[Finding]]:
    """Resolve a threshold name to (family, tier, findings).

    ``name`` matches a family exactly, or ``<family>.<tier>``.
    """
    fams = {f.family for f in scored.findings} | {fam for _, fam in scored.truth}
    fams |= {fam for _, fam in scored.metrics}
    if name in fams or "." not in name:
        family, tier = name, None
    else:
        family, tier = name.rsplit(".", 1)
    rows = [f for f in scored.findings if f.family == family and (tier is None or f.tier == tier)]
    if "min_confidence" in spec:
        rows = [
            f for f in rows if f.confidence is not None and f.confidence >= spec["min_confidence"]
        ]
    return family, tier, rows


@dataclass
class Check:
    threshold: str
    repo: str
    metric: str
    value: float | None
    bound: str
    status: str  # pass | fail | unmeasured
    note: str = ""


def _bound(threshold, repo, metric, value, lo=None, hi=None, note="") -> Check:
    bound = f">= {lo}" if lo is not None else f"<= {hi}"
    if value is None:
        return Check(threshold, repo, metric, None, bound, "unmeasured", note)
    ok = (lo is None or value >= lo) and (hi is None or value <= hi)
    return Check(threshold, repo, metric, round(value, 4), bound, "pass" if ok else "fail", note)


def evaluate(scored: Scored, thresholds: dict) -> list[Check]:
    """Apply every ``[family.X]`` threshold to the families present in the input.

    Pooled checks: precision, precision_ci_low, min_n, recall, min_findings,
    ece_max (``[calibration]`` default), metrics. Per-repo checks:
    precision_per_repo, recall_per_repo, metrics. ``per_tier = true``
    applies the precision floor to every tier of the family separately.
    Families absent from all inputs are skipped, so a partial run scores
    only what it measured; a family that has truth or labels but zero
    findings still trips ``min_findings`` (wholesale deletion is not a fix).
    """
    bins = thresholds.get("calibration", {}).get("bins", 10)
    default_ece = thresholds.get("calibration", {}).get("ece_max")
    present = {f.family for f in scored.findings} | {fam for _, fam in scored.truth}
    present |= {fam for _, fam in scored.metrics}
    checks: list[Check] = []
    for name, spec in thresholds.get("family", {}).items():
        family, tier, rows = _select(scored, name, spec)
        if family not in present:
            continue
        repos = sorted({f.repo for f in rows} | {r for r, fam in scored.truth if fam == family})
        repos += sorted({r for r, fam in scored.metrics if fam == family} - set(repos))
        truth = None if tier else _pooled_truth(scored, family, ALL)
        pooled = summarize(
            _keyed(rows, ALL), truth, repo=ALL, family=family, tier=tier or ALL, bins=bins
        )
        subgroups = [pooled]
        if spec.get("per_tier"):
            tiers = sorted({f.tier for f in rows if f.tier})
            subgroups = [
                summarize([f for f in rows if f.tier == t], None, repo=ALL, family=family, tier=t)
                for t in tiers
            ] or [pooled]
        for g in subgroups:
            label = name if g.tier in (ALL, tier) else f"{name}.{g.tier}"
            if "precision" in spec:
                checks.append(_bound(label, ALL, "precision", g.precision, lo=spec["precision"]))
            if "precision_ci_low" in spec:
                checks.append(
                    _bound(label, ALL, "precision_ci_low", g.ci_low, lo=spec["precision_ci_low"])
                )
            if "min_n" in spec:
                checks.append(_bound(label, ALL, "judged_n", g.judged, lo=spec["min_n"]))
        if "recall" in spec:
            checks.append(
                _bound(name, ALL, "recall", pooled.recall, lo=spec["recall"], note="needs truth")
            )
        if "min_findings" in spec:
            checks.append(_bound(name, ALL, "findings", pooled.findings, lo=spec["min_findings"]))
        ece_max = spec.get("ece_max", default_ece)
        if ece_max is not None and pooled.ece is not None:
            checks.append(_bound(name, ALL, "ece", pooled.ece, hi=ece_max))
        for repo in repos:
            rrows = [f for f in rows if f.repo == repo]
            rtruth = None if tier else scored.truth.get((repo, family))
            g = summarize(rrows, rtruth, repo=repo, family=family, tier=tier or ALL, bins=bins)
            if "precision_per_repo" in spec:
                checks.append(
                    _bound(name, repo, "precision", g.precision, lo=spec["precision_per_repo"])
                )
            if "recall_per_repo" in spec:
                checks.append(_bound(name, repo, "recall", g.recall, lo=spec["recall_per_repo"]))
            for metric, b in spec.get("metrics", {}).items():
                value = scored.metrics.get((repo, family), {}).get(metric)
                checks.append(_bound(name, repo, metric, value, lo=b.get("min"), hi=b.get("max")))
    return checks


# --- output ------------------------------------------------------------------


def _fmt(v, pct=True) -> str:
    if v is None:
        return "-"
    return f"{v * 100:.1f}%" if pct else f"{v:.3f}"


def to_markdown(groups: list[Group], checks: list[Check]) -> str:
    lines = [
        "| repo | family | tier | findings | judged | precision | 95% CI | recall | ECE |",
        "|---|---|---|---:|---:|---:|---|---:|---:|",
    ]
    for g in groups:
        ci = f"{_fmt(g.ci_low)}-{_fmt(g.ci_high)}" if g.ci_low is not None else "-"
        rec = f"{_fmt(g.recall)} of {g.truth}" if g.recall is not None else "-"
        lines.append(
            f"| {g.repo} | {g.family} | {g.tier} | {g.findings} | {g.judged} | "
            f"{_fmt(g.precision)} | {ci} | {rec} | {_fmt(g.ece, pct=False)} |"
        )
    if checks:
        lines += [
            "",
            "| threshold | repo | metric | value | bound | status |",
            "|---|---|---|---:|---|---|",
        ]
        for c in checks:
            val = "-" if c.value is None else f"{c.value:g}"
            lines.append(
                f"| {c.threshold} | {c.repo} | {c.metric} | {val} | {c.bound} | {c.status} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Score findings against ground truth and labels.",
        epilog="Row formats are documented at the top of this file.",
    )
    ap.add_argument(
        "--findings", action="append", default=[], help="findings JSON/JSONL (repeatable)"
    )
    ap.add_argument(
        "--truth", action="append", default=[], help="ground-truth JSON/JSONL (repeatable)"
    )
    ap.add_argument("--labels", action="append", default=[], help="labels JSON/JSONL (repeatable)")
    ap.add_argument(
        "--metrics", action="append", default=[], help="oracle metrics JSON/JSONL (repeatable)"
    )
    ap.add_argument("--repo", help="default repo for rows without one")
    ap.add_argument("--thresholds", default=str(DEFAULT_THRESHOLDS), help="thresholds TOML")
    ap.add_argument("--json", dest="json_out", help="write JSON report here")
    ap.add_argument("--md", dest="md_out", help="write markdown report here (default: stdout)")
    args = ap.parse_args(argv)

    def rows(paths):
        return [r for p in paths for r in load_rows(p, args.repo)]

    scored = judge(rows(args.findings), rows(args.truth), rows(args.labels), rows(args.metrics))
    thresholds = load_thresholds(args.thresholds)
    groups = group_table(scored, thresholds.get("calibration", {}).get("bins", 10))
    checks = evaluate(scored, thresholds)
    passed = not any(c.status == "fail" for c in checks)
    md = to_markdown(groups, checks)
    if args.md_out:
        Path(args.md_out).write_text(md)
    else:
        sys.stdout.write(md)
    if args.json_out:
        report = {
            "passed": passed,
            "groups": [asdict(g) for g in groups],
            "checks": [asdict(c) for c in checks],
        }
        Path(args.json_out).write_text(json.dumps(report, indent=2) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
