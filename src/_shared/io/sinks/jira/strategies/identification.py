"""Identification strategies — "is this ticket already in Jira?"

Two patterns:
- :class:`LabelSearchStrategy` — JQL by stable label
  (f2j's ``upstream:<external_id>``). Survives cache eviction; doubles as
  human-readable Jira-UI search.
- :class:`CacheTrustStrategy` — trust an externally-maintained map
  (jira_task_agent's Tier-3 matcher cache). Re-runs the LLM matcher if
  the cache evicts.
"""
from __future__ import annotations

from collections.abc import Mapping

from ...base import Ticket, TicketSink


class LabelSearchStrategy:
    """Identify by a stable label string templated from ticket fields.

    Args:
        label_template: Template string with ``{<key>}`` placeholders. Keys
            resolve against ``Ticket.custom_fields`` first, then well-known
            attributes (``summary``, ``type``). Example for f2j:
            ``"upstream:{external_id}"`` where each Ticket has
            ``custom_fields["external_id"] = "CORE-CHAT-026"``.
        project_key: Optional Jira project key to scope the JQL search.
            Recommended — without it, the search is global.

    Behavior on miss: returns ``None``. Behavior on hit: returns the first
    matching key (search ordered by created DESC so latest wins on
    accidental duplicates).
    """

    def __init__(
        self,
        *,
        label_template: str,
        project_key: str | None = None,
    ) -> None:
        self.label_template = label_template
        self.project_key = project_key

    def find(self, sink: TicketSink, ticket: Ticket) -> str | None:
        label = self._render_label(ticket)
        if not label:
            return None
        clauses = [f'labels = "{label}"']
        if self.project_key:
            clauses.append(f'project = "{self.project_key}"')
        jql = " AND ".join(clauses) + " ORDER BY created DESC"
        for issue in sink.search(jql):
            key = issue.get("key") if isinstance(issue, Mapping) else None
            if key:
                return str(key)
        return None

    def _render_label(self, ticket: Ticket) -> str | None:
        try:
            return self.label_template.format(
                **{**_well_known_fields(ticket), **ticket.custom_fields}
            )
        except KeyError:
            # Template referenced a key the ticket doesn't have — treat as
            # "no stable identity available", caller falls back to create.
            return None


class CacheTrustStrategy:
    """Identify via an externally-maintained cache mapping ticket → key.

    Args:
        cache: Any mapping ``{cache_key: jira_key}``. The cache key is
            resolved from the ticket via ``key_fn``.
        key_fn: Function ``(Ticket) -> cache_key``. Defaults to using
            ``ticket.custom_fields["cache_key"]`` if present.

    The mapping is read-only from this class's perspective. The owner
    (e.g. jira_task_agent's Tier-3 matcher) is responsible for populating
    and invalidating it.
    """

    def __init__(
        self,
        *,
        cache: Mapping[str, str],
        key_fn=None,
    ) -> None:
        self._cache = cache
        self._key_fn = key_fn or _default_cache_key

    def find(self, _sink: TicketSink, ticket: Ticket) -> str | None:
        # `_sink` unused — cache trust doesn't need to query the tracker.
        try:
            cache_key = self._key_fn(ticket)
        except (KeyError, AttributeError):
            return None
        if cache_key is None:
            return None
        return self._cache.get(str(cache_key))


def _well_known_fields(ticket: Ticket) -> dict[str, object]:
    """Subset of Ticket attrs exposed for label templating."""
    return {
        "summary": ticket.summary,
        "type": ticket.type,
        "epic_key": ticket.epic_key or "",
        "parent_key": ticket.parent_key or "",
    }


def _default_cache_key(ticket: Ticket) -> str | None:
    return ticket.custom_fields.get("cache_key")
