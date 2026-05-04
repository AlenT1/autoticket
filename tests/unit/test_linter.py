"""Tests for the post-hoc fix-language linter."""

from __future__ import annotations

import pytest

from file_to_jira.enrich.linter import lint_description


def test_strip_mode_removes_fix_phrases() -> None:
    text = (
        "The endpoint returns 201, expected 403.\n"
        "We should add a Depends(_require_admin) guard.\n"
        "The route handler does not call _require_admin.\n"
    )
    out = lint_description(text, mode="strip")
    assert "We should add" not in out.cleaned
    assert "endpoint returns 201" in out.cleaned
    assert "route handler does not call" in out.cleaned
    assert len(out.stripped_lines) == 1


def test_keep_mode_preserves_text_but_flags() -> None:
    text = "we should change X to Y"
    out = lint_description(text, mode="keep")
    assert out.cleaned == text
    assert out.flagged_lines == [text]
    assert out.stripped_lines == []


def test_code_fences_are_skipped() -> None:
    """Lines inside ``` fences must be preserved verbatim, even when offending."""
    text = (
        "Real prose with no offence.\n"
        "```python\n"
        "# fix: this is in a code snippet, do not strip\n"
        "value = should_be_kept()\n"
        "```\n"
        "We should add validation here.\n"
    )
    out = lint_description(text, mode="strip")
    assert "fix: this is in a code snippet" in out.cleaned
    assert "We should add validation" not in out.cleaned


@pytest.mark.parametrize(
    "phrase",
    [
        "We should add a check here.",
        "The fix is to wrap it.",
        "Change foo to bar.",
        "I recommend adding a Depends(_require_admin) guard.",
        "Refactor this to use the new helper.",
        "Set self.flag = True at the top.",
    ],
)
def test_strip_catches_known_phrases(phrase: str) -> None:
    out = lint_description(phrase, mode="strip")
    assert out.cleaned.strip() == ""
    assert out.stripped_lines == [phrase]


def test_observational_language_is_not_flagged() -> None:
    text = (
        "The handler returns 201 instead of 403.\n"
        "The middleware was added in commit b7801f5.\n"
        "Tests CORE-CHAT-031 and CORE-MOBILE-001 fail with the same error.\n"
    )
    out = lint_description(text, mode="strip")
    assert out.cleaned == text
    assert out.stripped_lines == []
    assert out.flagged_lines == []


def test_unknown_mode_raises() -> None:
    with pytest.raises(ValueError):
        lint_description("hi", mode="bogus")
