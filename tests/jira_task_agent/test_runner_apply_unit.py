"""Offline tests for the c5 apply path.

Covers the runner's apply functions after they were refactored to route
all writes through `JiraSink.create / update / comment` and read live
state via `sink.get_issue_normalized`. The load-bearing invariants:

  * `finalize_body` runs against the LIVE Jira description before each
    update — preserves user-ticked DoD checkmarks.
  * Pre-resolved assignee usernames go through `Ticket.custom_fields`
    so the sink's own `assignee_resolver` does not re-resolve them.
  * `_comment_for` reads via `sink.get_issue_normalized` (no module-level
    `get_issue` import on the runner side).
  * Capture mode is `CapturingJiraSink` (no monkey-patching); writes
    land in `sink.captured_writes`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from _shared.io.sinks import Ticket
from _shared.io.sinks.jira import CapturingJiraSink, JiraSink

from jira_task_agent.drive.client import DriveFile
from jira_task_agent.pipeline.reconciler import Action, EpicGroup, ReconcilePlan
from jira_task_agent.runner import (
    _apply_epic_action,
    _apply_plan,
    _apply_task_action,
    _comment_for,
    _ticket_for_create,
    _ticket_for_update,
)

from .conftest import MockJiraClient


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def patch_llm_for_finalize_and_comment(monkeypatch):
    """Patch the two LLM call sites the c5 apply path can reach offline:

      - `extractor.chat` (used by `finalize_body` when live body is
        non-empty AND differs from new body)
      - `commenter.chat` (used by `format_update_comment` when comment
        bullets are summarised)

    Returns a deterministic merge of the two bodies for finalize_body and
    a fixed bullet for the comment path. Tests that exercise the merge
    invariant (e.g. preserving `[x]` checkmarks) get a stubbed merger
    that simply union-merges DoD bullets — enough to verify the runner
    feeds finalize_body the live body, not whether finalize_body itself
    is correct (that's a different test's job)."""
    from jira_task_agent.pipeline import commenter, extractor

    def fake_finalize_chat(*, system, user, models, temperature, json_mode):
        # Pull the new body and live body out of the rendered prompt
        # (they're inside fenced markers in the prompt template). For
        # tests, we just merge by preferring lines from live that have
        # a `[x]` mark over the equivalent `[ ]` in new.
        new = ""
        live = ""
        if "<<<NEW BODY>>>" in user and "<<<LIVE JIRA BODY>>>" in user:
            new = user.split("<<<NEW BODY>>>", 1)[1].split("<<<", 1)[0]
            live = user.split("<<<LIVE JIRA BODY>>>", 1)[1].split("<<<", 1)[0]
        out_lines = []
        live_marks = {
            line.replace("- [x]", "- [ ]").strip(): True
            for line in live.splitlines()
            if "- [x]" in line
        }
        for line in (new or live).splitlines():
            if line.strip() in live_marks and "- [ ]" in line:
                out_lines.append(line.replace("- [ ]", "- [x]", 1))
            else:
                out_lines.append(line)
        return "\n".join(out_lines), None

    def fake_comment_chat(*, system, user, models, temperature, json_mode):
        return "* updated body and summary", None

    monkeypatch.setattr(extractor, "chat", fake_finalize_chat)
    monkeypatch.setattr(commenter, "chat", fake_comment_chat)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


class _RecordingSink:
    """Sink stub that records every method the runner calls. Implements
    just the surface c5's apply path uses."""

    def __init__(self, live_descriptions: dict[str, str] | None = None) -> None:
        self.created: list[Ticket] = []
        self.updated: list[tuple[str, Ticket]] = []
        self.comments: list[tuple[str, str]] = []
        self.normalized_reads: list[str] = []
        self._live_descriptions = live_descriptions or {}

    def create(self, ticket: Ticket) -> str:
        self.created.append(ticket)
        return f"NEW-{len(self.created)}"

    def update(self, key: str, ticket: Ticket) -> None:
        self.updated.append((key, ticket))

    def comment(self, key: str, body: str) -> None:
        self.comments.append((key, body))

    def get_issue_normalized(self, key: str) -> dict[str, Any]:
        self.normalized_reads.append(key)
        return {
            "summary": f"{key} live summary",
            "description": self._live_descriptions.get(key, ""),
            "assignee_username": "live_user",
            "reporter_username": "live_reporter",
        }


def _drive_file() -> DriveFile:
    return DriveFile(
        id="F1", name="F.md", mime_type="text/markdown",
        created_time=datetime(2026, 5, 6, tzinfo=timezone.utc),
        modified_time=datetime(2026, 5, 6, tzinfo=timezone.utc),
        size=100,
        creator_name=None, creator_email=None,
        last_modifying_user_name="Saar",
        last_modifying_user_email=None,
        parents=[],
        web_view_link="http://drive/F",
    )


def _action(kind: str, **kwargs: Any) -> Action:
    return Action(kind=kind, **kwargs)


# ----------------------------------------------------------------------
# _ticket_for_create / _ticket_for_update
# ----------------------------------------------------------------------


def test_ticket_for_create_carries_assignee_in_custom_fields_not_ticket_assignee():
    """Pre-resolved assignee username must NOT go through `Ticket.assignee`,
    because that would route through the sink's `assignee_resolver` again
    (the reconciler already resolved it). Custom fields override the
    resolver path."""
    a = _action(
        "create_epic",
        summary="S", description="D", assignee_username="sriftin",
    )
    ticket = _ticket_for_create(a, issue_type="Epic", epic_key=None)
    assert ticket.assignee is None
    assert ticket.custom_fields == {"assignee": {"name": "sriftin"}}


def test_ticket_for_create_omits_assignee_when_username_is_none():
    a = _action("create_epic", summary="S", description="D")
    ticket = _ticket_for_create(a, issue_type="Epic", epic_key=None)
    assert ticket.assignee is None
    assert ticket.custom_fields == {}


def test_ticket_for_create_sets_epic_key_for_tasks():
    a = _action("create_task", summary="T", description="d")
    ticket = _ticket_for_create(a, issue_type="Task", epic_key="DEMO-100")
    assert ticket.type == "Task"
    assert ticket.epic_key == "DEMO-100"


def test_ticket_for_update_passes_live_description_to_finalize_body(monkeypatch):
    """`_ticket_for_update` must invoke `finalize_body(new_body, live_body)`
    so the merge logic gets a chance to preserve user-ticked DoD checkmarks.
    Verifies the WIRING — finalize_body's own merge correctness is covered
    in `test_dod_preserve_live` (live) and finalize_body's own unit tests."""
    captured: dict = {}

    def fake_finalize_body(new_body: str, live_body: str) -> str:
        captured["new_body"] = new_body
        captured["live_body"] = live_body
        return f"FINALIZED({new_body!r},{live_body!r})"

    import jira_task_agent.runner as runner_module
    monkeypatch.setattr(runner_module, "finalize_body", fake_finalize_body)

    a = _action(
        "update_task", target_key="DEMO-200",
        summary="S", description="new body",
    )
    ticket = _ticket_for_update(
        a, {"description": "live body with user marks"}, issue_type="Task",
    )
    assert captured == {
        "new_body": "new body", "live_body": "live body with user marks",
    }
    assert ticket.description == "FINALIZED('new body','live body with user marks')"


def test_ticket_for_update_with_no_live_description_uses_new_body_unchanged():
    """When the live description is empty, finalize_body should fall back
    to the new body (or a body very close to it)."""
    new_body = (
        "Body.\n\n### Definition of Done\n- [ ] one\n\n"
        "<!-- managed-by:jira-task-agent v1 -->"
    )
    a = _action(
        "update_epic", target_key="DEMO-100",
        summary="S", description=new_body,
    )
    ticket = _ticket_for_update(a, {"description": ""}, issue_type="Epic")
    # The new body's content (Goal / DoD bullets) should still appear.
    assert "Definition of Done" in ticket.description
    assert "one" in ticket.description


# ----------------------------------------------------------------------
# _apply_epic_action
# ----------------------------------------------------------------------


def test_apply_epic_action_create_routes_through_sink_create():
    sink = _RecordingSink()
    a = _action("create_epic", summary="New epic", description="body")
    key = _apply_epic_action(a, drive_file=_drive_file(), sink=sink)

    assert key == "NEW-1"
    assert len(sink.created) == 1
    assert sink.created[0].type == "Epic"
    assert sink.created[0].summary == "New epic"
    # No update / comment / read for a create.
    assert sink.updated == []
    assert sink.comments == []
    assert sink.normalized_reads == []


def test_apply_epic_action_update_reads_live_then_updates_then_comments(
    patch_llm_for_finalize_and_comment,
):
    """Order matters: read live → update → post comment."""
    sink = _RecordingSink(live_descriptions={"DEMO-100": "old body"})
    a = _action(
        "update_epic", target_key="DEMO-100",
        summary="Updated summary", description="new body",
    )
    key = _apply_epic_action(a, drive_file=_drive_file(), sink=sink)

    assert key == "DEMO-100"
    # `_apply_epic_action` reads once via `_ticket_for_update`, comment
    # path reads again via `_comment_for` — both are valid sink calls.
    assert sink.normalized_reads == ["DEMO-100", "DEMO-100"]
    assert len(sink.updated) == 1
    assert sink.updated[0][0] == "DEMO-100"
    assert sink.updated[0][1].summary == "Updated summary"
    assert len(sink.comments) == 1
    assert sink.comments[0][0] == "DEMO-100"


def test_apply_epic_action_noop_returns_target_key_without_sink_writes():
    sink = _RecordingSink()
    a = _action("noop", target_key="DEMO-100")
    key = _apply_epic_action(a, drive_file=_drive_file(), sink=sink)

    assert key == "DEMO-100"
    assert sink.created == []
    assert sink.updated == []
    assert sink.comments == []


def test_apply_epic_action_skip_completed_epic_returns_none_no_writes():
    sink = _RecordingSink()
    a = _action("skip_completed_epic", target_key="DEMO-100")
    key = _apply_epic_action(a, drive_file=_drive_file(), sink=sink)

    assert key is None
    assert sink.created == []
    assert sink.updated == []


# ----------------------------------------------------------------------
# _apply_task_action
# ----------------------------------------------------------------------


def test_apply_task_action_create_routes_through_sink_create_with_epic_link():
    sink = _RecordingSink()
    a = _action(
        "create_task", summary="T", description="d", assignee_username="alice",
    )
    _apply_task_action(
        a, epic_key="DEMO-100", drive_file=_drive_file(), sink=sink,
    )

    assert len(sink.created) == 1
    ticket = sink.created[0]
    assert ticket.type == "Task"
    assert ticket.epic_key == "DEMO-100"
    assert ticket.custom_fields == {"assignee": {"name": "alice"}}


def test_apply_task_action_update_runs_finalize_body_then_comments(
    patch_llm_for_finalize_and_comment,
):
    sink = _RecordingSink(live_descriptions={"DEMO-200": "live"})
    a = _action(
        "update_task", target_key="DEMO-200",
        summary="T", description="new",
    )
    _apply_task_action(
        a, epic_key="DEMO-100", drive_file=_drive_file(), sink=sink,
    )

    assert len(sink.updated) == 1
    assert sink.updated[0][0] == "DEMO-200"
    assert len(sink.comments) == 1


def test_apply_task_action_orphan_does_not_touch_sink():
    sink = _RecordingSink()
    a = _action("orphan", target_key="DEMO-999")
    _apply_task_action(
        a, epic_key="DEMO-100", drive_file=_drive_file(), sink=sink,
    )
    assert sink.created == []
    assert sink.updated == []


# ----------------------------------------------------------------------
# _apply_plan
# ----------------------------------------------------------------------


def test_apply_plan_walks_epic_groups_and_tasks_in_order():
    """Plan walks each EpicGroup; epic action first, then its tasks."""
    sink = _RecordingSink()
    plan = ReconcilePlan(
        file_id="F1", file_name="F.md", role="single_epic",
        groups=[
            EpicGroup(
                epic_action=_action(
                    "create_epic", summary="E1", description="ed",
                ),
                task_actions=[
                    _action("create_task", summary="T1", description="d1"),
                    _action("create_task", summary="T2", description="d2"),
                ],
            ),
        ],
    )
    _apply_plan(plan, drive_file=_drive_file(), sink=sink)

    assert len(sink.created) == 3
    assert sink.created[0].type == "Epic"
    assert sink.created[0].summary == "E1"
    # Tasks routed under the freshly created epic key.
    assert sink.created[1].type == "Task"
    assert sink.created[1].epic_key == "NEW-1"
    assert sink.created[2].epic_key == "NEW-1"


def test_apply_plan_target_epic_overrides_per_plan_epic():
    """`--target-epic` short-circuits epic creation and routes all tasks
    under the explicit key."""
    sink = _RecordingSink()
    plan = ReconcilePlan(
        file_id="F1", file_name="F.md", role="single_epic",
        groups=[
            EpicGroup(
                epic_action=_action(
                    "create_epic", summary="E1", description="ed",
                ),
                task_actions=[
                    _action("create_task", summary="T1", description="d1"),
                ],
            ),
        ],
    )
    _apply_plan(
        plan, drive_file=_drive_file(), sink=sink, target_epic="OVR-1",
    )

    # No epic created — target_epic short-circuits it.
    assert all(t.type == "Task" for t in sink.created)
    assert len(sink.created) == 1
    assert sink.created[0].epic_key == "OVR-1"


# ----------------------------------------------------------------------
# _comment_for
# ----------------------------------------------------------------------


def test_comment_for_reads_live_via_sink_get_issue_normalized(
    patch_llm_for_finalize_and_comment,
):
    """The comment renderer must go through the sink's normalized read,
    not the module-level `get_issue` import that c5 removed."""
    sink = _RecordingSink()
    a = _action(
        "update_task", target_key="DEMO-200",
        summary="S", description="d",
    )
    body = _comment_for(a, drive_file=_drive_file(), sink=sink)

    assert sink.normalized_reads == ["DEMO-200"]
    assert isinstance(body, str)
    assert body  # non-empty


# ----------------------------------------------------------------------
# CapturingJiraSink — capture mode without monkey-patching
# ----------------------------------------------------------------------


def test_capturing_jira_sink_collects_writes_without_sending():
    """`--capture` mode is now `CapturingJiraSink`. Writes routed through
    `sink.create` / `sink.update` / `sink.comment` land in
    `sink.captured_writes` and the underlying client's `.post` / `.put`
    are diverted to recorder closures."""
    client = MockJiraClient()
    sink = CapturingJiraSink(
        client=client, project_key="DEMO", filter_components=False,
    )
    plan = ReconcilePlan(
        file_id="F1", file_name="F.md", role="single_epic",
        groups=[
            EpicGroup(
                epic_action=_action(
                    "create_epic", summary="E1", description="ed",
                ),
                task_actions=[
                    _action("create_task", summary="T1", description="d1"),
                ],
            ),
        ],
    )
    _apply_plan(plan, drive_file=_drive_file(), sink=sink)

    # Writes were captured, not sent.
    assert len(sink.captured_writes) == 2
    methods = [op["method"] for op in sink.captured_writes]
    paths = [op["path"] for op in sink.captured_writes]
    assert methods == ["POST", "POST"]
    assert paths == ["/issue", "/issue"]
    # MockJiraClient's recorded list should be empty — `_install_capture`
    # diverted post/put before any real call could land.
    assert client.recorded == [], (
        "CapturingJiraSink leaked a write through the underlying client"
    )


def test_capturing_jira_sink_synthesises_captured_keys_for_create():
    """Captured create-issue calls return synthetic CAPTURED-N keys so
    downstream task actions can still use them as epic_key."""
    client = MockJiraClient()
    sink = CapturingJiraSink(
        client=client, project_key="DEMO", filter_components=False,
    )
    plan = ReconcilePlan(
        file_id="F1", file_name="F.md", role="single_epic",
        groups=[
            EpicGroup(
                epic_action=_action(
                    "create_epic", summary="E1", description="ed",
                ),
                task_actions=[
                    _action("create_task", summary="T1", description="d1"),
                ],
            ),
        ],
    )
    _apply_plan(plan, drive_file=_drive_file(), sink=sink)

    # The task's create POST should reference the captured epic key.
    task_op = sink.captured_writes[1]
    epic_link = None
    for k, v in (task_op["body"].get("fields") or {}).items():
        if "customfield" in k and isinstance(v, str) and v.startswith("CAPTURED"):
            epic_link = v
            break
    assert epic_link == "CAPTURED-1"
