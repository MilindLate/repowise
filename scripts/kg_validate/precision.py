"""Precision-run bookkeeping for ``run.py --precision``: split selection,
per-repo results -> score.py rows, before/after compare, regression checks.

A per-repo *result* is the compact JSON committed as
``precision_baselines/<repo>.json``::

    {"repo": "click", "sha": "...", "split": "dev",
     "families": {
       "imports": {"python": {"tp": 1, "fp": 0, "fn": 2, "dst_not_indexed": 0}},
       "entry_points": {"p_at_5": 0.8, "recall": 0.5, ...},
       "identity": {"b3_precision": 1.0, "b3_recall": 0.9, "person_count_error": 0.1, ...}}}

Imports keep raw per-language counts rather than rates, so pooled floors
(``score.evaluate``) can be recomputed for any repo subset from baselines
alone, and a baseline's floor failures can be told apart from new ones.
"""

from __future__ import annotations

from dataclasses import dataclass

import score

FAMILIES = ("imports", "entry_points", "identity")
SPLITS = ("dev", "heldout", "all")
# A rate that moves the wrong way by more than this is a regression (R2).
REGRESSION_PP = 0.02
# Oracle metrics compared per family; every other metric is higher-is-better.
METRICS = {
    "entry_points": ("p_at_5", "recall"),
    "identity": ("b3_precision", "b3_recall", "person_count_error"),
}
LOWER_IS_BETTER = frozenset({"person_count_error"})
HELDOUT = "heldout"  # pseudo-repo name for the held-out aggregate


def select_repos(matrix: dict, split: str, repos: list[str] | None = None) -> list[str]:
    """Matrix entries in ``split`` (``all`` = both), optionally narrowed to ``repos``."""
    names = [n for n, spec in matrix.items() if split == "all" or spec.get("split") == split]
    if repos:
        missing = [r for r in repos if r not in names]
        if missing:
            raise ValueError(f"not in the {split} split of matrix.toml: {missing}")
        names = [n for n in names if n in repos]
    return names


def import_counts(compare_result: dict) -> dict:
    """Per-language counts from ``oracles.imports.compare``."""
    keys = ("tp", "fp", "fn", "dst_not_indexed")
    return {lang: {k: r[k] for k in keys} for lang, r in compare_result.items()}


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def family_metrics(result: dict) -> dict[str, dict[str, float | None]]:
    """``{family key: {metric: value}}`` for the compare table.

    Imports report pooled ``imports`` plus one ``imports.<lang>`` per language.
    """
    fams = result.get("families", {})
    out: dict[str, dict[str, float | None]] = {}
    langs = fams.get("imports")
    if langs is not None:
        per = {"imports": {"tp": 0, "fp": 0, "fn": 0}}
        for lang, c in sorted(langs.items()):
            per[f"imports.{lang}"] = c
            for k in ("tp", "fp", "fn"):
                per["imports"][k] += c[k]
        for key, c in per.items():
            out[key] = {
                "precision": _rate(c["tp"], c["tp"] + c["fp"]),
                "recall": _rate(c["tp"], c["tp"] + c["fn"]),
            }
    for fam, names in METRICS.items():
        if fam in fams:
            out[fam] = {m: fams[fam].get(m) for m in names}
    return out


def score_inputs(results: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """(findings, truth, metrics) rows for ``score.judge`` from results.

    Import counts expand to synthetic keyed rows: ``tp`` findings in truth,
    ``fp`` findings outside it, ``fn`` truth never found. Each language is
    emitted under family ``imports`` (tier = language) and again as family
    ``imports.<lang>``, so per-language floors get their own recall.
    """
    findings: list[dict] = []
    truth: list[dict] = []
    metrics: list[dict] = []
    for res in results:
        repo = res["repo"]
        fams = res.get("families", {})
        for lang, c in fams.get("imports", {}).items():
            for family, tier in (("imports", lang), (f"imports.{lang}", None)):
                for kind in ("tp", "fp", "fn"):
                    for i in range(c[kind]):
                        key = f"{lang}:{kind}:{i}"
                        if kind != "fn":
                            findings.append(
                                {"repo": repo, "family": family, "key": key, "tier": tier}
                            )
                        if kind != "fp":
                            truth.append({"repo": repo, "family": family, "key": key})
        for fam, names in METRICS.items():
            for m in names:
                value = fams.get(fam, {}).get(m)
                if value is not None:
                    metrics.append({"repo": repo, "family": fam, "metric": m, "value": value})
    return findings, truth, metrics


def evaluate_results(results: list[dict], thresholds: dict) -> list[score.Check]:
    findings, truth, metrics = score_inputs(results)
    return score.evaluate(score.judge(findings, truth, [], metrics), thresholds)


def check_key(c: score.Check) -> tuple[str, str, str]:
    return (c.threshold, c.repo, c.metric)


def new_failures(now: list[score.Check], before: list[score.Check]) -> list[score.Check]:
    """Checks failing now that did not already fail on the baseline."""
    known = {check_key(c) for c in before if c.status == "fail"}
    return [c for c in now if c.status == "fail" and check_key(c) not in known]


def aggregate(results: list[dict], name: str = HELDOUT) -> dict:
    """One pseudo-repo result: import counts summed, oracle metrics averaged.

    Used for the held-out split, which must never be reported repo by repo.
    """
    fams: dict = {}
    for res in results:
        for lang, c in res.get("families", {}).get("imports", {}).items():
            acc = fams.setdefault("imports", {}).setdefault(lang, dict.fromkeys(c, 0))
            for k, v in c.items():
                acc[k] += v
    for fam, names in METRICS.items():
        vals = {m: [] for m in names}
        for res in results:
            for m in names:
                v = res.get("families", {}).get(fam, {}).get(m)
                if v is not None:
                    vals[m].append(v)
        if any(vals.values()):
            fams[fam] = {m: (sum(v) / len(v) if v else None) for m, v in vals.items()}
    return {"repo": name, "repos": len(results), "families": fams}


@dataclass
class Row:
    repo: str
    family: str
    metric: str
    baseline: float | None
    now: float | None

    @property
    def delta(self) -> float | None:
        if self.baseline is None or self.now is None:
            return None
        return self.now - self.baseline

    @property
    def regressed(self) -> bool:
        d = self.delta
        if d is None:
            return False
        worse = d if self.metric in LOWER_IS_BETTER else -d
        return worse > REGRESSION_PP + 1e-9


def compare(current: list[dict], baselines: dict[str, dict]) -> list[Row]:
    """One row per repo x family x metric measured now or in the baseline."""
    rows = []
    for res in current:
        now = family_metrics(res)
        base = family_metrics(baselines.get(res["repo"], {}))
        for fam in sorted(now.keys() | base.keys()):
            for metric in sorted(now.get(fam, {}).keys() | base.get(fam, {}).keys()):
                row = Row(
                    res["repo"],
                    fam,
                    metric,
                    base.get(fam, {}).get(metric),
                    now.get(fam, {}).get(metric),
                )
                if row.baseline is not None or row.now is not None:
                    rows.append(row)
    return rows


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def render_compare(rows: list[Row]) -> str:
    lines = [
        "| repo | family | metric | baseline | now | delta | |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for r in rows:
        d = r.delta
        delta = "-" if d is None else f"{d * 100:+.1f}pp"
        flag = "REGRESSED" if r.regressed else ("new" if r.baseline is None else "")
        lines.append(
            f"| {r.repo} | {r.family} | {r.metric} | {_pct(r.baseline)} | {_pct(r.now)} "
            f"| {delta} | {flag} |"
        )
    return "\n".join(lines) + "\n"


def render_checks(checks: list[score.Check], new: list[score.Check]) -> str:
    """Failing floors only; ``new`` ones (not failing on the baseline) are marked."""
    new_keys = {check_key(c) for c in new}
    failing = [c for c in checks if c.status == "fail"]
    if not failing:
        return "All measured floors pass.\n"
    lines = [
        "| threshold | repo | metric | value | bound | |",
        "|---|---|---|---:|---|---|",
    ]
    for c in failing:
        flag = "NEW" if check_key(c) in new_keys else "known"
        lines.append(
            f"| {c.threshold} | {c.repo} | {c.metric} | {c.value:g} | {c.bound} | {flag} |"
        )
    return "\n".join(lines) + "\n"
