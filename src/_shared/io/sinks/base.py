"""TicketSink protocol + the generic ``Ticket`` shape.

A ``Ticket`` is the input-shape any sink accepts: title + description + a
few well-known fields + an open ``custom_fields`` bag for tracker-specific
extras (Jira customfields, Linear cycle/state ids, Monday board columns).

Strategies plug in per-tool so the sink stays generic while bodies keep
their distinct idempotency / assignee / epic-routing semantics:
- :class:`IdentificationStrategy`: how to detect "is this ticket already
  in the tracker?" — f2j uses label-search, jira_task_agent trusts its
  Tier-3 matcher cache.
- :class:`AssigneeResolver`: display-name → tracker-native id. f2j queries
  ``/user/picker`` + caches in YAML; drive uses a static team_mapping.
- :class:`EpicRouter`: which epic a created ticket should link to. f2j has
  a deterministic prefix→module→default chain; drive doesn't route at
  upload time (epics come from extracted-then-matched flow). Jira-flavored
  concept; other trackers will define their own equivalent.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Ticket:
    """Generic, tracker-agnostic ticket payload.

    Attributes:
        summary: Short title; bounded length per tracker (Jira: 255 chars).
        description: Body in Markdown. The sink converts to the tracker's
            native rendering language at the boundary (Jira wiki markup,
            Monday rich-text JSON, etc.) — bodies should NOT pre-render.
        type: Issue type name. Jira-flavored examples: ``"Bug"``,
            ``"Task"``, ``"Story"``, ``"Epic"``, ``"Sub-task"``. Other
            trackers may have their own vocabulary.
        parent_key: Parent issue key for sub-task linkage. Distinct from
            ``epic_key`` — sub-task → parent task vs task → parent epic.
        epic_key: Parent epic for non-sub-task issues. Routed through the
            tracker's epic-link mechanism (Jira customfield ``Epic Link``
            on Server, ``parent`` on next-gen).
        assignee: Display name (e.g. ``"Sharon Gordon"``). The sink resolves
            via the configured :class:`AssigneeResolver`. ``None`` leaves
            the ticket unassigned.
        labels: Free-form labels. Sinks may augment with content markers
            (e.g. ``ai-generated``) per their own conventions.
        components: Tracker-side component names. Sinks filter against the
            project's live component list to avoid invented values; unknown
            components are silently dropped.
        priority: Priority name (e.g. ``"P0 - Must have"``). Tracker-
            specific value; bodies map their internal P0/P1/... vocabulary
            to the live tracker priority strings before constructing the
            Ticket.
        custom_fields: Open bag for tracker-specific extras. Keys are the
            tracker's native field names (e.g. ``"Severity"``,
            ``"Found in build"``); the JiraSink translates these to
            ``customfield_NNNNN`` ids via field-name discovery.
    """

    summary: str
    description: str
    type: str
    parent_key: str | None = None
    epic_key: str | None = None
    assignee: str | None = None
    labels: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    priority: str | None = None
    custom_fields: dict[str, Any] = field(default_factory=dict)


class IdentificationStrategy(Protocol):
    """How to detect "is this ticket already in the tracker?"

    Returns the existing key on hit, ``None`` on miss. Implementations:
    - ``LabelSearchStrategy``: JQL by label (f2j's
      ``upstream:<external_id>``).
    - ``CacheTrustStrategy``: trust an external cache (jira_task_agent's
      Tier 3 matcher cache).
    """

    def find(self, sink: "TicketSink", ticket: Ticket) -> str | None: ...


class AssigneeResolver(Protocol):
    """Display name → tracker-native id.

    Returns ``None`` if the name can't be resolved — sinks then leave the
    ticket unassigned rather than failing the whole upload.
    """

    def resolve(self, display_name: str) -> str | None: ...


class EpicRouter(Protocol):
    """Decide which epic key (if any) a created ticket should link to.

    Implementations:
    - ``DeterministicChainStrategy``: external_id-prefix → module → LLM
      pick → default (f2j's pattern).
    - ``NoOpStrategy``: trust ``ticket.epic_key`` already set by the body
      (jira_task_agent's pattern — epics come from the extract+match step).

    Tracker-flavored concept. Other trackers will define their own routing
    abstractions; this lives under ``sinks/jira/strategies/``.
    """

    def route(self, ticket: Ticket, *, hint: str | None = None) -> str | None: ...


class TicketSink(Protocol):
    """Tracker-agnostic ticket sink.

    Implementations:
    - ``JiraSink``: today's only impl, wraps ``JiraClient``.
    - Future: ``MondaySink``, ``LinearSink``, ``GitHubIssuesSink``, ...
    """

    def create(self, ticket: Ticket) -> str:
        """Create a new ticket in the tracker. Returns the ticket key."""
        ...

    def update(self, key: str, ticket: Ticket) -> None:
        """Update an existing ticket. Diff-aware vs full overwrite is
        impl-defined — current JiraSink overwrites.
        """
        ...

    def find_existing(
        self,
        ticket: Ticket,
        strategy: IdentificationStrategy,
    ) -> str | None:
        """Apply ``strategy`` to detect a pre-existing ticket. Returns the
        key on hit, ``None`` on miss.
        """
        ...

    def comment(self, key: str, body: str) -> None:
        """Post a comment on ``key``. Body is markdown; sinks convert to
        the tracker's native format.
        """
        ...

    def transition(self, key: str, status: str) -> bool:
        """Move ``key`` to a state with the given status name. Returns
        True on success, False if no matching transition is available.
        """
        ...

    def search(self, query: str) -> Iterable[dict[str, Any]]:
        """Run a tracker-native query (Jira: JQL). Returns issue dicts."""
        ...

    def get_issue(self, key: str) -> dict[str, Any]:
        """Fetch a single issue by key."""
        ...
