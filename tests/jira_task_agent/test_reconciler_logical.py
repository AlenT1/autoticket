"""Logical tests for the reconciler + dirty filter.

The reconciler operates on a `list[DirtySection]` produced upstream
by `pipeline.dirty_filter.filter_dirty`. Body comparison is gone: every
dirty item produces a write action with the extracted body. These
tests cover the action-mapping contract for each input shape.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jira_task_agent.drive.client import DriveFile
from jira_task_agent.pipeline.dirty_filter import (
    DirtySection,
    DirtyTask,
    filter_dirty,
)
from jira_task_agent.pipeline.extractor import (
    AGENT_MARKER,
    ExtractedEpic,
    ExtractedEpicWithTasks,
    ExtractedTask,
    ExtractionResult,
    MultiExtractionResult,
)
from jira_task_agent.pipeline.matcher import (
    FileEpicResult,
    MatchDecision,
    MatcherResult,
)
from jira_task_agent.pipeline.reconciler import build_plans_from_dirty

from .conftest import MockJiraClient


def _drive_file(name: str = "F.md", file_id: str | None = None) -> DriveFile:
    return DriveFile(
        id=file_id or name,
        name=name,
        mime_type="text/markdown",
        created_time=datetime(2026, 4, 27, tzinfo=timezone.utc),
        modified_time=datetime(2026, 4, 27, tzinfo=timezone.utc),
        size=1000,
        creator_name=None, creator_email=None,
        last_modifying_user_name="Saar",
        last_modifying_user_email=None,
        parents=[],
        web_view_link="http://drive/F",
    )


def _task_desc(body: str = "body") -> str:
    return (
        f"{body}\n\n### Definition of Done\n"
        "- [ ] one\n- [ ] two\n- [ ] three\n\n"
        f"{AGENT_MARKER}"
    )


def _t(summary: str, anchor: str) -> ExtractedTask:
    return ExtractedTask(
        summary=summary, description=_task_desc(summary),
        source_anchor=anchor, assignee_name="Saar",
    )


def _section(
    file_id: str = "F1",
    file_name: str = "F.md",
    section_index: int = 0,
    role: str = "single_epic",
    matched_jira_key: str | None = None,
    epic_summary: str = "Demo epic title",
    epic_dirty: bool = True,
    tasks: list[DirtyTask] | None = None,
    orphan_keys: list[str] | None = None,
) -> DirtySection:
    return DirtySection(
        drive_file=_drive_file(file_name, file_id),
        file_id=file_id,
        file_name=file_name,
        section_index=section_index,
        role=role,
        matched_jira_key=matched_jira_key,
        epic_match_confidence=0.95 if matched_jira_key else 0.0,
        epic_match_reason="stub",
        extracted_epic_summary=epic_summary,
        extracted_epic_description=f"Epic body.\n\n{AGENT_MARKER}",
        extracted_epic_assignee_raw="Saar",
        epic_dirty=epic_dirty,
        tasks=tasks or [],
        orphan_keys=orphan_keys or [],
    )


def _dirty_task(summary: str, anchor: str, candidate_key: str | None) -> DirtyTask:
    return DirtyTask(
        extracted=_t(summary, anchor),
        decision=MatchDecision(
            item_index=0, candidate_key=candidate_key,
            confidence=0.95 if candidate_key else 0.0,
            reason="stub",
        ),
    )


def _open_status_client(
    matched_keys: list[str] = (),
    *,
    status: str = "Open",
) -> MockJiraClient:
    """MockJiraClient that returns matched epics in the given status."""
    issues = {
        k: {
            "id": "1", "key": k, "self": "(mock)",
            "fields": {
                "summary": f"{k} live summary",
                "description": "live description",
                "assignee": {"name": "sriftin", "displayName": "Saar"},
                "reporter": {"name": "sriftin", "displayName": "Saar"},
                "issuetype": {"name": "Epic"},
                "status": {"name": status},
                "labels": ["ai-generated"],
                "priority": None,
                "created": "2026-04-01T00:00:00.000+0000",
                "updated": "2026-04-27T00:00:00.000+0000",
            },
        }
        for k in matched_keys
    }
    return MockJiraClient(issues=issues)


# ----------------------------------------------------------------------
# build_plans_from_dirty — action mapping per dirty section
# ----------------------------------------------------------------------


def test_brand_new_epic_emits_create_epic_and_creates():
    section = _section(
        matched_jira_key=None,
        tasks=[
            _dirty_task("Task A", "a1", candidate_key=None),
            _dirty_task("Task B", "a2", candidate_key=None),
        ],
    )
    plans = build_plans_from_dirty([section], client=_open_status_client())

    assert len(plans) == 1
    group = plans[0].groups[0]
    assert group.epic_action.kind == "create_epic"
    assert group.epic_action.summary == "Demo epic title"
    assert {a.kind for a in group.task_actions} == {"create_task"}
    assert {a.summary for a in group.task_actions} == {"Task A", "Task B"}


def test_dirty_matched_epic_emits_update_epic_with_new_summary_and_description():
    section = _section(
        matched_jira_key="DEMO-100",
        epic_summary="New epic title",
        epic_dirty=True,
        tasks=[],
    )
    plans = build_plans_from_dirty(
        [section], client=_open_status_client(["DEMO-100"]),
    )

    epic_action = plans[0].groups[0].epic_action
    assert epic_action.kind == "update_epic"
    assert epic_action.target_key == "DEMO-100"
    assert epic_action.summary == "New epic title"
    assert epic_action.description.endswith(AGENT_MARKER)


def test_clean_matched_epic_with_dirty_tasks_emits_noop_epic():
    """When the epic body itself isn't dirty but its tasks are, the epic
    action is a noop (no Jira write) but its target_key flows down so
    dirty tasks can attach under it."""
    section = _section(
        matched_jira_key="DEMO-100",
        epic_dirty=False,
        tasks=[_dirty_task("Edited task", "a", candidate_key="DEMO-201")],
    )
    plans = build_plans_from_dirty(
        [section], client=_open_status_client(["DEMO-100"]),
    )
    group = plans[0].groups[0]
    assert group.epic_action.kind == "noop"
    assert group.epic_action.target_key == "DEMO-100"
    assert len(group.task_actions) == 1
    assert group.task_actions[0].kind == "update_task"
    assert group.task_actions[0].epic_key == "DEMO-100"


def test_completed_status_suppresses_task_actions():
    section = _section(
        matched_jira_key="DEMO-100",
        tasks=[_dirty_task("Task", "a1", candidate_key="DEMO-101")],
    )
    plans = build_plans_from_dirty(
        [section], client=_open_status_client(["DEMO-100"], status="Done"),
    )

    group = plans[0].groups[0]
    assert group.epic_action.kind == "skip_completed_epic"
    assert group.task_actions == []


def test_dirty_task_with_match_emits_update_task():
    section = _section(
        matched_jira_key="DEMO-100",
        tasks=[_dirty_task("UI fix", "ui-1", candidate_key="DEMO-201")],
    )
    plans = build_plans_from_dirty(
        [section], client=_open_status_client(["DEMO-100"]),
    )

    task_actions = plans[0].groups[0].task_actions
    assert len(task_actions) == 1
    a = task_actions[0]
    assert a.kind == "update_task"
    assert a.target_key == "DEMO-201"
    assert a.summary == "UI fix"


def test_dirty_task_without_match_emits_create_task():
    section = _section(
        matched_jira_key="DEMO-100",
        tasks=[_dirty_task("Brand new", "new-1", candidate_key=None)],
    )
    plans = build_plans_from_dirty(
        [section], client=_open_status_client(["DEMO-100"]),
    )

    task_actions = plans[0].groups[0].task_actions
    assert len(task_actions) == 1
    a = task_actions[0]
    assert a.kind == "create_task"
    assert a.summary == "Brand new"


def test_rollup_when_two_dirty_tasks_share_one_candidate_key():
    section = _section(
        matched_jira_key="DEMO-100",
        tasks=[
            _dirty_task("Sub-task A", "a", candidate_key="DEMO-201"),
            _dirty_task("Sub-task B", "b", candidate_key="DEMO-201"),
        ],
    )
    plans = build_plans_from_dirty(
        [section], client=_open_status_client(["DEMO-100"]),
    )

    kinds = [a.kind for a in plans[0].groups[0].task_actions]
    assert kinds == ["covered_by_rollup", "covered_by_rollup"]


def test_orphan_keys_surfaced_for_unconsumed_children():
    section = _section(
        matched_jira_key="DEMO-100",
        tasks=[_dirty_task("Task", "a", candidate_key="DEMO-201")],
        orphan_keys=["DEMO-202", "DEMO-203", "DEMO-201"],
    )
    plans = build_plans_from_dirty(
        [section], client=_open_status_client(["DEMO-100"]),
    )

    actions = plans[0].groups[0].task_actions
    orphans = [a for a in actions if a.kind == "orphan"]
    assert sorted(a.target_key for a in orphans) == ["DEMO-202", "DEMO-203"]


def test_multi_epic_two_sections_one_per_group():
    sections = [
        _section(
            file_id="F1", file_name="F.md", section_index=0,
            role="multi_epic",
            matched_jira_key="DEMO-100",
            epic_summary="Section A epic",
            tasks=[_dirty_task("A1", "a", candidate_key="DEMO-201")],
        ),
        _section(
            file_id="F1", file_name="F.md", section_index=1,
            role="multi_epic",
            matched_jira_key=None,
            epic_summary="Section B epic",
            tasks=[_dirty_task("B1", "b", candidate_key=None)],
        ),
    ]
    plans = build_plans_from_dirty(
        sections, client=_open_status_client(["DEMO-100"]),
    )

    assert len(plans) == 1
    plan = plans[0]
    assert plan.role == "multi_epic"
    assert len(plan.groups) == 2
    assert plan.groups[0].epic_action.kind == "update_epic"
    assert plan.groups[1].epic_action.kind == "create_epic"


def test_empty_input_returns_empty_plans():
    assert build_plans_from_dirty([], client=_open_status_client()) == []


# ----------------------------------------------------------------------
# filter_dirty — produces the right DirtySection list
# ----------------------------------------------------------------------


def _ext(tasks: list[ExtractedTask]) -> ExtractionResult:
    return ExtractionResult(
        file_id="F1", file_name="F.md",
        epic=ExtractedEpic(
            summary="E",
            description=f"x\n\n{AGENT_MARKER}",
            assignee_name="Saar",
        ),
        tasks=tasks,
    )


def _multi_ext(epics: list[tuple[str, list[ExtractedTask]]]) -> MultiExtractionResult:
    return MultiExtractionResult(
        file_id="F1", file_name="F.md",
        epics=[
            ExtractedEpicWithTasks(
                summary=s, description=f"d\n\n{AGENT_MARKER}",
                assignee_name="Saar", tasks=ts,
            )
            for s, ts in epics
        ],
    )


def _fr(
    section_index: int = 0,
    matched_key: str | None = None,
    decisions: list[MatchDecision] | None = None,
    orphans: list[str] | None = None,
) -> FileEpicResult:
    return FileEpicResult(
        file_id="F1", file_name="F.md",
        section_index=section_index,
        extracted_epic_summary="E",
        extracted_epic_description=f"d\n\n{AGENT_MARKER}",
        extracted_epic_assignee_raw="Saar",
        matched_jira_key=matched_key,
        epic_match_confidence=0.95 if matched_key else 0.0,
        epic_match_reason="stub",
        task_decisions=decisions or [],
        task_anchors=[],
        orphan_keys=orphans or [],
    )


def test_filter_cold_path_keeps_everything():
    ext = _ext([_t("T1", "a1"), _t("T2", "a2")])
    fr = _fr(matched_key="DEMO-100", decisions=[
        MatchDecision(0, "DEMO-201", 0.9, "ok"),
        MatchDecision(1, None, 0.0, "no match"),
    ])
    result = filter_dirty(
        MatcherResult(file_results=[fr]),
        [(_drive_file(file_id="F1"), ext)],
        dirty_anchors_per_file=None,
    )
    assert len(result) == 1
    assert len(result[0].tasks) == 2


def test_filter_empty_dirty_drops_section():
    ext = _ext([_t("T", "a")])
    fr = _fr(matched_key="DEMO-100", decisions=[MatchDecision(0, "X", 0.9, "ok")])
    result = filter_dirty(
        MatcherResult(file_results=[fr]),
        [(_drive_file(file_id="F1"), ext)],
        dirty_anchors_per_file={"F1": set()},
    )
    assert result == []


def test_filter_keeps_only_dirty_tasks():
    ext = _ext([_t("T1", "a1"), _t("T2", "a2"), _t("T3", "a3")])
    fr = _fr(matched_key="DEMO-100", decisions=[
        MatchDecision(0, "X1", 0.9, "ok"),
        MatchDecision(1, "X2", 0.9, "ok"),
        MatchDecision(2, "X3", 0.9, "ok"),
    ])
    result = filter_dirty(
        MatcherResult(file_results=[fr]),
        [(_drive_file(file_id="F1"), ext)],
        dirty_anchors_per_file={"F1": {"a2"}},
    )
    assert len(result) == 1
    section = result[0]
    assert len(section.tasks) == 1
    assert section.tasks[0].extracted.source_anchor == "a2"


def test_filter_keeps_section_with_dirty_epic_token_even_if_no_dirty_task():
    ext = _ext([_t("T", "a")])
    fr = _fr(matched_key="DEMO-100", decisions=[MatchDecision(0, "X", 0.9, "ok")])
    result = filter_dirty(
        MatcherResult(file_results=[fr]),
        [(_drive_file(file_id="F1"), ext)],
        dirty_anchors_per_file={"F1": {"<epic>:0"}},
    )
    assert len(result) == 1
    assert result[0].tasks == []


def test_filter_drops_unmatched_cached_section_with_no_dirty_content():
    """An existing cached section that didn't pair with a Jira epic in
    the cold run is not re-created on every warm run unless its content
    is actually dirty. Brand-new sections appear in dirty via <epic>:N."""
    ext = _ext([_t("T", "a")])
    fr = _fr(matched_key=None, decisions=[MatchDecision(0, None, 0.0, "no")])
    result = filter_dirty(
        MatcherResult(file_results=[fr]),
        [(_drive_file(file_id="F1"), ext)],
        dirty_anchors_per_file={"F1": {"some-other-anchor"}},
    )
    assert result == []


def test_filter_keeps_section_when_its_task_is_dirty_even_unmatched():
    ext = _ext([_t("T", "a")])
    fr = _fr(matched_key=None, decisions=[MatchDecision(0, None, 0.0, "no")])
    result = filter_dirty(
        MatcherResult(file_results=[fr]),
        [(_drive_file(file_id="F1"), ext)],
        dirty_anchors_per_file={"F1": {"a"}},
    )
    assert len(result) == 1
    assert result[0].matched_jira_key is None
    assert len(result[0].tasks) == 1
    assert result[0].epic_dirty is False  # only the task was dirty


def test_filter_marks_epic_dirty_when_epic_token_in_dirty():
    ext = _ext([_t("T", "a")])
    fr = _fr(matched_key="DEMO-100", decisions=[MatchDecision(0, "X", 0.9, "ok")])
    result = filter_dirty(
        MatcherResult(file_results=[fr]),
        [(_drive_file(file_id="F1"), ext)],
        dirty_anchors_per_file={"F1": {"<epic>:0"}},
    )
    assert result[0].epic_dirty is True


def test_filter_drops_clean_sections_in_multi_epic():
    ext = _multi_ext([
        ("Section A", [_t("A1", "a1")]),
        ("Section B", [_t("B1", "b1")]),
    ])
    frs = [
        _fr(section_index=0, matched_key="DEMO-100",
            decisions=[MatchDecision(0, "X1", 0.9, "ok")]),
        _fr(section_index=1, matched_key="DEMO-200",
            decisions=[MatchDecision(0, "X2", 0.9, "ok")]),
    ]
    result = filter_dirty(
        MatcherResult(file_results=frs),
        [(_drive_file(file_id="F1"), ext)],
        dirty_anchors_per_file={"F1": {"b1"}},
    )
    assert len(result) == 1
    assert result[0].section_index == 1
    assert result[0].tasks[0].extracted.source_anchor == "b1"
