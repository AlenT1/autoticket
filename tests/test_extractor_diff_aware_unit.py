"""Unit tests for extract_diff_aware. Stubs the LLM `chat` call so no
network is involved. Covers parsing, validation, and error fallback."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from jira_task_agent.drive.client import DriveFile
from jira_task_agent.pipeline import extractor
from jira_task_agent.pipeline.extractor import (
    AGENT_MARKER,
    DiffExtractionResult,
    ExtractionError,
    extract_diff_aware,
)


def _df() -> DriveFile:
    return DriveFile(
        id="F1",
        name="V11_Dashboard_Tasks.md",
        mime_type="text/markdown",
        created_time=datetime.now(timezone.utc),
        modified_time=datetime.now(timezone.utc),
        size=100,
        creator_name="Aviv R",
        creator_email=None,
        last_modifying_user_name="Aviv R",
        last_modifying_user_email=None,
        parents=[],
        web_view_link="http://drive/F1",
    )


def _local_file(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "f1.md"
    p.write_text(content, encoding="utf-8")
    return p


def _stub_chat(monkeypatch, response: dict | object):
    """Patch extractor.chat to return `response` (dict) on every call.
    If `response` is an Exception, it's raised."""
    def _fake(**_kwargs):
        if isinstance(response, Exception):
            raise response
        return (response, {"model": "stub"})
    monkeypatch.setattr(extractor, "chat", _fake)


def _task(summary: str, anchor: str) -> dict:
    return {
        "summary": summary,
        "description": (
            f"{summary} body.\n\n### Definition of Done\n"
            "- [ ] one\n- [ ] two\n- [ ] three\n"
        ),
        "source_anchor": anchor,
        "assignee": None,
    }


# ----------------------------------------------------------------------
# happy paths
# ----------------------------------------------------------------------


def test_no_changed_chunks_short_circuits(tmp_path, monkeypatch):
    """Defensive: caller shouldn't pass empty changed_chunk_ids, but
    if they do we return an empty diff result without an LLM call."""
    called = {"n": 0}
    def _fake(**_):
        called["n"] += 1
        return ({}, {"model": "stub"})
    monkeypatch.setattr(extractor, "chat", _fake)

    result = extract_diff_aware(
        _df(),
        local_path=_local_file(tmp_path, "# Title\n## A\nbody"),
        root_context="",
        cached_extraction=None,
        changed_chunk_ids=[],
    )
    assert called["n"] == 0
    assert result.epic_changed is False
    assert result.epic is None
    assert result.tasks == []


def test_epic_unchanged_some_tasks_returned(tmp_path, monkeypatch):
    _stub_chat(monkeypatch, {
        "epic_changed": False,
        "epic": None,
        "tasks": [_task("Edited dashboard task", "V11-1 Edited dashboard")],
    })
    result = extract_diff_aware(
        _df(),
        local_path=_local_file(tmp_path, "# T\n## A\nbody"),
        root_context="root",
        cached_extraction={"type": "single_epic", "tasks": []},
        changed_chunk_ids=["A|0"],
    )
    assert result.epic_changed is False
    assert result.epic is None
    assert len(result.tasks) == 1
    assert result.tasks[0].source_anchor.startswith("V11-1")
    # Marker must be appended by validation.
    assert AGENT_MARKER in result.tasks[0].description


def test_epic_changed_returns_full_epic(tmp_path, monkeypatch):
    _stub_chat(monkeypatch, {
        "epic_changed": True,
        "epic": {
            "summary": "Dashboard improvements",
            "description": "Updated overview body.",
            "assignee": None,
        },
        "tasks": [],
    })
    result = extract_diff_aware(
        _df(),
        local_path=_local_file(tmp_path, "# T\n## A\nbody"),
        root_context="root",
        cached_extraction={"type": "single_epic"},
        changed_chunk_ids=["overview|0"],
    )
    assert result.epic_changed is True
    assert result.epic is not None
    assert result.epic.summary == "Dashboard improvements"
    assert AGENT_MARKER in result.epic.description


def test_zero_changes_returned(tmp_path, monkeypatch):
    """Changed chunk was a context section with no actionable items.
    LLM correctly returns empty tasks."""
    _stub_chat(monkeypatch, {
        "epic_changed": False,
        "epic": None,
        "tasks": [],
    })
    result = extract_diff_aware(
        _df(),
        local_path=_local_file(tmp_path, "# T\n## Risks\nNew risk note."),
        root_context="",
        cached_extraction={"type": "single_epic"},
        changed_chunk_ids=["Risks|0"],
    )
    assert result.epic_changed is False
    assert result.tasks == []


# ----------------------------------------------------------------------
# error paths
# ----------------------------------------------------------------------


def test_non_dict_response_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(
        extractor, "chat",
        lambda **_: (["not a dict"], {"model": "stub"}),
    )
    with pytest.raises(ExtractionError, match="JSON object"):
        extract_diff_aware(
            _df(),
            local_path=_local_file(tmp_path, "# T"),
            root_context="",
            cached_extraction=None,
            changed_chunk_ids=["A|0"],
        )


def test_tasks_not_a_list_raises(tmp_path, monkeypatch):
    _stub_chat(monkeypatch, {"epic_changed": False, "epic": None, "tasks": "oops"})
    with pytest.raises(ExtractionError, match="'tasks' is not a list"):
        extract_diff_aware(
            _df(),
            local_path=_local_file(tmp_path, "# T"),
            root_context="",
            cached_extraction=None,
            changed_chunk_ids=["A|0"],
        )


def test_epic_changed_true_but_no_epic_object_raises(tmp_path, monkeypatch):
    _stub_chat(monkeypatch, {
        "epic_changed": True,
        "epic": None,
        "tasks": [],
    })
    with pytest.raises(ExtractionError, match="did not return an 'epic' object"):
        extract_diff_aware(
            _df(),
            local_path=_local_file(tmp_path, "# T"),
            root_context="",
            cached_extraction=None,
            changed_chunk_ids=["overview|0"],
        )


def test_task_not_an_object_raises(tmp_path, monkeypatch):
    _stub_chat(monkeypatch, {
        "epic_changed": False,
        "epic": None,
        "tasks": ["not a dict"],
    })
    with pytest.raises(ExtractionError, match="not an object"):
        extract_diff_aware(
            _df(),
            local_path=_local_file(tmp_path, "# T"),
            root_context="",
            cached_extraction=None,
            changed_chunk_ids=["A|0"],
        )


def test_invalid_task_summary_raises(tmp_path, monkeypatch):
    """Task summary too short triggers _validate_summary."""
    _stub_chat(monkeypatch, {
        "epic_changed": False,
        "epic": None,
        "tasks": [{
            "summary": "x",  # too short
            "description": "body\n\n### Definition of Done\n- [ ] a\n- [ ] b\n- [ ] c\n",
            "source_anchor": "A-1",
        }],
    })
    with pytest.raises(ExtractionError):
        extract_diff_aware(
            _df(),
            local_path=_local_file(tmp_path, "# T"),
            root_context="",
            cached_extraction=None,
            changed_chunk_ids=["A|0"],
        )


# ----------------------------------------------------------------------
# input serializer (compact representation of cached extraction)
# ----------------------------------------------------------------------


def test_serialize_cached_handles_none():
    out = extractor._serialize_cached_for_prompt(None)
    assert out == "{}"


def test_serialize_cached_single_epic():
    cached = {
        "type": "single_epic",
        "epic": {"summary": "S", "description": "long body…"},
        "tasks": [
            {"summary": "T1", "description": "body", "source_anchor": "a1"},
        ],
    }
    out = extractor._serialize_cached_for_prompt(cached)
    assert "T1" in out
    assert "a1" in out
    # body should NOT be inlined (we ship summary + anchor only)
    assert "long body" not in out


def test_serialize_cached_multi_epic():
    cached = {
        "type": "multi_epic",
        "epics": [
            {"summary": "E1", "tasks": [{"summary": "T1", "source_anchor": "a1"}]},
            {"summary": "E2", "tasks": []},
        ],
    }
    out = extractor._serialize_cached_for_prompt(cached)
    assert "E1" in out
    assert "E2" in out
    assert "a1" in out
