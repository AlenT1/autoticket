"""Unit tests for pure helpers — no I/O, no LLM, no Jira."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from jira_task_agent.jira.client import JiraClient
from jira_task_agent.pipeline.dedupe import (
    _normalize as dedupe_normalize,
    find_duplicate_copies,
)
from jira_task_agent.pipeline.extractor import (
    AGENT_MARKER,
    _clean_assignee,
    _ensure_marker,
    _inject_co_owners,
    _split_co_owners,
)


# ----------------------------------------------------------------------
# extractor helpers
# ----------------------------------------------------------------------


class TestCleanAssignee:
    def test_strips_trailing_paren_annotation(self):
        assert _clean_assignee("Guy (50% for first 6 work days)") == "Guy"

    def test_strips_trailing_bracket_annotation(self):
        assert _clean_assignee("Evgeny [on PTO]") == "Evgeny"

    def test_strips_repeated_annotations(self):
        assert _clean_assignee("Saar (50%) (lead)") == "Saar"

    def test_preserves_composites(self):
        assert _clean_assignee("Lior + Aviv") == "Lior + Aviv"
        assert _clean_assignee("Nick/Joe") == "Nick/Joe"

    def test_returns_none_for_empty(self):
        assert _clean_assignee("") is None
        assert _clean_assignee(None) is None
        assert _clean_assignee("   ") is None


class TestSplitCoOwners:
    def test_single_owner(self):
        assert _split_co_owners("Sharon") == ("Sharon", [])

    def test_plus_separator(self):
        assert _split_co_owners("Lior + Aviv") == ("Lior", ["Aviv"])

    def test_slash_separator(self):
        assert _split_co_owners("Nick/Joe") == ("Nick", ["Joe"])

    def test_mixed_separators(self):
        # "Nick/Joe + Guy" -> first is "Nick", others are ["Joe", "Guy"]
        assert _split_co_owners("Nick/Joe + Guy") == ("Nick", ["Joe", "Guy"])

    def test_three_plus(self):
        assert _split_co_owners("Lior + Aviv + Guy") == ("Lior", ["Aviv", "Guy"])

    def test_strips_paren_annotations_first(self):
        # "Guy (50%) + Sharon" -> ("Guy + Sharon" after clean) -> ("Guy", ["Sharon"])
        first, others = _split_co_owners("Guy (50%) + Sharon")
        assert first == "Guy"
        assert others == ["Sharon"]

    def test_empty_returns_none(self):
        assert _split_co_owners("") == (None, [])
        assert _split_co_owners(None) == (None, [])


class TestInjectCoOwners:
    def test_no_op_for_single_owner(self):
        desc = f"Body.\n\n{AGENT_MARKER}"
        assert _inject_co_owners(desc, "Sharon") == desc

    def test_no_op_for_empty_owner(self):
        desc = f"Body.\n\n{AGENT_MARKER}"
        assert _inject_co_owners(desc, "") == desc
        assert _inject_co_owners(desc, None) == desc

    def test_inserts_before_marker(self):
        desc = f"Body.\n\n{AGENT_MARKER}"
        out = _inject_co_owners(desc, "Lior + Aviv")
        assert "Co-owners: Aviv" in out
        # marker must still be at the end
        assert out.rstrip().endswith(AGENT_MARKER)
        assert out.index("Co-owners:") < out.index(AGENT_MARKER)

    def test_appends_when_no_marker(self):
        desc = "Body."
        out = _inject_co_owners(desc, "Nick/Joe + Guy")
        assert out.endswith("Co-owners: Joe, Guy")

    def test_lists_all_others_comma_separated(self):
        desc = f"Body.\n\n{AGENT_MARKER}"
        out = _inject_co_owners(desc, "Lior + Aviv + Guy")
        assert "Co-owners: Aviv, Guy" in out


class TestEnsureMarker:
    def test_adds_marker_when_missing(self):
        assert _ensure_marker("Body").endswith(AGENT_MARKER)

    def test_idempotent_when_already_present(self):
        already = f"Body\n\n{AGENT_MARKER}"
        assert _ensure_marker(already) == already

    def test_handles_empty(self):
        assert _ensure_marker("") == AGENT_MARKER


# ----------------------------------------------------------------------
# Markdown -> Jira wiki conversion (jira/client.py)
# ----------------------------------------------------------------------


class TestMdToJiraWiki:
    """JiraClient._md_to_jira_wiki — staticmethod, callable without an instance."""

    def _convert(self, text: str) -> str:
        return JiraClient._md_to_jira_wiki(text)

    def test_h1_h2_h3(self):
        assert self._convert("# Title") == "h1. Title"
        assert self._convert("## Section") == "h2. Section"
        assert self._convert("### Subsection") == "h3. Subsection"

    def test_h3_in_context_among_paragraphs(self):
        src = "Intro.\n\n### Definition of Done\n- [ ] Item\n"
        out = self._convert(src)
        assert "h3. Definition of Done" in out
        assert "* (x) Item" in out

    def test_unchecked_checkbox_to_x_icon(self):
        out = self._convert("- [ ] Task A")
        assert out == "* (x) Task A"

    def test_checked_checkbox_to_green_icon(self):
        out = self._convert("- [x] Done thing")
        assert out == "* (/) Done thing"

    def test_plain_dash_bullet_to_star(self):
        out = self._convert("- Plain bullet")
        assert out == "* Plain bullet"

    def test_inline_code(self):
        assert self._convert("call `update_issue` now") == "call {{update_issue}} now"

    def test_bold(self):
        assert self._convert("note **important** detail") == "note *important* detail"

    def test_link(self):
        out = self._convert("see [the doc](https://example.com/doc)")
        assert out == "see [the doc|https://example.com/doc]"

    def test_fenced_code_with_language(self):
        src = "before\n```python\nprint('x')\n```\nafter"
        out = self._convert(src)
        assert "{code:python}" in out
        assert "print('x')" in out
        assert "{code}" in out
        # ensure the language wasn't double-applied
        assert "```python" not in out

    def test_marker_line_passes_through(self):
        # The marker is an HTML comment Jira shows verbatim; we don't
        # want it to be transformed (it's the manual-edit detection key).
        src = AGENT_MARKER
        assert AGENT_MARKER in self._convert(src)

    def test_empty_string_passthrough(self):
        assert self._convert("") == ""
        assert self._convert(None) is None


# ----------------------------------------------------------------------
# Drive dedupe (pipeline/dedupe.py)
# ----------------------------------------------------------------------


def _make_drive_file(
    *,
    file_id: str,
    name: str,
    mime: str = "text/markdown",
    modified: str = "2026-04-27T10:00:00+00:00",
):
    """Build a minimal DriveFile-shaped object for dedupe tests."""
    from jira_task_agent.drive.client import DriveFile

    dt = datetime.fromisoformat(modified)
    return DriveFile(
        id=file_id,
        name=name,
        mime_type=mime,
        created_time=dt,
        modified_time=dt,
        size=None,
        creator_name=None,
        creator_email=None,
        last_modifying_user_name=None,
        last_modifying_user_email=None,
        parents=[],
        web_view_link=None,
    )


class TestDedupeNormalize:
    def test_strips_google_doc_backslash_escapes(self):
        a = "Title — Rev 5)"
        b = "Title — Rev 5\\)"
        assert dedupe_normalize(a) == dedupe_normalize(b)

    def test_normalizes_table_alignment(self):
        a = "| col |\n|---|"
        b = "| col |\n| :---- |"
        assert dedupe_normalize(a) == dedupe_normalize(b)

    def test_collapses_whitespace(self):
        a = "Two  spaces"
        b = "Two\t\tspaces"
        assert dedupe_normalize(a) == dedupe_normalize(b)


class TestFindDuplicateCopies:
    def test_returns_empty_when_no_dupes(self, tmp_path):
        f1 = _make_drive_file(file_id="A", name="a.md")
        p1 = tmp_path / "a.md"
        p1.write_text("a" * 250)
        result = find_duplicate_copies([f1], {f1.id: p1})
        assert result == {}

    def test_detects_md_and_gdoc_twin(self, tmp_path):
        # Same content — one is the markdown upload, one is the
        # Google-Docs export with backslash escapes.
        md_content = "Title — Rev 5)\n\n## Section\n\n| col |\n|---|\n| v |\n" + "x" * 200
        gdoc_content = "Title — Rev 5\\)\n\n## Section\n\n| col |\n| :---- |\n| v |\n" + "x" * 200

        md = _make_drive_file(
            file_id="MD",
            name="May1_Tasks.md",
            mime="text/markdown",
            modified="2026-04-27T10:00:00+00:00",
        )
        gdoc = _make_drive_file(
            file_id="GD",
            name="May1_Tasks.md",
            mime="application/vnd.google-apps.document",
            modified="2026-04-27T11:00:00+00:00",  # newer
        )

        p_md = tmp_path / "md.md"
        p_md.write_text(md_content)
        p_gdoc = tmp_path / "gdoc.md"
        p_gdoc.write_text(gdoc_content)

        result = find_duplicate_copies([md, gdoc], {md.id: p_md, gdoc.id: p_gdoc})
        # Markdown upload should win as canonical; gdoc export is the duplicate
        assert result == {"GD": "MD"}

    def test_short_files_are_not_considered_duplicates(self, tmp_path):
        """Files with normalized content < 200 chars are skipped (too small to
        be confidently duplicates)."""
        a = _make_drive_file(file_id="A", name="a.md")
        b = _make_drive_file(file_id="B", name="a.md")
        p1 = tmp_path / "a.md"
        p1.write_text("tiny")
        p2 = tmp_path / "b.md"
        p2.write_text("tiny")
        assert find_duplicate_copies([a, b], {a.id: p1, b.id: p2}) == {}
