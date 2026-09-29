#!/usr/bin/env python3
"""Finding labels for the labelled precision families (dead_code, health, perf).

See LABELING.md for the protocol. This module holds everything the labels
need that is not an oracle:

- ``finding_key``: the hosted public identity of a finding, reimplemented from
  hosted ``modal_app/indexer/finding_identity.py`` (the hosted repo is never
  imported). Dead-code findings key without a line span, because the hosted
  dead-code artifact carries none and hosted derives the id at serve time
  from ``file_path::kind::symbol_name::0::0``.
- loading ``labels/<repo>/<family>.jsonl`` and resolving one verdict per key
  (a human label, else - only with ``include_suggested`` - a
  ``claude-suggested`` one, else the mention oracle's auto-TP);
- counting findings per tier by verdict source, and "label debt" (a gated
  tier whose judged sample is below the protocol's per-cell target);
- Cohen's kappa over double-labelled keys;
- the queue sampler, which appends unlabelled rows to the label files.

Usage:
    python scripts/kg_validate/labels.py queue <checkout> --repo click --family dead_code
    python scripts/kg_validate/labels.py summary [--repo click]
    python scripts/kg_validate/labels.py kappa --repo click --family dead_code
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABELS_DIR = HERE / "labels"
FAMILIES = ("dead_code", "health", "perf")
SUGGESTED = "claude-suggested"
ORACLE = "oracle"
# Protocol targets (LABELING.md): findings sampled per cell, oracle
# auto-TPs spot-checked per cell, share double-labelled, kappa floor.
SAMPLE_PER_CELL = 40
ORACLE_SPOT_CHECK = 10
DOUBLE_LABEL_SHARE = 0.2
KAPPA_FLOOR = 0.7

LABELS = {
    "dead_code": {"TP", "FP", "unsure"},
    "health": {"TP", "FP", "unsure"},
    "perf": {"true_n_plus_1", "io_in_loop_inherent", "actionable", "FP", "unsure"},
}
REASONS = {
    "dead_code": {
        "used_in_file",
        "used_elsewhere",
        "framework_loaded",
        "build_input",
        "public_api",
        "decorator_registered",
        "test_only",
        "config_file",
        "truly_dead",
    },
    "health": {"accurate", "wrong_location", "wrong_number", "wrong_text", "not_a_smell"},
}
# Perf findings are worth acting on when batching or restructuring helps.
_PERF_TP = {"true_n_plus_1", "actionable"}


# --- identity ----------------------------------------------------------------


def finding_key(
    file_path: str,
    kind: str,
    symbol: str | None,
    line_start: int | None,
    line_end: int | None,
) -> str:
    """Hosted ``stable_finding_id``: sha1 of the coordinates, 24 hex chars."""
    seed = f"{file_path}::{kind}::{symbol or ''}::{line_start or 0}::{line_end or 0}"
    return hashlib.sha1(seed.encode()).hexdigest()[:24]


def key_for(family: str, row: dict) -> str:
    if family == "dead_code":
        return finding_key(row["file_path"], row["kind"], row.get("symbol_name"), None, None)
    return finding_key(
        row["file_path"],
        row["biomarker_type"],
        row.get("function_name"),
        row.get("line_start"),
        row.get("line_end"),
    )


# --- findings from an index --------------------------------------------------


def dead_code_tier(confidence: float, file_path: str, stored_safe: bool) -> str:
    """The engine's presentation tier: ``safe_to_delete`` (deletion-ready),
    ``high`` (>= the safe threshold but carrying a risk), ``review`` (>= the
    default ``min_confidence`` floor), else ``low`` (hidden by default)."""
    from repowise.core.analysis.dead_code.risk_factors import (
        RISK_CAP_CONFIDENCE,
        SAFE_CONFIDENCE_THRESHOLD,
        effective_safe_to_delete,
    )

    if effective_safe_to_delete(confidence, file_path, bool(stored_safe)):
        return "safe_to_delete"
    if confidence >= SAFE_CONFIDENCE_THRESHOLD:
        return "high"
    return "review" if confidence >= RISK_CAP_CONFIDENCE else "low"


def index_findings(db_path: Path, family: str) -> list[dict]:
    """The family's findings from an indexed checkout's ``wiki.db``, each with
    ``key`` and ``tier`` (dead code: engine tier; health/perf: biomarker type)."""
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        if family == "dead_code":
            rows = db.execute(
                "SELECT kind, file_path, symbol_name, symbol_kind, confidence, "
                "safe_to_delete, start_line, end_line, reason FROM dead_code_findings"
            ).fetchall()
        else:
            op = "=" if family == "perf" else "!="
            rows = db.execute(
                "SELECT biomarker_type, file_path, function_name, line_start, line_end, "
                "severity, COALESCE(dimension, 'defect') AS dimension, reason, details_json "
                f"FROM health_findings WHERE COALESCE(dimension, 'defect') {op} 'performance'"
            ).fetchall()
    out = []
    for r in rows:
        f = dict(r)
        if family == "dead_code":
            f["tier"] = dead_code_tier(f["confidence"], f["file_path"], f["safe_to_delete"])
        else:
            f["tier"] = f["biomarker_type"]
            f["details"] = json.loads(f.pop("details_json") or "{}")
        f["key"] = key_for(family, f)
        out.append(f)
    return sorted(out, key=lambda f: (f["tier"], f["file_path"], f["key"]))


# --- label files -------------------------------------------------------------


def labels_path(repo: str, family: str, labels_dir: Path = LABELS_DIR) -> Path:
    return Path(labels_dir) / repo / f"{family}.jsonl"


def load_labels(repo: str, family: str, labels_dir: Path = LABELS_DIR) -> list[dict]:
    path = labels_path(repo, family, labels_dir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def validate(rows: list[dict], family: str) -> list[str]:
    """Problems in a label file: unknown labels or reason codes."""
    problems = []
    for i, r in enumerate(rows, 1):
        label = r.get("label")
        if label is None:
            continue
        if label not in LABELS[family]:
            problems.append(f"row {i}: label {label!r} not in {sorted(LABELS[family])}")
        reason = r.get("reason")
        if family in REASONS and reason and reason not in REASONS[family]:
            problems.append(f"row {i}: reason {reason!r} not in {sorted(REASONS[family])}")
    return problems


def is_human(row: dict) -> bool:
    return row.get("labeler") not in (None, "", SUGGESTED, ORACLE)


def resolve(rows: list[dict], include_suggested: bool = False) -> dict[str, dict]:
    """One verdict row per key: the last human row, else (opt-in) the last
    ``claude-suggested`` row. Queue rows (no label) and ``unsure`` are kept
    out by ``verdict``, not here, so an ``unsure`` still shadows a suggestion."""
    human: dict[str, dict] = {}
    suggested: dict[str, dict] = {}
    for r in rows:
        if not r.get("label"):
            continue
        if is_human(r):
            human[r["finding_key"]] = r
        elif r.get("labeler") == SUGGESTED:
            suggested[r["finding_key"]] = r
    return {**(suggested if include_suggested else {}), **human}


def verdict(view: str, label: str | None) -> bool | None:
    """True/False for a label in one scored view, None when it does not judge.

    Views: ``dead_code``/``health`` (TP/FP), ``perf`` (worth acting on:
    a true N+1 or an actionable loop) and ``perf_n_plus_one`` (the "N+1"
    wording is right).
    """
    if label in (None, "unsure"):
        return None
    if view == "perf":
        return label in _PERF_TP
    if view == "perf_n_plus_one":
        return label == "true_n_plus_1"
    return {"TP": True, "FP": False}.get(label)


# --- counting ----------------------------------------------------------------


def count_cells(
    findings: list[dict],
    verdicts: dict[str, dict],
    view: str,
    oracle_tp: set[str] = frozenset(),
) -> dict[str, dict]:
    """``{tier: {findings, tp, fp, by_source: {labeler: {tp, fp}}}}``.

    Precedence per finding: a label in ``verdicts`` (human, or a suggestion
    when opted in), then the oracle's auto-TP. A suggestion outranks the
    oracle because it was made by reading the code; it only reaches here
    under ``--include-suggested``, which is reported as unconfirmed.
    """
    cells: dict[str, dict] = {}
    for f in findings:
        cell = cells.setdefault(f["tier"], {"findings": 0, "tp": 0, "fp": 0, "by_source": {}})
        cell["findings"] += 1
        row = verdicts.get(f["key"])
        if row is not None:
            source, ok = row["labeler"], verdict(view, row["label"])
        elif f["key"] in oracle_tp:
            source, ok = ORACLE, True
        else:
            continue
        if ok is None:
            continue
        cell["tp" if ok else "fp"] += 1
        src = cell["by_source"].setdefault(source, {"tp": 0, "fp": 0})
        src["tp" if ok else "fp"] += 1
    return dict(sorted(cells.items()))


def debt(cell: dict, target: int = SAMPLE_PER_CELL) -> int:
    """How many more judged findings the cell needs to reach its sample
    target (every finding when the cell is smaller than the target)."""
    judged = cell["tp"] + cell["fp"]
    return max(0, min(cell["findings"], target) - judged)


# --- agreement ---------------------------------------------------------------


def cohen_kappa(a: list[str], b: list[str]) -> float | None:
    """Cohen's kappa for two labellers' labels on the same items, in order.

    None when there are no items; 1.0 when both labellers used one identical
    category throughout (agreement is perfect, chance agreement is 1).
    """
    if len(a) != len(b):
        raise ValueError("label lists differ in length")
    n = len(a)
    if n == 0:
        return None
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[k] * cb[k] for k in ca.keys() | cb.keys()) / (n * n)
    if expected == 1.0:
        return 1.0
    return (observed - expected) / (1 - expected)


def double_labelled(rows: list[dict]) -> tuple[list[str], list[str], int]:
    """(first labeller's labels, second labeller's labels, human-labelled keys)
    over keys two different humans labelled; each labeller's last row counts."""
    by_key: dict[str, dict[str, str]] = defaultdict(dict)
    for r in rows:
        if r.get("label") and is_human(r):
            by_key[r["finding_key"]][r["labeler"]] = r["label"]
    a, b = [], []
    for labels in by_key.values():
        if len(labels) >= 2:
            first, second = list(labels.values())[:2]
            a.append(first)
            b.append(second)
    return a, b, len(by_key)


# --- queue sampler -----------------------------------------------------------


def _git_head(checkout: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def queue_rows(
    checkout: Path,
    family: str,
    existing: set[str],
    *,
    per_cell: int = SAMPLE_PER_CELL,
    spot_check: int = ORACLE_SPOT_CHECK,
    seed: int = 0,
    tiers: set[str] | None = None,
) -> list[dict]:
    """Unlabelled queue rows sampled from ``checkout``'s index.

    Cells are (kind, tier) for dead code and biomarker type for health/perf.
    Dead-code findings the mention oracle auto-labels are left out, except a
    spot-check sample (``oracle: "no_mentions"``) that measures the oracle.
    ``tiers`` restricts the queue to those tiers (e.g. unlabelled types).
    """
    findings = index_findings(checkout / ".repowise" / "wiki.db", family)
    if tiers:
        findings = [f for f in findings if f["tier"] in tiers]
    sha = _git_head(checkout)
    mentions: dict[str, list] = {}
    if family == "dead_code":
        sys.path.insert(0, str(HERE))
        from oracles.dead_code_mentions import MentionIndex

        index = MentionIndex(checkout)
        mentions = {f["key"]: index.mentions(f) for f in findings}
    # Hosted keys can name several findings (one per file for some biomarkers);
    # a key is one labelling unit, so queue it once.
    cells: dict[tuple, list[dict]] = defaultdict(list)
    seen = set(existing)
    for f in findings:
        if f["key"] not in seen:
            seen.add(f["key"])
            cells[(f.get("kind", ""), f["tier"])].append(f)
    rng = random.Random(seed)
    out = []
    for (_, _), pool in sorted(cells.items()):
        if family == "dead_code":
            mentioned = [f for f in pool if mentions[f["key"]]]
            silent = [f for f in pool if not mentions[f["key"]]]
            picked = rng.sample(mentioned, min(per_cell, len(mentioned)))
            picked += rng.sample(silent, min(spot_check, len(silent)))
        else:
            picked = rng.sample(pool, min(per_cell, len(pool)))
        for f in picked:
            row = {"finding_key": f["key"], "sha": sha, "tier": f["tier"]}
            if family == "dead_code":
                hits = mentions[f["key"]]
                row |= {
                    "kind": f["kind"],
                    "file_path": f["file_path"],
                    "symbol_name": f["symbol_name"],
                    "line_start": f["start_line"],
                    "line_end": f["end_line"],
                    "confidence": f["confidence"],
                    "mentions": len(hits),
                    "mentioned_in": [f"{p}:{n}" for p, n in hits[:3]],
                }
                if not hits:
                    row["oracle"] = "no_mentions"
            else:
                row |= {
                    "biomarker_type": f["biomarker_type"],
                    "file_path": f["file_path"],
                    "function_name": f["function_name"],
                    "line_start": f["line_start"],
                    "line_end": f["line_end"],
                    "claim": f["reason"],
                }
            row |= {"label": None, "reason": None, "labeler": None, "date": None}
            out.append(row)
    return out


# --- CLI ---------------------------------------------------------------------


def _append(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _summary(labels_dir: Path, repos: list[str] | None) -> str:
    lines = ["| repo | family | labeler | label | n |", "|---|---|---|---|---:|"]
    spot = ["", "| repo | oracle spot-checks judged | agree (TP) |", "|---|---:|---:|"]
    dirs = sorted(p for p in Path(labels_dir).iterdir() if p.is_dir())
    for d in dirs:
        if repos and d.name not in repos:
            continue
        for fam in FAMILIES:
            rows = load_labels(d.name, fam, labels_dir)
            counts = Counter((r.get("labeler") or "(queue)", r.get("label") or "-") for r in rows)
            for (who, label), n in sorted(counts.items()):
                lines.append(f"| {d.name} | {fam} | {who} | {label} | {n} |")
            checked = [r for r in rows if r.get("oracle") == "no_mentions" and r.get("label")]
            judged = [r for r in checked if r["label"] != "unsure"]
            if judged:
                agree = sum(r["label"] == "TP" for r in judged)
                spot.append(f"| {d.name} | {len(judged)} | {agree} |")
    return "\n".join(lines + (spot if len(spot) > 3 else [])) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Finding labels: queue, summary, kappa.")
    ap.add_argument("--labels-dir", type=Path, default=LABELS_DIR)
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("queue", help="append a sampled labelling queue for one indexed checkout")
    q.add_argument("checkout", type=Path)
    q.add_argument("--repo", required=True)
    q.add_argument("--family", choices=FAMILIES, required=True)
    q.add_argument("--per-cell", type=int, default=SAMPLE_PER_CELL)
    q.add_argument("--spot-check", type=int, default=ORACLE_SPOT_CHECK)
    q.add_argument("--seed", type=int, default=0)
    q.add_argument("--tiers", help="comma-separated tiers / biomarker types to queue")
    s = sub.add_parser("summary", help="label counts by repo, family, labeler and label")
    s.add_argument("--repo", action="append")
    k = sub.add_parser("kappa", help="Cohen's kappa over double-labelled findings")
    k.add_argument("--repo", required=True)
    k.add_argument("--family", choices=FAMILIES, required=True)
    args = ap.parse_args(argv)

    if args.cmd == "queue":
        path = labels_path(args.repo, args.family, args.labels_dir)
        existing = {r["finding_key"] for r in load_labels(args.repo, args.family, args.labels_dir)}
        rows = queue_rows(
            args.checkout,
            args.family,
            existing,
            per_cell=args.per_cell,
            spot_check=args.spot_check,
            seed=args.seed,
            tiers=set(args.tiers.split(",")) if args.tiers else None,
        )
        _append(path, rows)
        print(f"{len(rows)} queue rows appended to {path}")
        return 0
    if args.cmd == "summary":
        sys.stdout.write(_summary(args.labels_dir, args.repo))
        return 0
    rows = load_labels(args.repo, args.family, args.labels_dir)
    problems = validate(rows, args.family)
    for p in problems:
        print(f"invalid: {p}")
    a, b, labelled = double_labelled(rows)
    kappa = cohen_kappa(a, b)
    share = len(a) / labelled if labelled else 0.0
    shown = "-" if kappa is None else f"{kappa:.3f}"
    print(f"double-labelled {len(a)}/{labelled} ({share:.0%}), kappa {shown}")
    ok = kappa is not None and kappa >= KAPPA_FLOOR and share >= DOUBLE_LABEL_SHARE
    print("PASS" if ok and not problems else "FAIL")
    return 0 if ok and not problems else 1


if __name__ == "__main__":
    sys.exit(main())
