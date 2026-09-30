"""The facts a file page opens with, reduced to what a sentence can carry.

A file page is rendered without a model, so its first paragraph is the one
place a reader, the wiki list and ``get_context`` meet the file in prose. That
paragraph leads with the author's own words when the file has a docstring and
otherwise says what the file defines and who imports it. The sentences live in
``file_page.j2`` so they stay in the label catalog; this module only counts,
picks and orders, which the template language does badly.

Every value is derived from the parse, the import graph or the knowledge graph,
and every choice breaks ties by name, so the same index renders the same bytes.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from repowise.core.test_paths import is_test_related_path

from .structural import api_symbols, as_markdown, code_span, oneline, signature

# Names quoted in a sentence before it switches to "and N more"; one more
# than this is named in full, since "and 1 more" is longer than the name.
_NAMED_IN_SENTENCE = 3
# A dependency list this short is named in full rather than counted.
_LIST_IN_FULL = 3
# One line of a symbol's docstring beside its signature.
_SYMBOL_DOC_LIMIT = 140
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s")


def first_sentence(text: object, limit: int = _SYMBOL_DOC_LIMIT) -> str:
    """The first sentence of a docstring's prose, flattened to one line."""
    paragraphs = [p for p in as_markdown(text).split("\n\n") if not p.startswith("#")]
    paragraph = paragraphs[0] if paragraphs else ""
    flat = " ".join(paragraph.split())
    sentence = _SENTENCE_END_RE.split(flat, 1)[0]
    line = oneline(sentence, limit)
    # A cut through a code span would leave it open and swallow the rest of
    # the line into code.
    if line.count("`") % 2:
        cut = line.endswith("…")
        line = line.rstrip("…") + "`" + ("…" if cut else "")
    return line


def _largest_group(paths: Iterable[str]) -> tuple[str, int]:
    """The directory holding most of *paths* and how many, or ("", 0).

    Only a clear winner is named: at least two files and more than any other
    directory. A tie or a spread of singletons names nothing rather than an
    arbitrary directory.
    """
    counts = Counter(p.rsplit("/", 1)[0] if "/" in p else "." for p in paths)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    if not ranked or ranked[0][1] < 2:
        return "", 0
    if len(ranked) > 1 and ranked[1][1] == ranked[0][1]:
        return "", 0
    return ranked[0]


def _neighbours(paths: list[str]) -> dict[str, Any]:
    """Count, short list and largest directory of an import neighbourhood."""
    tests = {p for p in paths if is_test_related_path(p)}
    code = [p for p in paths if p not in tests]
    # Where the code that uses it lives says more than where its tests do.
    directory, share = _largest_group(code or paths)
    return {
        "count": len(paths),
        "tests": len(tests),
        "listed": [code_span(p) for p in sorted(paths)] if len(paths) <= _LIST_IN_FULL else [],
        "directory": code_span(directory) if directory else "",
        "share": share,
    }


def file_facts(ctx: Any) -> dict[str, Any]:
    """The values ``file_page.j2`` builds its opening sentences from."""
    api = api_symbols(ctx.symbols)
    top_level = [s for s in api if not s.get("parent_name")] or api
    named = len(top_level) if len(top_level) <= _NAMED_IN_SENTENCE + 1 else _NAMED_IN_SENTENCE
    purpose = ""
    if ctx.docstring:
        purpose = as_markdown(ctx.docstring)
    elif ctx.kg_node_summary and (ctx.is_test or not api):
        # The graph's one-line role is the best available for a file with no
        # API of its own (a test, a config module); for code that defines
        # symbols it only restates them, which the sentence below does better.
        purpose = oneline(ctx.kg_node_summary)
    return {
        "name": code_span(ctx.file_path.rsplit("/", 1)[-1]),
        "purpose": purpose,
        "defines": [code_span(s["name"]) for s in top_level[:named]],
        "defines_more": len(top_level) - len(top_level[:named]),
        "importers": _neighbours(list(ctx.dependents)),
        "imports": _neighbours(list(ctx.dependencies)),
    }


def symbol_entries(symbols: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The API list: one entry per symbol, methods nested under their class.

    A method whose class is not itself listed stays at the top level, so no
    public name leaves the page.
    """
    api = api_symbols(symbols)
    entries: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    for sym in api:
        declared = signature(sym.get("signature") or "") or sym["name"]
        if sym["name"] not in declared:
            declared = f"{sym['name']}: {declared}"
        entry = {
            "declared": code_span(declared),
            "doc": first_sentence(sym.get("docstring")) if sym.get("docstring") else "",
            "members": [],
        }
        parent = by_name.get(sym.get("parent_name") or "")
        if parent is not None:
            parent["members"].append(entry)
            continue
        entries.append(entry)
        if not sym.get("parent_name"):
            by_name[sym["name"]] = entry
    return entries


def with_subject(sentences: Iterable[str], first: str, rest: str) -> list[str]:
    """Fill each sentence's ``{subject}`` slot: *first* once, *rest* after.

    ``str.replace`` rather than ``format``: the sentences already carry file
    paths and signatures, whose braces ``format`` would read as fields.
    """
    return [s.replace("{subject}", first if i == 0 else rest) for i, s in enumerate(sentences)]
