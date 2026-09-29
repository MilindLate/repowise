"""No repo from the precision corpus is named in shipped source (rule R2).

A fix that only works because the code knows it is looking at Composio (or
trpc, or petclinic) raises that repo's score and nothing else, and the harness
cannot tell it from a real improvement. So no string literal under
``packages/*/src`` may name a corpus repo or a path inside one.

Tokens come from ``scripts/kg_validate/matrix.toml`` (repo names and the
``owner/repo`` of URL sources) plus ``extra`` in ``corpus_special_casing.toml``.
Matching is case-insensitive and whole-word, with ``-`` and ``_`` spellings
equivalent. Names that are ordinary words or real library names (``click``,
``args``, ``django``) are listed as ``ordinary`` there and count only when
owner-qualified or used as a middle path segment. Legitimate hits go in its
``[[allow]]`` table with a reason.

Only literals that can change behaviour are scanned: docstrings and other bare
string statements are prose, like comments, and may cite a repo as an example.

Ceiling: Python only (``ast``). The TypeScript packages under ``packages/*/src``
are not scanned; a grep of them found no corpus tokens when this was written.
"""

from __future__ import annotations

import ast
import re
import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = tomllib.loads((Path(__file__).with_name("corpus_special_casing.toml")).read_text())
MATRIX = tomllib.loads((REPO_ROOT / "scripts/kg_validate/matrix.toml").read_text())
SOURCE_GLOB = "packages/*/src/**/*.py"


def _word(token: str) -> str:
    # Boundaries only where the token itself starts/ends alphanumeric, so a
    # path token like ``ts/packages/`` still matches ``ts/packages/core``.
    left = r"(?<![a-z0-9])" if token[0].isalnum() else ""
    right = r"(?![a-z0-9])" if token[-1].isalnum() else ""
    return left + re.escape(token) + right


def _pattern(token: str, *, segment: bool = False) -> re.Pattern[str]:
    """Any ``-``/``_`` spelling of ``token``; ``segment`` = only as ``/token/``."""
    token = token.lower()
    spellings = sorted({token, token.replace("-", "_"), token.replace("_", "-")})
    parts = [f"/{re.escape(s)}/" if segment else _word(s) for s in spellings]
    return re.compile("|".join(parts))


def build_patterns() -> dict[str, re.Pattern[str]]:
    ordinary = set(CONFIG["ordinary"])
    patterns: dict[str, re.Pattern[str]] = {}
    for name, spec in MATRIX.items():
        source = spec["source"]
        if source.startswith(("http://", "https://")):
            owner_repo = "/".join(source.rstrip("/").removesuffix(".git").split("/")[-2:])
            patterns[owner_repo.lower()] = _pattern(owner_repo)
        patterns[name] = _pattern(name, segment=name in ordinary)
    for token in CONFIG["extra"]:
        patterns[token] = _pattern(token)
    return patterns


def _prose_ids(tree: ast.AST) -> set[int]:
    """Docstrings and other bare string statements: prose, not behaviour."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }


def _source_files(root: Path) -> list[Path]:
    """Committed sources only: a local scratch file is not shipped code."""
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--", SOURCE_GLOB],
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:  # not a git checkout (e.g. an sdist)
        return sorted(root.glob(SOURCE_GLOB))
    return sorted(root / line for line in listed.stdout.splitlines() if line)


def scan(root: Path, patterns: dict[str, re.Pattern[str]]) -> list[tuple[str, int, str, str]]:
    hits = []
    for path in _source_files(root):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=rel)
        prose = _prose_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in prose
            ):
                text = node.value.lower()
                for token, pattern in patterns.items():
                    if pattern.search(text):
                        hits.append((rel, node.lineno, token, node.value[:80]))
    return hits


def test_ordinary_names_are_matrix_repos():
    # A stale entry would silently weaken matching for a name nobody uses.
    assert set(CONFIG["ordinary"]) <= set(MATRIX), set(CONFIG["ordinary"]) - set(MATRIX)


def test_every_allow_entry_has_a_reason():
    for entry in CONFIG.get("allow", []):
        assert entry.get("reason", "").strip(), entry


def test_patterns_catch_special_casing_and_spare_ordinary_words():
    patterns = build_patterns()

    def matched(text: str) -> set[str]:
        return {t for t, p in patterns.items() if p.search(text.lower())}

    assert "composio" in matched("composio_openai")
    assert "ts/packages/" in matched("ts/packages/core/src/index.ts")
    assert "mini-taskq" in matched("mini_taskq")
    assert "pallets/click" in matched("https://github.com/pallets/click")
    assert "click" in matched("src/click/core.py")
    assert not matched("args")
    assert not matched("click here to continue")
    assert not matched("script/task")
    assert not matched("django")


def test_no_corpus_names_in_source_literals():
    allowed = {(a["path"], a["token"]) for a in CONFIG.get("allow", [])}
    hits = scan(REPO_ROOT, build_patterns())
    offending = [h for h in hits if (h[0], h[2]) not in allowed]
    assert not offending, (
        "string literals name a precision-corpus repo (R2: no special-casing). Make the "
        "rule general, or add a justified [[allow]] entry to corpus_special_casing.toml:\n"
        + "\n".join(f"  {p}:{line}  {tok!r} in {text!r}" for p, line, tok, text in offending)
    )
    unused = allowed - {(h[0], h[2]) for h in hits}
    assert not unused, f"allowlist entries that no longer match anything, remove them: {unused}"
