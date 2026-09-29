"""Revert-based supersession: retire a decision whose commits were reverted.

A decision mined from a commit is shown as current for as long as nothing says
otherwise, including when a later commit reverted it. Git records the reversal
itself, so this reads it from history rather than from similarity (the
semantic detector in :mod:`.evolution` stays off; see the note there).

A revert is matched to its target by three rules, strongest first, and a
commit whose message fits a stronger rule is never retried with a weaker one:

- **body**: the message carries ``This reverts commit <40-hex sha>`` (what
  ``git revert`` writes).
- **subject**: the subject is ``Revert "<S>"`` and exactly one other commit in
  history has subject ``S``, both compared with a trailing ``(#N)`` or
  ``(gh-N)`` stripped.
- **pr**: the subject is ``revert:``/``revert(scope):`` and the message
  references ``#N`` or ``/pull/N``, with exactly one commit that is that pull
  request (subject ending ``(#N)`` or ``Merge pull request #N``).

Every match also needs the target to be an ancestor of the revert, and the two
must change at least one file in common. A match is dropped when a commit after
the revert carries the target's subject again (ignoring ``reland``/``reapply``
markers) or closes the same issue (``Fixes #N``): the change was re-landed, so
the decision holds. A revert that calls itself partial is not matched at all.

A revert can itself be reverted, so a commit counts as reverted at HEAD only
when at least one of its reverts is not (parity along the chain).

A decision is retired only when *every* evidence commit it rests on is reverted
at HEAD and it has no evidence that is not a commit (an ADR file, an inline
marker). Anything partial or ambiguous is left as it was.

Ceiling: uniqueness for the subject and pr rules is judged over the history the
clone holds, so a shallow clone can make a duplicate subject look unique. The
target must still be an ancestor of the revert, which bounds the damage to
commits inside the clone's window.
"""

from __future__ import annotations

import bisect
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

__all__ = [
    "REVERT_SUPERSEDED_PREFIX",
    "RevertLink",
    "apply_revert_supersession",
    "find_revert_links",
    "reverted_at_head",
]

#: ``superseded_by`` for a record retired here: ``revert:<sha prefix>``. It is
#: not a decision id, so no lineage walk or alias lookup resolves it, and
#: ``unretire_auto_superseded`` (which keys on ``auto-detected:`` edges) never
#: touches it. The column is 32 characters, so the sha is cut to fit.
REVERT_SUPERSEDED_PREFIX = "revert:"
_SUPERSEDED_BY_WIDTH = 32

_BODY_RE = re.compile(r"This reverts commit ([0-9a-f]{40})\b")
_SUBJECT_RE = re.compile(r'^Revert "(.+)"$')
_CONVENTIONAL_RE = re.compile(r"^revert(?:\([^)]*\))?!?:", re.IGNORECASE)
_PR_REF_RE = re.compile(r"(?:#|/pull/)(\d+)\b")
_PR_SUFFIX_RE = re.compile(r"\s*\((?:#|gh-)(\d+)\)\s*$", re.IGNORECASE)
_MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+)\b")
_PARTIAL_RE = re.compile(r"\bpart(?:ial(?:ly)?|ly)\b", re.IGNORECASE)
_RELAND_WORD_RE = re.compile(r"\s*\(?\b(?:re-?land(?:ed)?|re-?appl(?:y|ied))\b\)?:?", re.IGNORECASE)
_CLOSES_RE = re.compile(r"\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+#(\d+)\b", re.IGNORECASE)

_FIELD = "\x1f"
_RECORD = "\x1e"


@dataclass(frozen=True)
class RevertLink:
    """``revert`` undoes ``target``; ``rule`` is body | subject | pr."""

    revert: str
    target: str
    rule: str


def _git(repo_path: Path | str, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout


def _strip_pr_suffix(subject: str) -> str:
    while True:
        stripped = _PR_SUFFIX_RE.sub("", subject)
        if stripped == subject:
            return subject.strip()
        subject = stripped


def _reland_key(subject: str) -> str:
    """A subject with any "reland"/"reapply" marker removed, for re-land matching."""
    text = _RELAND_WORD_RE.sub(" ", _strip_pr_suffix(subject)).replace("`", "")
    return " ".join(text.split()).lower()


def _pr_number(subject: str) -> str | None:
    m = _PR_SUFFIX_RE.search(subject) or _MERGE_PR_RE.match(subject)
    return m.group(1) if m else None


def find_revert_links(repo_path: Path | str, head: str = "HEAD") -> list[RevertLink]:
    """Every revert in *head*'s history matched to its target, ancestors only."""
    # Only messages that can fit a rule; the full subject list is read only if
    # one of them needs a uniqueness check.
    code, out = _git(
        repo_path,
        "log",
        head,
        "-i",
        "-E",
        "--grep=^revert",
        "--grep=This reverts commit [0-9a-f]{40}",
        f"--format=%H{_FIELD}%s{_FIELD}%b{_RECORD}",
    )
    if code != 0:
        return []
    candidates: list[tuple[str, str, str]] = []
    for raw in out.split(_RECORD):
        parts = raw.strip("\n").split(_FIELD)
        if len(parts) == 3 and parts[0]:
            candidates.append((parts[0].strip(), parts[1], parts[2]))
    if not candidates:
        return []

    by_subject: dict[str, list[str]] = {}
    by_pr: dict[str, list[str]] = {}
    subject_of: dict[str, str] = {}
    by_reland_key: dict[str, list[str]] = {}
    by_closed_issue: dict[str, list[str]] = {}
    code, out = _git(repo_path, "log", head, f"--format=%H{_FIELD}%s")
    if code != 0:
        return []
    for line in out.splitlines():
        sha, _, subject = line.partition(_FIELD)
        subject_of[sha] = _strip_pr_suffix(subject)
        by_reland_key.setdefault(_reland_key(subject), []).append(sha)
        for issue in set(_CLOSES_RE.findall(subject)):
            by_closed_issue.setdefault(issue, []).append(sha)
        by_subject.setdefault(_strip_pr_suffix(subject), []).append(sha)
        pr = _pr_number(subject)
        if pr:
            by_pr.setdefault(pr, []).append(sha)

    proposed: list[RevertLink] = []
    for sha, subject, body in candidates:
        if _PARTIAL_RE.search(f"{subject}\n{body}"):
            # "Partially revert X": X's decision may well still hold.
            continue
        cited = _BODY_RE.findall(f"{subject}\n{body}")
        if cited:
            proposed.extend(RevertLink(sha, t, "body") for t in dict.fromkeys(cited) if t != sha)
            continue
        m = _SUBJECT_RE.match(_strip_pr_suffix(subject))
        if m:
            others = [s for s in by_subject.get(_strip_pr_suffix(m.group(1)), []) if s != sha]
            if len(others) == 1:
                proposed.append(RevertLink(sha, others[0], "subject"))
            continue
        if _CONVENTIONAL_RE.match(subject):
            refs = set(_PR_REF_RE.findall(f"{subject}\n{body}"))
            targets = {s for n in refs for s in by_pr.get(n, []) if s != sha}
            if len(targets) == 1:
                proposed.append(RevertLink(sha, targets.pop(), "pr"))

    def is_ancestor(older: str, newer: str) -> bool:
        return _git(repo_path, "merge-base", "--is-ancestor", older, newer)[0] == 0

    files_cache: dict[str, set[str]] = {}

    def files(sha: str) -> set[str]:
        if sha not in files_cache:
            out = _git(
                repo_path,
                "diff-tree",
                "-r",
                "--no-commit-id",
                "--name-only",
                "-m",
                "--first-parent",
                sha,
            )[1]
            files_cache[sha] = set(out.split("\n")) - {""}
        return files_cache[sha]

    revert_shas = {sha for sha, _, _ in candidates}

    def relanded(link: RevertLink) -> bool:
        # The change landing again after the revert, under the same subject or
        # closing the same issue: the decision is current, carried by the
        # later commit.
        later = set(by_reland_key.get(_reland_key(subject_of[link.target]), []))
        for issue in _CLOSES_RE.findall(subject_of[link.target]):
            later.update(by_closed_issue.get(issue, []))
        return any(
            s != link.target and s not in revert_shas and is_ancestor(link.revert, s) for s in later
        )

    return [
        link
        for link in proposed
        if link.target in subject_of
        and is_ancestor(link.target, link.revert)
        # A squash commit can carry a branch's "This reverts commit" lines
        # without its diff undoing any of them; a revert touches its target.
        and files(link.target) & files(link.revert)
        and not relanded(link)
    ]


def reverted_at_head(links: list[RevertLink]) -> dict[str, RevertLink]:
    """Commits whose change is undone at HEAD, each with a revert still in force.

    A commit is reverted when one of its reverts is not itself reverted; a
    revert of a revert restores the original, a third revert undoes it again.
    """
    reverts_of: dict[str, list[RevertLink]] = {}
    for link in links:
        reverts_of.setdefault(link.target, []).append(link)

    memo: dict[str, RevertLink | None] = {}

    def effective(sha: str, seen: frozenset[str]) -> RevertLink | None:
        if sha in memo:
            return memo[sha]
        found = None
        for link in reverts_of.get(sha, []):
            # A cycle cannot happen through ancestry; guard anyway.
            if link.revert in seen:
                continue
            if effective(link.revert, seen | {sha}) is None:
                found = link
                break
        memo[sha] = found
        return found

    out: dict[str, RevertLink] = {}
    for sha in reverts_of:
        link = effective(sha, frozenset())
        if link is not None:
            out[sha] = link
    return out


def _resolve(sha: str, full_shas: list[str]) -> str | None:
    """A stored sha (possibly abbreviated) as the unique full sha it names."""
    sha = sha.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        return None
    i = bisect.bisect_left(full_shas, sha)
    if i >= len(full_shas) or not full_shas[i].startswith(sha):
        return None
    if i + 1 < len(full_shas) and full_shas[i + 1].startswith(sha):
        return None
    return full_shas[i]


async def apply_revert_supersession(
    session: Any, repository_id: str, repo_path: Path | str | None
) -> dict[str, int]:
    """Retire decisions whose evidence commits are all reverted at HEAD.

    Also restores a record this pass retired earlier once its commits are no
    longer all reverted (a later revert of the revert). Idempotent.
    """
    from sqlalchemy import or_, select

    from repowise.core.analysis.decisions.evolution import _retire
    from repowise.core.analysis.decisions.lifecycle import RETIRED_STATUSES
    from repowise.core.persistence.decision_graph import sync_links_from_record
    from repowise.core.persistence.models import DecisionEvidence, DecisionRecord

    result = {"superseded": 0, "restored": 0}
    if not repo_path or not Path(repo_path).exists():
        return result

    records = (
        (
            await session.execute(
                select(DecisionRecord).where(
                    DecisionRecord.repository_id == repository_id,
                    or_(
                        DecisionRecord.status.not_in(tuple(RETIRED_STATUSES)),
                        DecisionRecord.superseded_by.startswith(REVERT_SUPERSEDED_PREFIX),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    commits_by_record: dict[str, set[str]] = {}
    for rec in records:
        try:
            commits = json.loads(rec.evidence_commits_json or "[]")
        except ValueError:
            commits = []
        commits_by_record[rec.id] = {c for c in commits if isinstance(c, str) and c}
    non_commit_evidence: set[str] = set()
    if records:
        rows = (
            await session.execute(
                select(DecisionEvidence.decision_id, DecisionEvidence.evidence_commit).where(
                    DecisionEvidence.decision_id.in_([r.id for r in records])
                )
            )
        ).all()
        for decision_id, commit in rows:
            if commit:
                commits_by_record[decision_id].add(commit)
            else:
                non_commit_evidence.add(decision_id)
    if not any(commits_by_record.values()):
        return result

    reverted = reverted_at_head(find_revert_links(repo_path))
    code, out = _git(repo_path, "rev-list", "HEAD")
    full_shas = sorted(out.split()) if code == 0 else []

    for rec in records:
        commits = commits_by_record.get(rec.id, set())
        resolved = [_resolve(c, full_shas) for c in commits]
        all_reverted = (
            bool(resolved)
            and rec.id not in non_commit_evidence
            and all(c is not None and c in reverted for c in resolved)
        )
        retired_here = (rec.superseded_by or "").startswith(REVERT_SUPERSEDED_PREFIX)
        if all_reverted and not retired_here:
            revert_sha = reverted[resolved[0]].revert  # type: ignore[index]
            await _retire(
                session,
                rec,
                successor_id=f"{REVERT_SUPERSEDED_PREFIX}{revert_sha}"[:_SUPERSEDED_BY_WIDTH],
            )
            await sync_links_from_record(session, rec)
            result["superseded"] += 1
        elif retired_here and not all_reverted and rec.status == "superseded":
            # The revert was itself reverted: the decision holds again.
            # ``proposed``, as ``unretire_auto_superseded`` restores: the lower
            # claim, and one ``decision confirm`` away from active.
            rec.status = "proposed"
            rec.superseded_by = None
            await sync_links_from_record(session, rec)
            result["restored"] += 1

    if any(result.values()):
        await session.flush()
        logger.info("decisions.revert_supersession", **result)
    return result
