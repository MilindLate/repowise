"""What a signature change does to a caller, decided from the two indexed signatures."""

from __future__ import annotations

import pytest

from repowise.core.analysis.signature_effect import (
    EFFECT_BREAKING,
    EFFECT_COMPATIBLE,
    EFFECT_NONE,
    EFFECT_UNKNOWN,
    classify_signature_change,
)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # Reflowed across lines, as an indexed multi-line signature carries it.
        ("def run(self, x, y) -> int", "def run(\n    self,\n    x,\n    y\n) -> int"),
        # Trailing comma.
        ("def run(x, y)", "def run(x, y,)"),
        # A receiver appearing: a function became a method, callers pass the same.
        ("def run(x)", "def run(self, x)"),
    ],
)
def test_text_only_changes_have_no_effect(before, after):
    assert classify_signature_change(before, after).effect == EFFECT_NONE


@pytest.mark.parametrize(
    ("before", "after", "reason"),
    [
        ("def run(x)", "def run(x, y=None)", "added optional `y`"),
        ("function run(a: string)", "function run(a: string, b?: number)", "added optional `b`"),
        ("def run(x, y)", "def run(x, y=1)", "`y` is now optional"),
        ("def run(x)", "def run(x: int)", "annotated `x`"),
        ("def run(x) -> int", "def run(x) -> str", "changed its return annotation"),
    ],
)
def test_changes_every_existing_call_survives_are_compatible(before, after, reason):
    effect = classify_signature_change(before, after)
    assert effect.effect == EFFECT_COMPATIBLE
    assert effect.reason == reason


@pytest.mark.parametrize(
    ("before", "after", "reason"),
    [
        ("def run(x, y)", "def run(x)", "removed the required `y`"),
        ("def run(x)", "def run(x, y)", "added the required `y`"),
        ("def run(x, y=1)", "def run(x, y)", "`y` is now required"),
        ("def run(x, y)", "def run(y, x)", "reordered its parameters"),
        ("def run(x, y)", "def run(x, z)", "renamed `y` to `z`"),
        ("def run(x)", "async def run(x)", "became async, so its callers' await changes"),
    ],
)
def test_changes_that_can_break_a_call_are_breaking(before, after, reason):
    effect = classify_signature_change(before, after)
    assert effect.effect == EFFECT_BREAKING
    assert effect.reason == reason


def test_an_unbalanced_parameter_list_is_unknown_not_safe():
    effect = classify_signature_change("def run(x, y)", "def run(x, y: Dict[str, int)")
    assert effect.effect == EFFECT_UNKNOWN


def test_a_go_method_compares_its_parameters_not_its_receiver():
    before = "func (s *Server) Handle(w Writer) -> error"
    after = "func (s *Server) Handle(w Writer, r *Request) -> error"
    effect = classify_signature_change(before, after)
    assert effect.effect == EFFECT_BREAKING
    assert effect.reason == "added the required `r *Request`"


def test_an_arrow_type_does_not_unbalance_the_list():
    before = "function run(cb: (a: number) => void)"
    after = "function run(cb: (a: number) => void, opts?: Options)"
    assert classify_signature_change(before, after).effect == EFFECT_COMPATIBLE


def test_a_dropped_base_class_breaks_and_an_added_one_does_not():
    assert classify_signature_change("class A(B, C)", "class A(B)").effect == EFFECT_BREAKING
    assert classify_signature_change("class A(B)", "class A(B, C)").effect == EFFECT_COMPATIBLE


def test_a_comment_inside_the_signature_does_not_make_it_unparseable():
    before = "function Drawer({\n  row,\n}: {\n  /** The drawer's row. */\n  row: Row;\n})"
    after = (
        "function Drawer({\n  row,\n  onClose,\n}: {\n  /** The drawer's row. */\n"
        "  row: Row;\n  onClose?: () => void;\n})"
    )
    assert classify_signature_change(before, after).effect == EFFECT_COMPATIBLE


def test_a_keyword_only_marker_is_named_not_counted_as_a_parameter():
    added = classify_signature_change("def run(a, b=1)", "def run(a, *, b=1)")
    assert added.effect == EFFECT_BREAKING
    assert added.reason == "made its later parameters keyword-only"
    removed = classify_signature_change("def run(a, *, b=1)", "def run(a, b=1)")
    assert removed.effect == EFFECT_COMPATIBLE
