"""Fold a commit-mined decision into the PR decision mined from the same merge."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable

from repowise.core.generation.page_overlap import jaccard

from ..models import DecisionRecord
from .decision_evidence import _first_commit

#: Marks an incoming decision this fold retitled, so its files join the PR record's.
FOLDED_INTO_PR = "_folded_into_pr"

# Miners echo the 8-char sha shown in their prompt, so shas compare by prefix.
_MIN_SHA_PREFIX = 7

# A merge with several PR decisions pairs by title overlap. Fixed and strict
# because no labelled pairs exist to tune it; a looser floor needs them.
_MIN_TITLE_OVERLAP = 0.5


def fold_archaeology_into_pr(
    decisions: list[dict],
    existing: Iterable[DecisionRecord],
    normalize: Callable[[str], str],
) -> list[dict]:
    """Retitle each ``git_archaeology`` decision that restates a ``pr`` decision of its commit.

    The PR lane mines a merge or squash commit's body and the archaeology lane
    mines the same commit, so one merge can yield one decision under two
    titles. Taking the PR title lets title dedup fold the pair into the PR
    record, which outranks the commit lane and so keeps its id and wording.

    A commit with one PR decision pairs with it; with several, the unique best
    title overlap above :data:`_MIN_TITLE_OVERLAP` does. A PR title that two
    archaeology decisions pair with folds neither, since the pairing is then
    ambiguous.
    """
    pr_titles: dict[str, dict[str, str]] = {}
    for d in decisions:
        if d.get("source") == "pr":
            _add(pr_titles, _first_commit(d), d.get("title", ""), normalize)
    for rec in existing:
        if rec.source == "pr":
            _add(pr_titles, _record_commit(rec), rec.title, normalize)
    if not pr_titles:
        return decisions

    pairs = {
        i: _pair(d, pr_titles, normalize)
        for i, d in enumerate(decisions)
        if d.get("source") == "git_archaeology"
    }
    claims = Counter(norm for norm, _ in filter(None, pairs.values()))
    out: list[dict] = []
    for i, d in enumerate(decisions):
        pair = pairs.get(i)
        if pair is not None and claims[pair[0]] == 1:
            d = {**d, "title": pair[1], FOLDED_INTO_PR: True}
        out.append(d)
    return out


def _pair(
    d: dict, pr_titles: dict[str, dict[str, str]], normalize: Callable[[str], str]
) -> tuple[str, str] | None:
    """The ``(normalized, original)`` PR title this archaeology decision restates, if any."""
    sha = _first_commit(d)
    if not sha or len(sha) < _MIN_SHA_PREFIX:
        return None
    on_commit = _titles_on(pr_titles, sha)
    if len(on_commit) == 1:
        return next(iter(on_commit.items()))
    own = normalize(d.get("title", "")).split()
    scored = sorted(((jaccard(own, norm.split()), norm) for norm in on_commit), reverse=True)
    if not scored or scored[0][0] < _MIN_TITLE_OVERLAP:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None
    best = scored[0][1]
    return best, on_commit[best]


def _add(
    index: dict[str, dict[str, str]],
    sha: str | None,
    title: str,
    normalize: Callable[[str], str],
) -> None:
    norm = normalize(title)
    if sha and len(sha) >= _MIN_SHA_PREFIX and norm:
        index.setdefault(sha.lower(), {}).setdefault(norm, title)


def _titles_on(index: dict[str, dict[str, str]], sha: str) -> dict[str, str]:
    """Distinct titles (normalized -> original) on every sha sharing a prefix with *sha*."""
    sha = sha.lower()
    found: dict[str, str] = {}
    for key, titles in index.items():
        if key.startswith(sha) or sha.startswith(key):
            for norm, title in titles.items():
                found.setdefault(norm, title)
    return found


def _record_commit(rec: DecisionRecord) -> str | None:
    try:
        commits = json.loads(rec.evidence_commits_json or "[]")
    except ValueError:
        return None
    first = commits[0] if isinstance(commits, list) and commits else None
    return first if isinstance(first, str) else None


def merge_folded_files(rec: DecisionRecord, members: list[dict]) -> None:
    """Add the files of folded archaeology members to a PR record's scope."""
    extra = [f for d in members if d.get(FOLDED_INTO_PR) for f in d.get("affected_files") or []]
    if not extra or rec.source != "pr":
        return
    try:
        files = json.loads(rec.affected_files_json or "[]")
    except ValueError:
        return
    merged = list(dict.fromkeys([*files, *extra]))
    if merged != files:
        rec.affected_files_json = json.dumps(merged)
