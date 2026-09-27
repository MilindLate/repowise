"""What a signature change does to an existing caller.

Comparing two signature strings says only *that* a signature changed. A
reflowed parameter list, a trailing comma, a receiver appearing and an appended
optional argument all differ as text, and none of them can break a caller. This
module parses both parameter lists and asks the narrower question: could a call
site written against the old signature stop working?

It is conservative in one direction only. A form it cannot parse is reported as
``unknown``, which consumers treat as breaking: not knowing is not the same as
knowing it is safe.

Pure string work over the indexed signatures. No source, no graph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Values for :attr:`SignatureEffect.effect`, loosest to strictest.
EFFECT_NONE = "none"  # same contract, different text
EFFECT_COMPATIBLE = "compatible"  # changed, but every existing call still works
EFFECT_BREAKING = "breaking"  # an existing call can stop working
EFFECT_UNKNOWN = "unknown"  # unparseable on at least one side

# A method is called without its receiver, so it is dropped from both sides:
# turning a function into a method is not an added required parameter.
_RECEIVERS = frozenset({"self", "cls"})

# Brackets that must balance before a comma separates parameters. ``<`` covers
# generics (``Map<string, number>``); a stray one degrades to ``unknown``.
_OPENERS = {"(": ")", "[": "]", "{": "}", "<": ">"}
_CLOSERS = {v: k for k, v in _OPENERS.items()}

_ASYNC = re.compile(r"\basync\b")
# ``class Foo(Bar)`` puts base classes where a function puts parameters.
_CLASS = re.compile(
    r"^\s*(?:export\s+|public\s+|private\s+|abstract\s+|final\s+)*"
    r"(?:class|interface|trait|struct|enum)\b"
)
# A Go method signature leads with its receiver: ``func (r *T) Name(a int)``.
_GO_RECEIVER = re.compile(r"^\s*func\s*\(")
# Comments inside a multi-line signature. An apostrophe in one ("the drawer's
# row") would otherwise open a quote that never closes. Line comments are only
# stripped when they own the line, so ``"http://x"`` in a default survives.
_COMMENTS = re.compile(r"(?s:/\*.*?\*/)|^[ \t]*(?://|#).*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class SignatureEffect:
    """The verdict, and the specific difference behind it."""

    effect: str
    reason: str


@dataclass(frozen=True, slots=True)
class _Param:
    name: str
    has_default: bool = False
    #: TypeScript's ``x?: T``: same caller-visible effect as a default.
    optional: bool = False
    #: ``*args`` / ``**kwargs`` / ``...rest``.
    variadic: bool = False
    #: Explains a verdict, never decides one: an annotation is not a runtime contract.
    annotation: str = ""

    @property
    def required(self) -> bool:
        return not (self.has_default or self.optional or self.variadic)


def _normalize(signature: str) -> str:
    return re.sub(r"\s+", " ", _COMMENTS.sub(" ", signature or "")).strip()


def _split_params(raw: str) -> list[str] | None:
    """Split on top-level commas, or ``None`` when brackets or quotes do not balance."""
    parts: list[str] = []
    depth: list[str] = []
    quote: str | None = None
    current: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if quote is not None:
            current.append(ch)
            if ch == "\\" and i + 1 < len(raw):
                current.append(raw[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            current.append(ch)
        elif ch in _OPENERS:
            # A ``<`` with no ``>`` ahead is a comparison in a default value.
            if ch != "<" or ">" in raw[i:]:
                depth.append(ch)
            current.append(ch)
        elif ch == ">" and raw[i - 1 : i] in ("=", "-"):
            current.append(ch)  # an arrow, not a generic close
        elif ch in _CLOSERS:
            if depth and depth[-1] == _CLOSERS[ch]:
                depth.pop()
            elif ch != ">":
                return None
            current.append(ch)
        elif ch == "," and not depth:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    if depth or quote is not None:
        return None
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _top_level_index(text: str, needle: str) -> int | None:
    """Index of ``needle`` at bracket depth zero, ignoring ``==``, ``=>`` and the like."""
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS and not (ch == ">" and text[i - 1 : i] in ("=", "-")):
            depth -= 1
        elif depth == 0 and ch == needle:
            if needle == "=" and (
                text[i - 1 : i] in ("=", "!", "<", ">", ":") or text[i + 1 : i + 2] in ("=", ">")
            ):
                i += 1
                continue
            return i
        i += 1
    return None


def _parse_param(raw: str, position: int) -> _Param:
    text = raw.strip()
    variadic = False
    if text.startswith(("**", "...")):
        variadic = True
        text = text.lstrip("*.").strip()
    elif text.startswith("*"):
        text = text.lstrip("*").strip()
        # A bare ``*`` is Python's keyword-only marker: nameless, but its
        # position is a real contract, so it stays visible.
        if not text:
            return _Param(name="*")
        variadic = True

    has_default = False
    default_at = _top_level_index(text, "=")
    if default_at is not None:
        has_default = True
        text = text[:default_at].strip()

    annotation = ""
    ann_at = _top_level_index(text, ":")
    if ann_at is not None:
        annotation = _normalize(text[ann_at + 1 :])
        text = text[:ann_at].strip()

    # A destructured parameter has no caller-visible name, so it is keyed by
    # position: reading one more field out of it is not a contract change.
    if text.startswith(("{", "[")):
        return _Param(
            name=f"<destructured:{position}>",
            has_default=has_default,
            variadic=variadic,
            annotation=annotation,
        )
    return _Param(
        name=text.rstrip("?").strip() or f"<anonymous:{position}>",
        has_default=has_default,
        optional=text.endswith("?"),
        variadic=variadic,
        annotation=annotation,
    )


def _param_span(signature: str) -> tuple[int, int] | None:
    """``(open, close)`` of the parameter list's parentheses, or ``None``."""
    start = 0
    if _GO_RECEIVER.match(signature):
        receiver = _param_span_from(signature, signature.find("("))
        if receiver is None:
            return None
        start = receiver[1] + 1
    open_at = signature.find("(", start)
    return None if open_at < 0 else _param_span_from(signature, open_at)


def _param_span_from(signature: str, open_at: int) -> tuple[int, int] | None:
    depth = 0
    for i in range(open_at, len(signature)):
        if signature[i] == "(":
            depth += 1
        elif signature[i] == ")":
            depth -= 1
            if depth == 0:
                return open_at, i
    return None


def _param_list(signature: str) -> list[_Param] | None:
    span = _param_span(signature)
    if span is None:
        return None
    parts = _split_params(signature[span[0] + 1 : span[1]])
    if parts is None:
        return None
    params = [_parse_param(p, n) for n, p in enumerate(parts)]
    return [p for p in params if p.name not in _RECEIVERS]


def _return_type(signature: str) -> str:
    """Whatever follows the parameter list. Explains a verdict; a caller passes no return type."""
    span = _param_span(signature)
    if span is None:
        return ""
    return re.sub(r"^(->|:)\s*", "", signature[span[1] + 1 :].strip()).strip()


def classify_signature_change(before: str, after: str) -> SignatureEffect:
    """Compare two indexed signatures of the same symbol, from a caller's side."""
    lhs, rhs = _normalize(before), _normalize(after)
    if lhs == rhs:
        return SignatureEffect(EFFECT_NONE, "only whitespace changed")

    if _CLASS.match(lhs) or _CLASS.match(rhs):
        return _classify_class(lhs, rhs)

    # ``async`` is a real contract: a caller that did not await now gets a
    # coroutine, and one that did now awaits a plain value.
    was_async, is_async = bool(_ASYNC.search(lhs)), bool(_ASYNC.search(rhs))
    if was_async != is_async:
        verb = "became async" if is_async else "is no longer async"
        return SignatureEffect(EFFECT_BREAKING, f"{verb}, so its callers' await changes")

    before_params, after_params = _param_list(lhs), _param_list(rhs)
    if before_params is None or after_params is None:
        return SignatureEffect(EFFECT_UNKNOWN, "the parameter list could not be parsed")
    return _classify_params(before_params, after_params, _return_type(lhs), _return_type(rhs))


def _classify_class(lhs: str, rhs: str) -> SignatureEffect:
    before = [p.name for p in _param_list(lhs) or []]
    after = [p.name for p in _param_list(rhs) or []]
    dropped = [n for n in before if n not in after]
    added = [n for n in after if n not in before]
    if dropped:
        return SignatureEffect(EFFECT_BREAKING, f"no longer inherits from {_join(dropped)}")
    if added:
        return SignatureEffect(EFFECT_COMPATIBLE, f"now also inherits from {_join(added)}")
    return SignatureEffect(EFFECT_COMPATIBLE, "declaration changed without changing its bases")


def _classify_params(
    before: list[_Param], after: list[_Param], before_ret: str, after_ret: str
) -> SignatureEffect:
    before_by_name = {p.name: p for p in before}
    after_by_name = {p.name: p for p in after}
    has_variadic = any(p.variadic for p in after)

    breaking: list[str] = []
    compatible: list[str] = []

    removed = [p for p in before if p.name not in after_by_name]
    added = [p for p in after if p.name not in before_by_name]

    # One removal and one addition at the same index with the same requiredness
    # reads as a rename: still breaking for a keyword caller, but said plainly.
    if len(removed) == 1 and len(added) == 1:
        gone, arrived = removed[0], added[0]
        if (
            before.index(gone) == after.index(arrived)
            and gone.required == arrived.required
            and gone.variadic == arrived.variadic
        ):
            breaking.append(f"renamed `{gone.name}` to `{arrived.name}`")
            removed, added = [], []

    for p in removed:
        if p.name == "*":
            compatible.append("its keyword-only parameters now also accept positions")
        elif p.variadic and has_variadic:
            compatible.append(f"renamed the variadic `{p.name}`")
        elif p.required:
            breaking.append(f"removed the required `{p.name}`")
        else:
            breaking.append(f"removed `{p.name}`")

    for p in added:
        if p.name == "*":
            breaking.append("made its later parameters keyword-only")
        elif p.required:
            breaking.append(f"added the required `{p.name}`")
        else:
            compatible.append(f"added optional `{p.name}`")

    for name, old in before_by_name.items():
        new = after_by_name.get(name)
        if new is None:
            continue
        if old.required and not new.required:
            compatible.append(f"`{name}` is now optional")
        elif not old.required and new.required:
            breaking.append(f"`{name}` is now required")
        elif old.annotation != new.annotation:
            compatible.append(f"{'retyped' if old.annotation else 'annotated'} `{name}`")

    # Reordering what a caller passes positionally breaks even with the same set.
    common = [p.name for p in before if p.name in after_by_name]
    if common != [p.name for p in after if p.name in before_by_name]:
        breaking.append("reordered its parameters")

    if breaking:
        return SignatureEffect(EFFECT_BREAKING, _join(breaking))
    if before_ret != after_ret:
        compatible.append("changed its return annotation")
    if compatible:
        return SignatureEffect(EFFECT_COMPATIBLE, _join(compatible))
    # Same parameters, same return: a trailing comma, a reflow, a receiver.
    return SignatureEffect(EFFECT_NONE, "only formatting changed; the parameters are the same")


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" and {items[-1]}"
