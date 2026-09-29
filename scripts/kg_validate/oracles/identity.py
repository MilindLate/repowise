#!/usr/bin/env python3
"""Identity oracle: which author emails belong to the same person.

Truth is the repo's ``.mailmap`` as git applies it: every raw author email
from ``git log`` (``%ae``) is clustered by its mapped email (``%aE``).
Hand-labelled alias sets from ``labels/<repo>/identity.jsonl`` (see
labels/README.md) are unioned on top, for repos without a mailmap.

To score a tool's own merging on a repo that maintains a mailmap, the tool
must run with the mailmap hidden, otherwise it just copies the answer. Git
always reads the worktree ``.mailmap`` for ``%aN``/``%aE`` (neither
``-c mailmap.file=/dev/null`` nor ``--no-mailmap`` stops that), so
:func:`hidden_mailmap` moves the file aside for the duration of the run.

Metrics over the truth's email universe (a predicted email outside it is
ignored; a truth email the prediction omits is its own singleton, so bot
filtering doesn't masquerade as a merge error):
- B-cubed precision / recall of the predicted clusters,
- person-count error ``|predicted people - true people| / true people``.

Never import repowise here: this grades repowise.

Usage:
    python scripts/kg_validate/oracles/identity.py <checkout> --repo NAME \\
        [--predicted clusters.json] [--labels-dir DIR]
``clusters.json`` is a list of email lists, or of ``{"emails": [...]}``.
Prints the truth clusters, or metric rows for score.py with --predicted.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

LABELS_DIR = Path(__file__).resolve().parent.parent / "labels"


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)

    def clusters(self) -> list[set[str]]:
        groups: dict[str, set[str]] = defaultdict(set)
        for x in self.parent:
            groups[self.find(x)].add(x)
        return list(groups.values())


def git_author_pairs(checkout: str | Path) -> list[tuple[str, str]]:
    """(raw email, mailmap-mapped email) per commit, lowercased."""
    out = subprocess.run(
        ["git", "-C", str(checkout), "log", "--format=%ae%x00%aE"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    pairs = []
    for line in out.splitlines():
        raw, mapped = line.split("\0")
        pairs.append((raw.strip().lower(), mapped.strip().lower()))
    return pairs


def load_alias_sets(repo: str, labels_dir: str | Path = LABELS_DIR) -> list[set[str]]:
    """Hand-labelled alias sets from ``labels/<repo>/identity.jsonl`` (may be empty)."""
    path = Path(labels_dir) / repo / "identity.jsonl"
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [{e.lower() for e in row["emails"]} for row in rows]


def truth_clusters(
    checkout: str | Path, alias_sets: Iterable[Iterable[str]] = ()
) -> list[set[str]]:
    """Raw author emails clustered by mailmap, joined with labelled alias sets.

    Alias-set emails that never authored a commit are left out.
    """
    uf = _UnionFind()
    for raw, mapped in git_author_pairs(checkout):
        uf.find(raw)
        # Key the canonical side apart from raw emails so a mapped email that
        # is also some other raw email still joins through union-find.
        uf.union(raw, "\0" + mapped)
    authors = {x for x in uf.parent if not x.startswith("\0")}
    for aliases in alias_sets:
        present = [e.lower() for e in aliases if e.lower() in authors]
        for e in present[1:]:
            uf.union(present[0], e)
    return [c - {x for x in c if x.startswith("\0")} for c in uf.clusters() if c & authors]


@contextlib.contextmanager
def hidden_mailmap(checkout: str | Path):
    """Move the worktree ``.mailmap`` aside so a tool sees raw identities."""
    path = Path(checkout) / ".mailmap"
    aside = path.with_name(".mailmap.kg-validate-hidden")
    moved = path.is_file()
    if moved:
        path.rename(aside)
    try:
        yield
    finally:
        if moved:
            aside.rename(path)


def score(predicted: Iterable[Iterable[str]], truth: list[set[str]]) -> dict:
    universe = {e for c in truth for e in c}
    true_of = {e: frozenset(c) for c in truth for e in c}
    pred_of: dict[str, frozenset[str]] = {}
    for cluster in predicted:
        members = frozenset(e.lower() for e in cluster) & universe
        for e in members:
            pred_of[e] = members
    for e in universe - pred_of.keys():
        pred_of[e] = frozenset({e})
    if not universe:
        return {"b3_precision": None, "b3_recall": None, "person_count_error": None}
    precision = sum(len(pred_of[e] & true_of[e]) / len(pred_of[e]) for e in universe) / len(
        universe
    )
    recall = sum(len(pred_of[e] & true_of[e]) / len(true_of[e]) for e in universe) / len(universe)
    n_pred = len(set(pred_of.values()))
    return {
        "b3_precision": precision,
        "b3_recall": recall,
        "person_count_error": abs(n_pred - len(truth)) / len(truth),
        "true_people": len(truth),
        "predicted_people": n_pred,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Identity oracle (mailmap + labelled aliases).")
    ap.add_argument("checkout", help="repo checkout")
    ap.add_argument("--repo", required=True, help="repo name (labels/<repo>/)")
    ap.add_argument("--predicted", help="JSON list of the tool's email clusters")
    ap.add_argument("--labels-dir", default=str(LABELS_DIR))
    args = ap.parse_args(argv)
    truth = truth_clusters(args.checkout, load_alias_sets(args.repo, args.labels_dir))
    if not args.predicted:
        for cluster in sorted(sorted(c) for c in truth):
            print(json.dumps({"repo": args.repo, "family": "identity", "emails": cluster}))
        return 0
    raw = json.loads(Path(args.predicted).read_text())
    predicted = [c["emails"] if isinstance(c, dict) else c for c in raw]
    for metric, value in score(predicted, truth).items():
        if value is not None:
            print(
                json.dumps(
                    {"repo": args.repo, "family": "identity", "metric": metric, "value": value}
                )
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
