"""Precision-run bookkeeping for ``run.py --precision``: split selection,
per-repo results -> score.py rows, before/after compare, regression checks.

A per-repo *result* is the compact JSON committed as
``precision_baselines/<repo>.json``::

    {"repo": "click", "sha": "...", "split": "dev",
     "families": {
       "imports": {"python": {"tp": 1, "fp": 0, "fn": 2, "dst_not_indexed": 0}},
       "entry_points": {"p_at_5": 0.8, "recall": 0.5, ...},
       "identity": {"b3_precision": 1.0, "b3_recall": 0.9, "person_count_error": 0.1, ...},
       "packages": {"precision": 0.97, "recall": 1.0, "declared": 85, ...},
       "calls": {"claims": {"0.95": {"tp": 3, "fp": 0, "unknown": 1}},
                 "sites": {"hit": {"0.9": 5}, "fn": 2, "unknown": 1, ...}, ...},
       "dead_code": {"tiers": {"safe_to_delete": {"findings": 12, "tp": 3, "fp": 1,
                                                  "by_source": {"oracle": {"tp": 2, "fp": 0}, ...}}}},
       "perf": {"tiers": {...}, "n_plus_one": {"findings": 4, "tp": 0, "fp": 2, ...}}}}

Imports and calls keep raw counts rather than rates, so pooled floors
(``score.evaluate``) can be recomputed for any repo subset from baselines
alone, and a baseline's floor failures can be told apart from new ones. The
labelled families (dead_code, health, perf) keep per-tier counts the same way:
``findings`` emitted, ``tp``/``fp`` judged by labels or the mention oracle,
the rest unlabelled (see labels.py and LABELING.md).
"""

from __future__ import annotations

from dataclasses import dataclass

import labels as label_store
import score
from oracles.calls import HIGH as CALLS_HIGH

# Mechanical oracles run by default. calls needs jedi (requirements-oracles.txt)
# and a LanguageService pass per repo, and the labelled families need labels
# for their floors, so those run only when --families names them.
DEFAULT_FAMILIES = ("imports", "entry_points", "identity", "packages")
LABELLED_FAMILIES = ("dead_code", "health", "perf")
FAMILIES = (*DEFAULT_FAMILIES, "calls", *LABELLED_FAMILIES)
SPLITS = ("dev", "heldout", "all")
# A rate that moves the wrong way by more than this is a regression (R2).
REGRESSION_PP = 0.02
# Oracle metrics compared per family; every other metric is higher-is-better.
METRICS = {
    "entry_points": ("p_at_5", "recall"),
    "identity": ("b3_precision", "b3_recall", "person_count_error"),
    "packages": ("precision", "recall"),
}
LOWER_IS_BETTER = frozenset({"person_count_error", "unknown_rate"})
HELDOUT = "heldout"  # pseudo-repo name for the held-out aggregate


def select_repos(
    matrix: dict, split: str, repos: list[str] | None = None, ci: str | None = None
) -> list[str]:
    """Matrix entries in ``split`` (``all`` = both), optionally narrowed to ``repos``
    and to one ``ci`` tier (``fast`` = the per-PR job's set)."""
    names = [
        n
        for n, spec in matrix.items()
        if (split == "all" or spec.get("split") == split) and (ci is None or spec.get("ci") == ci)
    ]
    if repos:
        missing = [r for r in repos if r not in names]
        if missing:
            raise ValueError(f"not in the {split} split (ci={ci}) of matrix.toml: {missing}")
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
    Calls report precision at confidence >= ``CALLS_HIGH``, caller recall at
    any confidence, and the share of sampled items the oracle could not judge.
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
    calls = fams.get("calls", {})
    claims, sites = calls.get("claims", {}), calls.get("sites", {})
    if claims or sites:
        high = [c for conf, c in claims.items() if float(conf) >= CALLS_HIGH]
        tp, fp = sum(c["tp"] for c in high), sum(c["fp"] for c in high)
        hits = sum(sites.get("hit", {}).values())
        judged = sum(sum(c.values()) for c in claims.values()) + hits
        judged += sum(v for k, v in sites.items() if k != "hit")
        unknown = sum(c["unknown"] for c in claims.values()) + sites.get("unknown", 0)
        out["calls"] = {
            "precision": _rate(tp, tp + fp),
            "recall": _rate(hits, hits + sites.get("fn", 0)),
            "unknown_rate": _rate(unknown, judged),
        }
    for view, cells in labelled_cells(result).items():
        tp = sum(c["tp"] for c in cells.values())
        fp = sum(c["fp"] for c in cells.values())
        out[view] = {"precision": _rate(tp, tp + fp)}
        for tier, c in cells.items():
            if tier != ALL_TIERS:
                out[f"{view}.{tier}"] = {"precision": _rate(c["tp"], c["tp"] + c["fp"])}
    return out


ALL_TIERS = "*"  # the single cell of a view with no tiers (perf_n_plus_one)


def labelled_cells(result: dict) -> dict[str, dict[str, dict]]:
    """``{view: {tier: counts}}`` for the labelled families in one result.

    ``perf`` carries a second view, ``perf_n_plus_one``: the findings whose
    text says "N+1", judged on whether it is one.
    """
    fams = result.get("families", {})
    out = {}
    for fam in LABELLED_FAMILIES:
        if fam in fams:
            out[fam] = fams[fam].get("tiers", {})
            if "n_plus_one" in fams[fam]:
                out[f"{fam}_n_plus_one"] = {ALL_TIERS: fams[fam]["n_plus_one"]}
    return out


def score_inputs(results: list[dict]) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """(findings, truth, labels, metrics) rows for ``score.judge`` from results.

    Import counts expand to synthetic keyed rows: ``tp`` findings in truth,
    ``fp`` findings outside it, ``fn`` truth never found. Each language is
    emitted under family ``imports`` (tier = language) and again as family
    ``imports.<lang>``, so per-language floors get their own recall.

    Calls come from two samples, so they get two families: ``calls`` holds
    the sampled claims, labelled tp/fp (``unknown`` stays unlabelled) with
    their confidence and tier ``high``/``low`` around ``CALLS_HIGH``;
    ``calls.callers`` holds the recall sample as truth, hits as findings.
    Labelled-family counts expand to one finding row per finding and one
    label row per judged finding; the rest stay unlabelled.
    """
    findings: list[dict] = []
    truth: list[dict] = []
    labels: list[dict] = []
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
        calls = fams.get("calls", {})
        for conf, c in calls.get("claims", {}).items():
            tier = "high" if float(conf) >= CALLS_HIGH else "low"
            for kind in ("tp", "fp", "unknown"):
                for i in range(c[kind]):
                    key = f"{conf}:{kind}:{i}"
                    findings.append(
                        {
                            "repo": repo,
                            "family": "calls",
                            "key": key,
                            "tier": tier,
                            "confidence": float(conf),
                        }
                    )
                    if kind != "unknown":
                        labels.append(
                            {"repo": repo, "family": "calls", "finding_key": key, "label": kind}
                        )
        sites = calls.get("sites", {})
        for kind, n in (("hit", sum(sites.get("hit", {}).values())), ("fn", sites.get("fn", 0))):
            for i in range(n):
                row = {"repo": repo, "family": "calls.callers", "key": f"{kind}:{i}"}
                truth.append(row)
                if kind == "hit":
                    findings.append(row)
        for fam, names in METRICS.items():
            for m in names:
                value = fams.get(fam, {}).get(m)
                if value is not None:
                    metrics.append({"repo": repo, "family": fam, "metric": m, "value": value})
        for view, cells in labelled_cells(res).items():
            for tier, c in cells.items():
                for i in range(c["findings"]):
                    key = f"{tier}:{i}"
                    t = None if tier == ALL_TIERS else tier
                    findings.append({"repo": repo, "family": view, "key": key, "tier": t})
                    if i < c["tp"] + c["fp"]:
                        verdict = "tp" if i < c["tp"] else "fp"
                        labels.append(
                            {"repo": repo, "family": view, "finding_key": key, "label": verdict}
                        )
    return findings, truth, labels, metrics


def evaluate_results(results: list[dict], thresholds: dict) -> list[score.Check]:
    findings, truth, labels, metrics = score_inputs(results)
    return score.evaluate(score.judge(findings, truth, labels, metrics), thresholds)


def label_debt(results: list[dict], thresholds: dict) -> list[dict]:
    """Gated cells whose judged sample is below the protocol target.

    A cell is gated when a floor applies to it: ``[family."<view>.<tier>"]``,
    ``[family.<view>]`` with ``per_tier``, or ``[family.<view>]`` for a view
    without tiers. Its findings are then scored only on the judged sample,
    so an unlabelled gated cell is precision nobody has measured.
    """
    floors = thresholds.get("family", {})
    out = []
    for res in results:
        for view, cells in labelled_cells(res).items():
            for tier, c in cells.items():
                gated = (
                    f"{view}.{tier}" in floors
                    or floors.get(view, {}).get("per_tier")
                    or (tier == ALL_TIERS and view in floors)
                )
                missing = label_store.debt(c)
                if gated and missing:
                    out.append(
                        {
                            "repo": res["repo"],
                            "cell": view if tier == ALL_TIERS else f"{view}.{tier}",
                            "findings": c["findings"],
                            "judged": c["tp"] + c["fp"],
                            "missing": missing,
                        }
                    )
    return out


def render_debt(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = [
        f"LABEL DEBT: {len(rows)} gated cell(s) below the LABELING.md sample target "
        "(precision there is unmeasured, not good):",
        "| repo | cell | findings | judged | labels missing |",
        "|---|---|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['repo']} | {r['cell']} | {r['findings']} | {r['judged']} | {r['missing']} |"
        )
    return "\n".join(lines) + "\n"


def check_key(c: score.Check) -> tuple[str, str, str]:
    return (c.threshold, c.repo, c.metric)


def new_failures(now: list[score.Check], before: list[score.Check]) -> list[score.Check]:
    """Checks failing now that did not already fail on the baseline."""
    known = {check_key(c) for c in before if c.status == "fail"}
    return [c for c in now if c.status == "fail" and check_key(c) not in known]


def _add_counts(acc: dict, counts: dict) -> None:
    """Sum nested ``{key: int | {key: ...}}`` count dicts into ``acc``."""
    for k, v in counts.items():
        if isinstance(v, dict):
            _add_counts(acc.setdefault(k, {}), v)
        else:
            acc[k] = acc.get(k, 0) + v


def aggregate(results: list[dict], name: str = HELDOUT) -> dict:
    """One pseudo-repo result: import and call counts summed, oracle metrics averaged.

    Used for the held-out split, which must never be reported repo by repo.
    """
    fams: dict = {}
    for res in results:
        for lang, c in res.get("families", {}).get("imports", {}).items():
            acc = fams.setdefault("imports", {}).setdefault(lang, dict.fromkeys(c, 0))
            for k, v in c.items():
                acc[k] += v
        calls = res.get("families", {}).get("calls")
        if calls:
            counts = {k: calls[k] for k in ("claims", "sites", "no_site") if k in calls}
            _add_counts(fams.setdefault("calls", {}), counts)
    for fam, names in METRICS.items():
        vals = {m: [] for m in names}
        for res in results:
            for m in names:
                v = res.get("families", {}).get(fam, {}).get(m)
                if v is not None:
                    vals[m].append(v)
        if any(vals.values()):
            fams[fam] = {m: (sum(v) / len(v) if v else None) for m, v in vals.items()}
    for res in results:
        for fam in LABELLED_FAMILIES:
            src = res.get("families", {}).get(fam)
            if src is None:
                continue
            acc = fams.setdefault(fam, {"tiers": {}})
            cells = [
                (acc["tiers"].setdefault(t, _empty_cell()), c) for t, c in src["tiers"].items()
            ]
            if "n_plus_one" in src:
                cells.append((acc.setdefault("n_plus_one", _empty_cell()), src["n_plus_one"]))
            for into, c in cells:
                for k in ("findings", "tp", "fp"):
                    into[k] += c[k]
    return {"repo": name, "repos": len(results), "families": fams}


def _empty_cell() -> dict:
    return {"findings": 0, "tp": 0, "fp": 0}


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
