"""Contract tests for JiraSink + identification strategies.

Uses a fake JiraClient that records calls instead of hitting the network.
The fake mirrors the public surface JiraSink consumes: ``create_issue``,
``update_issue``, ``post_comment``, ``transition_issue``, ``search``,
``get``, ``get_components``, ``search_user_picker``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from _shared.io.sinks import (
    AssigneeResolver,
    EpicRouter,
    Ticket,
)
from _shared.io.sinks.jira import JiraSink
from _shared.io.sinks.jira.strategies import (
    CacheTrustStrategy,
    LabelSearchStrategy,
)


# ===========================================================================
# Fake JiraClient
# ===========================================================================


@dataclass
class FakeJiraClient:
    """Records every call. Search/get return canned data set per-test."""

    components: list[dict[str, Any]] = field(default_factory=list)
    search_results: list[dict[str, Any]] = field(default_factory=list)
    issues_by_key: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_create_key: str = "MOCK-1"
    transitions_succeed: bool = True

    creates: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    comments: list[dict[str, Any]] = field(default_factory=list)
    transitions: list[dict[str, Any]] = field(default_factory=list)
    searches: list[str] = field(default_factory=list)

    def create_issue(self, **kwargs: Any) -> dict[str, Any]:
        self.creates.append(kwargs)
        return {"key": self.next_create_key, "id": "1", "self": "(mock)"}

    def update_issue(self, key: str, fields: dict[str, Any]) -> None:
        self.updates.append({"key": key, "fields": fields})

    def post_comment(self, key: str, body: str) -> dict[str, Any]:
        self.comments.append({"key": key, "body": body})
        return {}

    def transition_issue(self, key: str, target_status: str) -> bool:
        self.transitions.append({"key": key, "status": target_status})
        return self.transitions_succeed

    def search(
        self,
        jql: str,
        *,
        fields: list[str] | None = None,
        max_results: int = 100,
    ) -> list[dict]:
        self.searches.append(jql)
        return list(self.search_results)

    def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        if path.startswith("/issue/"):
            key = path[len("/issue/"):]
            return self.issues_by_key.get(key, {})
        return {}

    def get_components(self, project_key: str) -> list[dict[str, Any]]:
        return list(self.components)

    def search_user_picker(self, query: str) -> list[dict[str, Any]]:
        return []


# ===========================================================================
# JiraSink.create
# ===========================================================================


def test_create_minimal_ticket_returns_key():
    fake = FakeJiraClient(next_create_key="CENTPM-1234")
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)
    key = sink.create(
        Ticket(
            summary="Add login button",
            description="The page is missing a login button.",
            type="Task",
        )
    )
    assert key == "CENTPM-1234"
    assert len(fake.creates) == 1
    call = fake.creates[0]
    assert call["project_key"] == "CENTPM"
    assert call["summary"] == "Add login button"
    assert call["issue_type"] == "Task"
    # Default: ai-generated label is added (drive convention).
    assert call["add_ai_generated_label"] is True


def test_create_opts_out_of_ai_generated_label():
    """f2j's bug-ticket flow opts out so Bug tickets don't carry the marker."""
    fake = FakeJiraClient()
    sink = JiraSink(
        client=fake,
        project_key="CENTPM",
        add_ai_generated_label=False,
        filter_components=False,
    )
    sink.create(
        Ticket(summary="x", description="y", type="Bug", labels=["upstream:CORE-1"])
    )
    call = fake.creates[0]
    assert call["add_ai_generated_label"] is False


def test_create_subtask_routes_parent_key_not_epic_link():
    fake = FakeJiraClient()
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)
    sink.create(
        Ticket(
            summary="QA",
            description="Test it",
            type="Sub-task",
            parent_key="CENTPM-100",
            epic_key="CENTPM-1184",  # set but should NOT be used for sub-tasks
        )
    )
    call = fake.creates[0]
    assert call["parent_key"] == "CENTPM-100"
    assert call["epic_link"] is None  # sub-tasks bypass epic link


def test_create_non_subtask_routes_via_epic_link():
    fake = FakeJiraClient()
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)
    sink.create(
        Ticket(
            summary="Story",
            description="d",
            type="Story",
            epic_key="CENTPM-1184",
        )
    )
    call = fake.creates[0]
    assert call["parent_key"] is None
    assert call["epic_link"] == "CENTPM-1184"


def test_create_filters_components_against_live_list():
    """Invented component names get silently dropped; valid ones pass through."""
    fake = FakeJiraClient(
        components=[{"id": "1", "name": "Core"}, {"id": "2", "name": "API"}]
    )
    sink = JiraSink(client=fake, project_key="CENTPM")
    sink.create(
        Ticket(
            summary="x",
            description="y",
            type="Task",
            components=["Core", "InventedNonsense", "API"],
        )
    )
    extra = fake.creates[0]["extra_fields"]
    assert extra["components"] == [{"name": "Core"}, {"name": "API"}]


def test_create_drops_components_when_project_has_none():
    """CENTPM today has zero components — field should be omitted entirely."""
    fake = FakeJiraClient(components=[])
    sink = JiraSink(client=fake, project_key="CENTPM")
    sink.create(
        Ticket(
            summary="x",
            description="y",
            type="Task",
            components=["Core"],  # invented
        )
    )
    extra = fake.creates[0]["extra_fields"]
    assert "components" not in (extra or {})


def test_create_uses_assignee_resolver():
    class _Resolver:
        def resolve(self, display_name: str) -> str | None:
            return {"Sharon Gordon": "sgordon"}.get(display_name)

    fake = FakeJiraClient()
    sink = JiraSink(
        client=fake,
        project_key="CENTPM",
        assignee_resolver=_Resolver(),
        filter_components=False,
    )
    sink.create(
        Ticket(
            summary="x", description="y", type="Task", assignee="Sharon Gordon"
        )
    )
    extra = fake.creates[0]["extra_fields"]
    assert extra["assignee"] == {"name": "sgordon"}


def test_create_drops_assignee_when_resolver_returns_none():
    class _Resolver:
        def resolve(self, display_name: str) -> str | None:
            return None

    fake = FakeJiraClient()
    sink = JiraSink(
        client=fake,
        project_key="CENTPM",
        assignee_resolver=_Resolver(),
        filter_components=False,
    )
    sink.create(
        Ticket(
            summary="x", description="y", type="Task", assignee="Unknown Person"
        )
    )
    extra = fake.creates[0].get("extra_fields") or {}
    assert "assignee" not in extra


def test_create_uses_epic_router():
    captured = {}

    class _Router:
        def route(self, ticket: Ticket, *, hint: str | None = None) -> str | None:
            captured["ticket"] = ticket
            return "CENTPM-1184"

    fake = FakeJiraClient()
    sink = JiraSink(
        client=fake,
        project_key="CENTPM",
        epic_router=_Router(),
        filter_components=False,
    )
    sink.create(
        Ticket(
            summary="x", description="y", type="Task", epic_key="CENTPM-OTHER"
        )
    )
    assert captured["ticket"].summary == "x"
    assert fake.creates[0]["epic_link"] == "CENTPM-1184"  # router beats ticket.epic_key


# ===========================================================================
# JiraSink.update / comment / transition / search / get_issue
# ===========================================================================


def test_update_emits_only_changed_fields():
    fake = FakeJiraClient()
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)
    sink.update(
        "CENTPM-100",
        Ticket(
            summary="new title",
            description="new desc",
            type="Task",
            priority="P1 - Should have",
            labels=["x"],
        ),
    )
    assert len(fake.updates) == 1
    call = fake.updates[0]
    assert call["key"] == "CENTPM-100"
    fields = call["fields"]
    assert fields["summary"] == "new title"
    assert fields["description"] == "new desc"
    assert fields["priority"] == {"name": "P1 - Should have"}
    # ai-generated label is auto-added on update too (drive convention).
    assert "ai-generated" in fields["labels"]
    assert "x" in fields["labels"]


def test_comment_passes_through_to_client():
    fake = FakeJiraClient()
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)
    sink.comment("CENTPM-100", "see fix at *line 42*")
    assert fake.comments == [{"key": "CENTPM-100", "body": "see fix at *line 42*"}]


def test_transition_returns_client_result():
    fake = FakeJiraClient(transitions_succeed=False)
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)
    assert sink.transition("CENTPM-100", "Done") is False
    assert fake.transitions == [{"key": "CENTPM-100", "status": "Done"}]


def test_get_issue_passes_through():
    fake = FakeJiraClient(issues_by_key={"CENTPM-100": {"key": "CENTPM-100"}})
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)
    issue = sink.get_issue("CENTPM-100")
    assert issue == {"key": "CENTPM-100"}


# ===========================================================================
# LabelSearchStrategy
# ===========================================================================


def test_label_search_finds_existing_via_external_id():
    """f2j's idempotency: upstream:<external_id> JQL search."""
    fake = FakeJiraClient(
        search_results=[
            {"key": "CENTPM-1337", "fields": {"summary": "Old ticket"}}
        ]
    )
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)
    strategy = LabelSearchStrategy(
        label_template="upstream:{external_id}", project_key="CENTPM"
    )
    ticket = Ticket(
        summary="x",
        description="y",
        type="Bug",
        custom_fields={"external_id": "CORE-CHAT-026"},
    )
    found = sink.find_existing(ticket, strategy)
    assert found == "CENTPM-1337"
    # JQL was scoped to project + label
    assert len(fake.searches) == 1
    jql = fake.searches[0]
    assert "upstream:CORE-CHAT-026" in jql
    assert "CENTPM" in jql


def test_label_search_returns_none_on_miss():
    fake = FakeJiraClient(search_results=[])
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)
    strategy = LabelSearchStrategy(label_template="upstream:{external_id}")
    ticket = Ticket(
        summary="x",
        description="y",
        type="Bug",
        custom_fields={"external_id": "NEW-BUG-001"},
    )
    assert sink.find_existing(ticket, strategy) is None


def test_label_search_returns_none_when_template_key_missing():
    """Template references a key the ticket lacks → no stable identity → miss."""
    fake = FakeJiraClient()
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)
    strategy = LabelSearchStrategy(label_template="upstream:{external_id}")
    ticket = Ticket(summary="x", description="y", type="Bug")  # no external_id
    assert sink.find_existing(ticket, strategy) is None
    # And no JQL was run (skipped early)
    assert fake.searches == []


# ===========================================================================
# CacheTrustStrategy
# ===========================================================================


def test_cache_trust_returns_key_from_mapping():
    """Drive's pattern — Tier 3 matcher cache supplies the mapping."""
    fake = FakeJiraClient()
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)
    strategy = CacheTrustStrategy(cache={"doc::v11/extracted_task_3": "CENTPM-9000"})
    ticket = Ticket(
        summary="x",
        description="y",
        type="Task",
        custom_fields={"cache_key": "doc::v11/extracted_task_3"},
    )
    assert sink.find_existing(ticket, strategy) == "CENTPM-9000"
    # No tracker-side query — cache trust is purely local.
    assert fake.searches == []


def test_cache_trust_returns_none_when_key_missing_from_cache():
    fake = FakeJiraClient()
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)
    strategy = CacheTrustStrategy(cache={})
    ticket = Ticket(
        summary="x",
        description="y",
        type="Task",
        custom_fields={"cache_key": "doc::missing"},
    )
    assert sink.find_existing(ticket, strategy) is None


def test_cache_trust_returns_none_when_ticket_has_no_cache_key():
    fake = FakeJiraClient()
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)
    strategy = CacheTrustStrategy(cache={"doc::known": "CENTPM-1"})
    ticket = Ticket(summary="x", description="y", type="Task")
    assert sink.find_existing(ticket, strategy) is None


def test_cache_trust_custom_key_fn():
    fake = FakeJiraClient()
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)
    strategy = CacheTrustStrategy(
        cache={"composed-CORE-CHAT-026-Bug": "CENTPM-1337"},
        key_fn=lambda t: f"composed-{t.custom_fields['external_id']}-{t.type}",
    )
    ticket = Ticket(
        summary="x",
        description="y",
        type="Bug",
        custom_fields={"external_id": "CORE-CHAT-026"},
    )
    assert sink.find_existing(ticket, strategy) == "CENTPM-1337"


def test_jira_sink_create_raises_when_client_returns_no_key():
    """Defensive: never silently swallow a malformed create response."""
    fake = FakeJiraClient(next_create_key="")
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)
    with pytest.raises(RuntimeError, match="returned no key"):
        sink.create(Ticket(summary="x", description="y", type="Task"))


# ===========================================================================
# JiraSink.get_issue_normalized + fetch_project_tree
# ===========================================================================


def test_get_issue_normalized_returns_drives_flat_shape():
    fake = FakeJiraClient(
        issues_by_key={
            "CENTPM-1": {
                "key": "CENTPM-1",
                "fields": {
                    "summary": "test",
                    "description": "body",
                    "status": {"name": "In Progress"},
                    "assignee": {"name": "alice", "displayName": "Alice"},
                    "reporter": {"name": "bob", "displayName": "Bob"},
                    "priority": {"name": "P1"},
                    "issuetype": {"name": "Task"},
                    "labels": ["x"],
                    "created": "2026-01-01",
                    "updated": "2026-01-02",
                },
            },
        },
    )
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)

    result = sink.get_issue_normalized("CENTPM-1")

    assert result["key"] == "CENTPM-1"
    assert result["summary"] == "test"
    assert result["status"] == "In Progress"
    assert result["assignee_username"] == "alice"
    assert result["assignee_name"] == "Alice"
    assert result["issue_type"] == "Task"
    assert result["priority"] == "P1"
    assert result["labels"] == ["x"]


def test_fetch_project_tree_delegates_to_helper(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_fetch(client, project_key, *, log=None):
        seen["client"] = client
        seen["project_key"] = project_key
        return {"epic_count": 0, "child_count": 0, "orphan_count": 0, "epics": []}

    monkeypatch.setattr(
        "_shared.io.sinks.jira.project_tree.fetch_project_tree", fake_fetch
    )

    fake = FakeJiraClient()
    sink = JiraSink(client=fake, project_key="CENTPM", filter_components=False)

    result = sink.fetch_project_tree()

    assert seen["client"] is fake
    assert seen["project_key"] == "CENTPM"
    assert result["epic_count"] == 0


def test_fetch_project_tree_accepts_explicit_project_override(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_fetch(client, project_key, *, log=None):
        seen["project_key"] = project_key
        return {"epic_count": 0, "child_count": 0, "orphan_count": 0, "epics": []}

    monkeypatch.setattr(
        "_shared.io.sinks.jira.project_tree.fetch_project_tree", fake_fetch
    )

    sink = JiraSink(
        client=FakeJiraClient(), project_key="DEFAULT", filter_components=False,
    )
    sink.fetch_project_tree("OTHER")

    assert seen["project_key"] == "OTHER"


# ===========================================================================
# CapturingJiraSink
# ===========================================================================


def _make_offline_jira_client():
    """Construct a real :class:`JiraClient` with caches pre-populated so no
    HTTP request is ever made — used to exercise CapturingJiraSink's
    .post/.put diversion through the real client's higher-level methods."""
    from _shared.io.sinks.jira.client import JiraClient

    client = JiraClient(
        host="example.com", auth_header="Bearer fake", auth_mode="bearer",
    )
    # Block any lazy /field discovery so we don't hit the network.
    client._custom_fields = {}
    return client


def test_capturing_sink_create_records_post_and_returns_synthetic_key():
    from _shared.io.sinks.jira import CapturingJiraSink

    client = _make_offline_jira_client()
    sink = CapturingJiraSink(
        client=client, project_key="CENTPM", filter_components=False,
    )

    key = sink.create(Ticket(summary="hello", description="world", type="Task"))

    assert key == "CAPTURED-1"
    assert len(sink.captured_writes) == 1
    write = sink.captured_writes[0]
    assert write["method"] == "POST"
    assert write["path"] == "/issue"
    fields = write["body"]["fields"]
    assert fields["summary"] == "hello"
    assert fields["issuetype"] == {"name": "Task"}
    assert fields["project"] == {"key": "CENTPM"}


def test_capturing_sink_update_records_put_with_fields():
    from _shared.io.sinks.jira import CapturingJiraSink

    client = _make_offline_jira_client()
    sink = CapturingJiraSink(
        client=client, project_key="CENTPM", filter_components=False,
    )

    sink.update(
        "CENTPM-7",
        Ticket(summary="updated", description="new body", type="Task"),
    )

    assert len(sink.captured_writes) == 1
    write = sink.captured_writes[0]
    assert write["method"] == "PUT"
    assert write["path"] == "/issue/CENTPM-7"
    assert write["body"]["fields"]["summary"] == "updated"


def test_capturing_sink_comment_records_post_to_comment_endpoint():
    from _shared.io.sinks.jira import CapturingJiraSink

    client = _make_offline_jira_client()
    sink = CapturingJiraSink(
        client=client, project_key="CENTPM", filter_components=False,
    )

    sink.comment("CENTPM-7", "this is a comment")

    assert len(sink.captured_writes) == 1
    write = sink.captured_writes[0]
    assert write["method"] == "POST"
    assert write["path"] == "/issue/CENTPM-7/comment"
    assert write["body"]["body"] == "this is a comment"


def test_capturing_sink_reads_pass_through_to_real_client():
    """Reads (get_issue_normalized, search, etc.) must NOT be intercepted
    by capture mode — they hit the underlying client unchanged."""
    from _shared.io.sinks.jira import CapturingJiraSink

    client = _make_offline_jira_client()

    seen_paths: list[str] = []

    def fake_get(path: str, params=None):
        seen_paths.append(path)
        return {
            "key": "CENTPM-1",
            "fields": {
                "status": {"name": "Open"},
                "assignee": None,
                "reporter": None,
                "priority": None,
                "issuetype": {"name": "Task"},
                "summary": "x",
                "description": "y",
                "labels": [],
            },
        }

    client.get = fake_get  # type: ignore[method-assign]

    sink = CapturingJiraSink(
        client=client, project_key="CENTPM", filter_components=False,
    )
    result = sink.get_issue_normalized("CENTPM-1")

    assert seen_paths == ["/issue/CENTPM-1"]
    assert result["status"] == "Open"
    # Reads must not appear in the capture log.
    assert sink.captured_writes == []


def test_capturing_sink_assigns_sequential_capture_keys():
    """Multiple creates within the same sink get incrementing CAPTURED-N keys."""
    from _shared.io.sinks.jira import CapturingJiraSink

    client = _make_offline_jira_client()
    sink = CapturingJiraSink(
        client=client, project_key="CENTPM", filter_components=False,
    )

    k1 = sink.create(Ticket(summary="a", description="...", type="Task"))
    k2 = sink.create(Ticket(summary="b", description="...", type="Task"))
    k3 = sink.create(Ticket(summary="c", description="...", type="Task"))

    assert (k1, k2, k3) == ("CAPTURED-1", "CAPTURED-2", "CAPTURED-3")
    assert len(sink.captured_writes) == 3
