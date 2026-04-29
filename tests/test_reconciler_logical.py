"""Phase 1 — logical tests for the reconciler.

After the matcher refactor, the reconciler is pure logic — no LLM
calls. Tests construct a `MatcherResult` directly and call
`build_plans_from_match` to verify Action emission for each scenario:

  - empty Jira project    → all `create_*`
  - matched epic + content unchanged → `noop`
  - partial-match + extra child → some `noop`/`update_task`/`create_task`
                                 + `orphan`
  - manual-edit on epic   → `skip_manual_edits`
  - multi-epic file       → multiple groups, distinct match decisions
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jira_task_agent.drive.client import DriveFile
from jira_task_agent.pipeline import reconciler
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

from .conftest import MockJiraClient


PROJECT = "DEMO"
DRIVE_URL = "https://drive.google.com/file/d/DEMO_FILE/view"


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------


def _drive_file(name: str = "V_Demo_Tasks.md", file_id: str | None = None) -> DriveFile:
    return DriveFile(
        id=file_id or name,
        name=name,
        mime_type="text/markdown",
        created_time=datetime(2026, 4, 27, tzinfo=timezone.utc),
        modified_time=datetime(2026, 4, 27, tzinfo=timezone.utc),
        size=1000,
        creator_name=None,
        creator_email=None,
        last_modifying_user_name="Saar",
        last_modifying_user_email=None,
        parents=[],
        web_view_link=DRIVE_URL,
    )


def _task_description(text: str = "Body of task.") -> str:
    return (
        f"{text}\n\n"
        "### Acceptance criteria\n"
        "- It works\n\n"
        "### Definition of Done\n"
        "- [ ] Code merged\n"
        "- [ ] Tests pass\n"
        "- [ ] Reviewed\n\n"
        f"{AGENT_MARKER}"
    )


def _extraction(
    *,
    file_id: str = "V_Demo_Tasks.md",
    file_name: str = "V_Demo_Tasks.md",
    epic_summary: str = "Demo epic",
    task_summaries: list[str] | None = None,
) -> ExtractionResult:
    task_summaries = task_summaries or ["Task A", "Task B", "Task C"]
    return ExtractionResult(
        file_id=file_id,
        file_name=file_name,
        epic=ExtractedEpic(
            summary=epic_summary,
            description=f"Epic body.\n\n{AGENT_MARKER}",
            assignee_name="Saar",
        ),
        tasks=[
            ExtractedTask(
                summary=s,
                description=_task_description(f"Body of {s}."),
                source_anchor=f"anchor-{i}",
                assignee_name="Saar",
            )
            for i, s in enumerate(task_summaries)
        ],
    )


def _file_result(
    *,
    file_id: str,
    file_name: str,
    section_index: int = 0,
    epic_summary: str = "Demo epic",
    epic_description: str | None = None,
    matched_jira_key: str | None = None,
    task_decisions: list[MatchDecision] | None = None,
    orphan_keys: list[str] | None = None,
) -> FileEpicResult:
    return FileEpicResult(
        file_id=file_id,
        file_name=file_name,
        section_index=section_index,
        extracted_epic_summary=epic_summary,
        extracted_epic_description=(
            epic_description if epic_description is not None
            else f"Epic body.\n\n{AGENT_MARKER}"
        ),
        extracted_epic_assignee_raw="Saar",
        matched_jira_key=matched_jira_key,
        epic_match_confidence=0.95 if matched_jira_key else 0.0,
        epic_match_reason="stub",
        task_decisions=task_decisions or [],
        orphan_keys=orphan_keys or [],
    )


def _live_issue(
    *,
    key: str,
    summary: str,
    description: str,
    assignee_username: str | None = "sriftin",
    issue_type: str = "Task",
) -> dict:
    """Raw Jira-shaped issue (with `fields` nested) — matches the shape
    `_normalize_issue` and the reconciler's downstream code expect."""
    return {
        "id": "1",
        "key": key,
        "self": "(mock)",
        "fields": {
            "summary": summary,
            "description": description,
            "assignee": {
                "name": assignee_username,
                "displayName": "Saar Riftin",
                "emailAddress": "sriftin@nvidia.com",
            } if assignee_username else None,
            "reporter": {
                "name": "sriftin",
                "displayName": "Saar Riftin",
            },
            "labels": ["ai-generated"],
            "issuetype": {"name": issue_type},
            "status": {"name": "Open"},
            "priority": None,
            "created": "2026-04-01T00:00:00.000+0000",
            "updated": "2026-04-27T00:00:00.000+0000",
        },
    }


def _pre_converted_task_desc(body: str) -> str:
    from jira_task_agent.jira.client import JiraClient
    return JiraClient._md_to_jira_wiki(_task_description(body))


# ----------------------------------------------------------------------
# scenarios
# ----------------------------------------------------------------------


def test_empty_project_all_creates():
    """No Jira match → 1 create_epic + N create_task."""
    client = MockJiraClient(static_map={"saar": "sriftin"})
    extraction = _extraction(task_summaries=["A", "B", "C"])
    matcher_result = MatcherResult(
        file_results=[
            _file_result(
                file_id=extraction.file_id,
                file_name=extraction.file_name,
                matched_jira_key=None,
            )
        ]
    )

    plans = reconciler.build_plans_from_match(
        matcher_result, [(_drive_file(), extraction)], client=client
    )

    assert len(plans) == 1
    plan = plans[0]
    assert plan.role == "single_epic"
    assert len(plan.groups) == 1
    grp = plan.groups[0]
    assert grp.epic_action.kind == "create_epic"
    assert [a.kind for a in grp.task_actions] == ["create_task"] * 3


def test_matched_epic_unchanged_content_is_noop():
    """Match found, content equal on both sides → noop epic + noop tasks."""
    client = MockJiraClient(
        issues={
            "DEMO-100": _live_issue(
                key="DEMO-100",
                summary="Demo epic",
                description=f"Epic body.\n\n{AGENT_MARKER}",
            ),
            "DEMO-101": _live_issue(
                key="DEMO-101", summary="Task A",
                description=_pre_converted_task_desc("Body of Task A."),
            ),
            "DEMO-102": _live_issue(
                key="DEMO-102", summary="Task B",
                description=_pre_converted_task_desc("Body of Task B."),
            ),
        },
        children_by_epic={
            "DEMO-100": [
                _live_issue(
                    key="DEMO-101", summary="Task A",
                    description=_pre_converted_task_desc("Body of Task A."),
                ),
                _live_issue(
                    key="DEMO-102", summary="Task B",
                    description=_pre_converted_task_desc("Body of Task B."),
                ),
            ]
        },
        static_map={"saar": "sriftin"},
    )

    extraction = _extraction(task_summaries=["Task A", "Task B"])
    matcher_result = MatcherResult(
        file_results=[
            _file_result(
                file_id=extraction.file_id,
                file_name=extraction.file_name,
                matched_jira_key="DEMO-100",
                task_decisions=[
                    MatchDecision(item_index=0, candidate_key="DEMO-101",
                                  confidence=0.95, reason="A → 101"),
                    MatchDecision(item_index=1, candidate_key="DEMO-102",
                                  confidence=0.95, reason="B → 102"),
                ],
                orphan_keys=[],
            )
        ]
    )

    plans = reconciler.build_plans_from_match(
        matcher_result, [(_drive_file(), extraction)], client=client
    )

    grp = plans[0].groups[0]
    assert grp.epic_action.kind == "noop"
    assert grp.epic_action.target_key == "DEMO-100"
    assert [a.kind for a in grp.task_actions] == ["noop", "noop"]


def test_partial_match_creates_orphans_and_new():
    """2 of 3 extracted tasks match existing children; 1 existing child
    has no extracted counterpart → 2 noop + 1 create_task + 1 orphan."""
    client = MockJiraClient(
        issues={
            "DEMO-100": _live_issue(
                key="DEMO-100", summary="Demo epic",
                description=f"Epic body.\n\n{AGENT_MARKER}",
            ),
            "DEMO-101": _live_issue(
                key="DEMO-101", summary="Task A",
                description=_pre_converted_task_desc("Body of Task A."),
            ),
            "DEMO-102": _live_issue(
                key="DEMO-102", summary="Task B",
                description=_pre_converted_task_desc("Body of Task B."),
            ),
            "DEMO-103": _live_issue(
                key="DEMO-103", summary="Old task no longer in doc",
                description=_pre_converted_task_desc("This was once relevant."),
            ),
        },
        children_by_epic={
            "DEMO-100": [
                _live_issue(
                    key="DEMO-101", summary="Task A",
                    description=_pre_converted_task_desc("Body of Task A."),
                ),
                _live_issue(
                    key="DEMO-102", summary="Task B",
                    description=_pre_converted_task_desc("Body of Task B."),
                ),
                _live_issue(
                    key="DEMO-103",
                    summary="Old task no longer in doc",
                    description=_pre_converted_task_desc("This was once relevant."),
                ),
            ]
        },
        static_map={"saar": "sriftin"},
    )

    extraction = _extraction(
        task_summaries=["Task A", "Task B", "New Task C added in doc"]
    )
    matcher_result = MatcherResult(
        file_results=[
            _file_result(
                file_id=extraction.file_id,
                file_name=extraction.file_name,
                matched_jira_key="DEMO-100",
                task_decisions=[
                    MatchDecision(item_index=0, candidate_key="DEMO-101",
                                  confidence=0.95, reason="A → 101"),
                    MatchDecision(item_index=1, candidate_key="DEMO-102",
                                  confidence=0.95, reason="B → 102"),
                    MatchDecision(item_index=2, candidate_key=None,
                                  confidence=0.0, reason="new task"),
                ],
                orphan_keys=["DEMO-103"],
            )
        ]
    )

    plans = reconciler.build_plans_from_match(
        matcher_result, [(_drive_file(), extraction)], client=client
    )

    grp = plans[0].groups[0]
    kinds = [a.kind for a in grp.task_actions]
    assert kinds.count("noop") == 2
    assert kinds.count("create_task") == 1
    assert kinds.count("orphan") == 1
    orphan = next(a for a in grp.task_actions if a.kind == "orphan")
    assert orphan.target_key == "DEMO-103"


def test_manual_edit_skips_epic_update():
    """Matched epic but no agent marker in description → skip_manual_edits."""
    client = MockJiraClient(
        issues={
            "DEMO-100": _live_issue(
                key="DEMO-100", summary="Demo epic",
                description="Manually rewritten description, no marker.",
            ),
        },
        children_by_epic={"DEMO-100": []},
        static_map={"saar": "sriftin"},
    )

    extraction = _extraction(task_summaries=["A"])
    matcher_result = MatcherResult(
        file_results=[
            _file_result(
                file_id=extraction.file_id,
                file_name=extraction.file_name,
                matched_jira_key="DEMO-100",
                task_decisions=[
                    MatchDecision(item_index=0, candidate_key=None,
                                  confidence=0.0, reason="no children to match"),
                ],
                orphan_keys=[],
            )
        ]
    )

    plans = reconciler.build_plans_from_match(
        matcher_result, [(_drive_file(), extraction)], client=client
    )

    grp = plans[0].groups[0]
    assert grp.epic_action.kind == "skip_manual_edits"
    assert grp.epic_action.target_key == "DEMO-100"


def test_skip_completed_epic_suppresses_task_actions():
    """Matched epic is in a completed status (e.g. In Staging) → emit
    `skip_completed_epic` and produce ZERO task actions, regardless of
    what the matcher said about individual tasks. Doc is presumed stale."""
    client = MockJiraClient(
        issues={
            "DEMO-100": {
                "id": "1",
                "key": "DEMO-100",
                "self": "(mock)",
                "fields": {
                    "summary": "Demo epic",
                    "description": f"Original epic body.\n\n{AGENT_MARKER}",
                    "issuetype": {"name": "Epic"},
                    "status": {"name": "In Staging"},  # completed-ish
                    "assignee": None,
                    "reporter": {"name": "sriftin", "displayName": "Saar"},
                    "labels": ["ai-generated"],
                    "priority": None,
                    "created": "2026-04-01T00:00:00.000+0000",
                    "updated": "2026-04-27T00:00:00.000+0000",
                },
            },
        },
        children_by_epic={"DEMO-100": []},
        static_map={"saar": "sriftin"},
    )

    extraction = _extraction(task_summaries=["A", "B", "C"])
    matcher_result = MatcherResult(
        file_results=[
            _file_result(
                file_id=extraction.file_id,
                file_name=extraction.file_name,
                matched_jira_key="DEMO-100",
                # Even if the matcher had matched something, the status
                # guard suppresses task actions entirely.
                task_decisions=[
                    MatchDecision(item_index=0, candidate_key=None,
                                  confidence=0.0, reason="-"),
                    MatchDecision(item_index=1, candidate_key=None,
                                  confidence=0.0, reason="-"),
                    MatchDecision(item_index=2, candidate_key=None,
                                  confidence=0.0, reason="-"),
                ],
                orphan_keys=[],
            )
        ]
    )

    plans = reconciler.build_plans_from_match(
        matcher_result, [(_drive_file(), extraction)], client=client
    )

    grp = plans[0].groups[0]
    assert grp.epic_action.kind == "skip_completed_epic"
    assert grp.epic_action.target_key == "DEMO-100"
    assert "In Staging" in (grp.epic_action.note or "")
    # The whole point: no task actions emitted.
    assert grp.task_actions == []


def test_covered_by_rollup_when_multiple_tasks_match_same_candidate():
    """Two extracted tasks both match CENTPM-1239-style rollup → both
    become `covered_by_rollup` (not duplicate creates)."""
    client = MockJiraClient(
        issues={
            "DEMO-100": _live_issue(
                key="DEMO-100", summary="Demo epic",
                description=f"Epic body.\n\n{AGENT_MARKER}",
            ),
            "DEMO-ROLLUP": _live_issue(
                key="DEMO-ROLLUP", summary="Big rollup",
                description=_pre_converted_task_desc(
                    "Implemented scope:\n- step alpha\n- step beta\n- step gamma."
                ),
            ),
        },
        children_by_epic={
            "DEMO-100": [
                _live_issue(
                    key="DEMO-ROLLUP", summary="Big rollup",
                    description=_pre_converted_task_desc(
                        "Implemented scope:\n- step alpha\n- step beta\n- step gamma."
                    ),
                ),
            ]
        },
        static_map={"saar": "sriftin"},
    )

    extraction = _extraction(task_summaries=["step alpha", "step beta", "step gamma"])
    matcher_result = MatcherResult(
        file_results=[
            _file_result(
                file_id=extraction.file_id,
                file_name=extraction.file_name,
                matched_jira_key="DEMO-100",
                # Matcher cited DEMO-ROLLUP for ALL THREE extracted tasks
                # — rollup pattern.
                task_decisions=[
                    MatchDecision(item_index=0, candidate_key="DEMO-ROLLUP",
                                  confidence=0.9, reason="bullet alpha"),
                    MatchDecision(item_index=1, candidate_key="DEMO-ROLLUP",
                                  confidence=0.88, reason="bullet beta"),
                    MatchDecision(item_index=2, candidate_key="DEMO-ROLLUP",
                                  confidence=0.85, reason="bullet gamma"),
                ],
                orphan_keys=[],  # DEMO-ROLLUP is matched (just multi-cited)
            )
        ]
    )

    plans = reconciler.build_plans_from_match(
        matcher_result, [(_drive_file(), extraction)], client=client
    )
    grp = plans[0].groups[0]

    assert grp.epic_action.kind in {"noop", "update_epic"}
    # All three tasks → covered_by_rollup (none should be create_task).
    assert [a.kind for a in grp.task_actions] == ["covered_by_rollup"] * 3
    for a in grp.task_actions:
        assert a.target_key == "DEMO-ROLLUP"
        assert "covered by the rollup issue DEMO-ROLLUP" in (a.note or "")


def test_multi_epic_two_groups():
    """One multi_epic file → two FileEpicResults → one ReconcilePlan with 2 EpicGroups."""
    client = MockJiraClient(
        issues={
            "DEMO-200": _live_issue(
                key="DEMO-200", summary="Security Hardening",
                description=f"Epic body.\n\n{AGENT_MARKER}",
            ),
            "DEMO-201": _live_issue(
                key="DEMO-201", summary="Production Setup",
                description=f"Epic body.\n\n{AGENT_MARKER}",
            ),
        },
        children_by_epic={"DEMO-200": [], "DEMO-201": []},
        static_map={"saar": "sriftin"},
    )

    multi = MultiExtractionResult(
        file_id="May1.md",
        file_name="May1.md",
        epics=[
            ExtractedEpicWithTasks(
                summary="Production Security Hardening",
                description=f"Section A.\n\n{AGENT_MARKER}",
                assignee_name="Sharon",
                tasks=[
                    ExtractedTask(
                        summary="JWT secret",
                        description=_task_description("Generate JWT secret."),
                        source_anchor="SEC-1",
                        assignee_name="Sharon",
                    ),
                ],
            ),
            ExtractedEpicWithTasks(
                summary="Production Environment Setup",
                description=f"Section B.\n\n{AGENT_MARKER}",
                assignee_name="Yuval",
                tasks=[
                    ExtractedTask(
                        summary="Provision PostgreSQL",
                        description=_task_description("Provision DB."),
                        source_anchor="ENV-1",
                        assignee_name="Yuval",
                    ),
                ],
            ),
        ],
    )

    matcher_result = MatcherResult(
        file_results=[
            _file_result(
                file_id="May1.md",
                file_name="May1.md",
                section_index=0,
                epic_summary="Production Security Hardening",
                epic_description=f"Section A.\n\n{AGENT_MARKER}",
                matched_jira_key="DEMO-200",
                task_decisions=[
                    MatchDecision(item_index=0, candidate_key=None,
                                  confidence=0.0, reason="no children"),
                ],
                orphan_keys=[],
            ),
            _file_result(
                file_id="May1.md",
                file_name="May1.md",
                section_index=1,
                epic_summary="Production Environment Setup",
                epic_description=f"Section B.\n\n{AGENT_MARKER}",
                matched_jira_key="DEMO-201",
                task_decisions=[
                    MatchDecision(item_index=0, candidate_key=None,
                                  confidence=0.0, reason="no children"),
                ],
                orphan_keys=[],
            ),
        ]
    )

    plans = reconciler.build_plans_from_match(
        matcher_result, [(_drive_file(name="May1.md", file_id="May1.md"), multi)],
        client=client,
    )

    assert len(plans) == 1
    plan = plans[0]
    assert plan.role == "multi_epic"
    assert len(plan.groups) == 2
    assert plan.groups[0].epic_action.target_key == "DEMO-200"
    assert plan.groups[1].epic_action.target_key == "DEMO-201"
    # Each section has 1 create_task (no children to match).
    assert all(
        g.task_actions and g.task_actions[0].kind == "create_task"
        for g in plan.groups
    )
