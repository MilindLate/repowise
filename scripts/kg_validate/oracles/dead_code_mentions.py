#!/usr/bin/env python3
"""Dead-code mention oracle: is a "dead" finding's name mentioned anywhere else?

Scans **every tracked file** (``git ls-files``: code, JSON, YAML, TOML, MD,
shell, package.json, lockfiles, ...) for a finding's name outside its own
definition:

- symbol findings (``unused_export``, ``unused_internal``): the symbol name as
  a whole identifier (``[A-Za-z0-9_$]`` boundaries). Occurrences inside the
  definition span in the defining file do not count; without a span, the
  first occurrence in the defining file is taken as the declaration. A use
  elsewhere in the defining file does count (``used_in_file``).
- file findings (``unreachable_file``) and ``zombie_package`` directories:
  path-shaped tokens only, never the bare stem (``utils`` or ``index`` alone
  would match everything). A file ``a/b/mod.py`` is mentioned by
  ``a/b/mod.py``, ``b/mod.py``, ``mod.py``, ``/mod`` (an import specifier or
  path ending in the stem, e.g. ``./mod``), and for Python the dotted module
  suffixes ``a.b.mod`` / ``b.mod`` (``__init__.py`` means its package) plus
  the ``module:attr`` object reference that entry points and app servers use
  (``mod:cli``, ``pkg.mod:app``). A mention inside the file itself does not
  count. A directory is mentioned by its path.

A finding with **zero** mentions is auto-labelled TP (label source
``oracle``): nothing in the repository names it, so nothing can load it by
name. Any mention sends the finding to the human labelling queue - a mention
is not proof of use (a comment, a changelog line), only a reason to look.
Matching is deliberately liberal: an extra mention only costs a human look,
while a missed one would auto-label a live finding dead.

Never import repowise here: this grades repowise.

Usage:
    python scripts/kg_validate/oracles/dead_code_mentions.py <checkout> \\
        --findings findings.jsonl [--max-shown 3]
Each findings row is ``{kind, file_path, symbol_name?, start_line?, end_line?}``;
prints the row with ``mentions`` (count) and ``mentioned_in`` (first hits).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path, PurePosixPath

FILE_KINDS = frozenset({"unreachable_file", "zombie_package"})
# Files larger than this are generated bundles or data dumps; skipping them
# can only miss a mention in a file no human wrote.
MAX_FILE_BYTES = 4_000_000
_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_WORD = r"(?<![A-Za-z0-9_$]){}(?![A-Za-z0-9_$])"


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None  # binary
    return data.decode("utf-8", "replace")


def file_tokens(path: str) -> list[str]:
    """Path-shaped strings that name ``path``. Never the bare stem."""
    p = PurePosixPath(path)
    parts = p.parts
    tokens = {"/".join(parts[i:]) for i in range(len(parts) - 1)}  # >= dir/name
    tokens.add("/" + p.stem)  # ./mod, dir/mod (an extensionless specifier)
    if p.suffix:
        tokens.add(p.name)  # mod.py
        no_ext = [*parts[:-1], p.stem]
        tokens |= {"/".join(no_ext[i:]) for i in range(len(no_ext) - 1)}  # >= dir/stem
    if p.suffix == ".py":
        mod = list(parts[:-1]) if p.stem == "__init__" else [*parts[:-1], p.stem]
        tokens |= {".".join(mod[i:]) for i in range(len(mod) - 1)}  # >= pkg.mod
        if mod:
            tokens.add(mod[-1] + ":")  # mod:attr (the stem only in this form)
    return sorted(t for t in tokens if t)


def _token_pattern(token: str) -> re.Pattern:
    # A token must not continue a longer path or identifier on either side
    # (``b/mod.py`` must not match ``b/mod.pyc``; ``/mod`` not ``/model``).
    head = "" if token[0] in "/." else r"(?<![A-Za-z0-9_$\-])"
    # ``mod:`` counts only as ``mod:attr`` (never ``mod: value`` in YAML).
    tail = r"(?=[A-Za-z_])" if token.endswith(":") else r"(?![A-Za-z0-9_$\-])"
    return re.compile(head + re.escape(token) + tail)


class MentionIndex:
    """Every tracked text file of one checkout, with an identifier index."""

    def __init__(self, root: Path, files: list[str] | None = None):
        self.root = Path(root)
        self.texts: dict[str, str] = {}
        for rel in tracked_files(self.root) if files is None else files:
            text = _read_text(self.root / rel)
            if text is not None:
                self.texts[rel] = text
        self._idents: dict[str, list[tuple[str, int]]] | None = None

    def _ident_index(self) -> dict[str, list[tuple[str, int]]]:
        if self._idents is None:
            idx: dict[str, list[tuple[str, int]]] = defaultdict(list)
            for rel, text in self.texts.items():
                for lineno, line in enumerate(text.splitlines(), 1):
                    for name in set(_IDENT.findall(line)):
                        idx[name].append((rel, lineno))
            self._idents = idx
        return self._idents

    def symbol_mentions(
        self, name: str, def_path: str, start: int | None = None, end: int | None = None
    ) -> list[tuple[str, int]]:
        if _IDENT.fullmatch(name):
            hits = list(self._ident_index().get(name, []))
        else:  # dotted / odd names: fall back to a scan
            pat = re.compile(_WORD.format(re.escape(name)))
            hits = [
                (rel, n)
                for rel, text in self.texts.items()
                for n, line in enumerate(text.splitlines(), 1)
                if pat.search(line)
            ]
        if start:
            span = range(start, (end or start) + 1)
            return [(rel, n) for rel, n in hits if not (rel == def_path and n in span)]
        own = [h for h in hits if h[0] == def_path]
        declaration = own[0] if own else None
        return [h for h in hits if h != declaration]

    def path_mentions(self, path: str, *, is_dir: bool = False) -> list[tuple[str, int]]:
        path = path.rstrip("/")
        # A directory (zombie package) is named by its full path only.
        tokens = [path] if is_dir else file_tokens(path)
        patterns = [_token_pattern(t) for t in tokens]
        hits = []
        for rel, text in self.texts.items():
            if rel == path or rel.startswith(path + "/"):
                continue  # a file (or package) naming itself is not a use
            for n, line in enumerate(text.splitlines(), 1):
                if any(p.search(line) for p in patterns):
                    hits.append((rel, n))
        return hits

    def mentions(self, finding: dict) -> list[tuple[str, int]]:
        if finding.get("kind") in FILE_KINDS or not finding.get("symbol_name"):
            return self.path_mentions(
                finding["file_path"], is_dir=finding.get("kind") == "zombie_package"
            )
        return self.symbol_mentions(
            finding["symbol_name"],
            finding["file_path"],
            finding.get("start_line") or finding.get("line_start"),
            finding.get("end_line") or finding.get("line_end"),
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("checkout", type=Path)
    ap.add_argument("--findings", required=True, help="findings JSON/JSONL")
    ap.add_argument("--max-shown", type=int, default=3)
    args = ap.parse_args(argv)
    text = Path(args.findings).read_text()
    rows = (
        json.loads(text)
        if text.lstrip().startswith("[")
        else [json.loads(line) for line in text.splitlines() if line.strip()]
    )
    index = MentionIndex(args.checkout)
    for row in rows:
        hits = index.mentions(row)
        shown = [f"{rel}:{n}" for rel, n in hits[: args.max_shown]]
        print(json.dumps({**row, "mentions": len(hits), "mentioned_in": shown}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
