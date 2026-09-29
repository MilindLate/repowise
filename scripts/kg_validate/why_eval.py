"""Score local ``get_why`` against a repo's labelled design questions.

    python scripts/kg_validate/why_eval.py --repo /path/to/indexed/clone \
        --labels scripts/kg_validate/labels/<repo>/why_questions.json [--json out.json]

Each question is asked with no targets, through the production middleware. The
served evidence is read in response order (decisions, then rationale comments,
then archaeology commits) and a question hits when one of the first three items
is a labelled comment span (same path, overlapping lines) or a labelled commit.

Two precision checks ride along:

* ``fabricated`` — a served comment whose text is not verbatim in the file from
  the lines it cites, or a served commit whose sha/subject is not in the repo.
  Must be 0.
* ``false_serves`` — rationale served for a question labelled as having no
  recorded answer (``expected: []``).

Needs a real indexed clone, so it does not run in CI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

_TOKEN = re.compile(r"[a-z0-9]+")


def _served(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """The evidence rows a reader sees, in response order."""
    items: list[dict[str, Any]] = []
    for d in resp.get("decisions") or []:
        ev = (d.get("evidence") or [{}])[0] if isinstance(d, dict) else {}
        items.append(
            {
                "kind": "decision",
                "path": ev.get("evidence_file") or "",
                "lines": [ev.get("evidence_line") or 0] * 2,
                "commit": ev.get("evidence_commit") or "",
            }
        )
    for r in resp.get("code_rationale") or []:
        items.append(
            {
                "kind": "comment",
                "path": r.get("path", ""),
                "lines": r.get("lines") or [0, 0],
                "text": r.get("comment", ""),
            }
        )
    arch = resp.get("git_archaeology") or {}
    for lane in ("file_commits", "cross_references", "git_log"):
        for c in arch.get(lane) or []:
            items.append(
                {
                    "kind": "commit",
                    "commit": c.get("commit") or c.get("sha", ""),
                    "message": c.get("message", ""),
                }
            )
    return items


def _matches(item: dict[str, Any], expected: dict[str, Any]) -> bool:
    if "commit" in expected:
        sha = item.get("commit") or ""
        return bool(sha) and (
            sha.startswith(expected["commit"]) or expected["commit"].startswith(sha)
        )
    if item.get("path") != expected["path"]:
        return False
    a, b = item.get("lines") or [0, 0]
    lo, hi = expected["lines"]
    return a <= hi and lo <= b


def _fabricated(item: dict[str, Any], repo: Path) -> bool:
    """Whether a served row claims text the repo does not hold where it says."""
    if item["kind"] == "comment":
        try:
            lines = (repo / item["path"]).read_text(errors="replace").splitlines()
        except OSError:
            return True
        a, b = item["lines"]
        # The harvest caps a long block's cited span at 12 lines but quotes up
        # to 600 chars of it, so a quote may run past `b`: it must start in
        # the span and continue verbatim, not end there.
        region = _TOKEN.findall(" ".join(lines[max(a - 1, 0) : b + 60]).lower())
        # A truncated quote ends mid-word; drop that last token.
        claimed = _TOKEN.findall(item["text"].rstrip("…").lower())[:-1] or _TOKEN.findall(
            item["text"].lower()
        )
        text = " " + " ".join(region) + " "
        return (" " + " ".join(claimed) + " ") not in text
    if item["kind"] == "commit":
        out = subprocess.run(
            ["git", "log", "-1", "--format=%s", item["commit"], "--"],
            cwd=repo,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        return out.returncode != 0 or (item.get("message") or "").strip() not in out.stdout
    return False


async def _run(repo: Path, questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from repowise.server.mcp_server import _server, _state, tool_middleware
    from repowise.server.mcp_server.tool_why import get_why

    _state._repo_path = str(repo)
    handler = tool_middleware(get_why)
    rows = []
    async with _server._lifespan(None):
        for q in questions:
            resp = await handler(query=q["question"])
            items = _served(resp)
            expected = q["expected"]
            rows.append(
                {
                    "id": q["id"],
                    "question": q["question"],
                    "answerable": bool(expected),
                    "hit3": any(_matches(i, e) for i in items[:3] for e in expected),
                    "served": len(items),
                    "fabricated": sum(_fabricated(i, repo) for i in items),
                    "answer_basis": resp.get("answer_basis"),
                    "reason": resp.get("reason", ""),
                    "top": items[:3],
                }
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--json")
    options = parser.parse_args()
    os.environ.setdefault("REPOWISE_TELEMETRY_DISABLED", "1")
    labels = json.loads(Path(options.labels).read_text())
    rows = asyncio.run(_run(Path(options.repo), labels["questions"]))

    answerable = [r for r in rows if r["answerable"]]
    unanswerable = [r for r in rows if not r["answerable"]]
    summary = {
        "labels_status": labels.get("status"),
        "answerable": len(answerable),
        "hit3": sum(r["hit3"] for r in answerable),
        "unanswerable": len(unanswerable),
        "false_serves": sum(r["served"] > 0 for r in unanswerable),
        "fabricated": sum(r["fabricated"] for r in rows),
    }
    for r in rows:
        mark = (
            ("HIT " if r["hit3"] else "miss")
            if r["answerable"]
            else ("FALSE" if r["served"] else "ok  ")
        )
        print(f"{mark} {r['id']} served={r['served']} basis={r['answer_basis']} :: {r['question']}")
    print(json.dumps(summary))
    if options.json:
        Path(options.json).write_text(
            json.dumps({"summary": summary, "rows": rows}, indent=1, default=str)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
