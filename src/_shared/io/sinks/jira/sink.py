"""JiraSink — wraps :class:`JiraClient` to expose the :class:`TicketSink` protocol.

Translates the generic :class:`Ticket` shape into Jira REST payloads,
applying:
- Markdown → wiki conversion (handled inside JiraClient).
- Assignee resolution via the configured :class:`AssigneeResolver`.
- Epic-link routing via the configured :class:`EpicRouter`.
- Component filtering against the project's live component list — unknown
  names are silently dropped (avoids "Component name 'X' is not valid"
  upload failures from invented LLM output).
- Priority + customfield passthrough.

The actual idempotency check (label-search vs cache-trust) lives in the
:class:`IdentificationStrategy` you pass to :meth:`find_existing`.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .client import JiraClient

from ..base import (
    AssigneeResolver,
    EpicRouter,
    IdentificationStrategy,
    Ticket,
)
from .strategies import NoOpStrategy, PassthroughAssigneeResolver


class JiraSink:
    """:class:`TicketSink` impl backed by drive's :class:`JiraClient`.

    Args:
        client: A ready-to-use :class:`JiraClient` (from
            ``JiraClient.from_env()`` or constructed directly).
        project_key: Default project key for created tickets.
        assignee_resolver: How to translate ``ticket.assignee`` (display
            name) to a tracker-native username. Defaults to passthrough.
        epic_router: How to compute the parent epic for a ticket. Defaults
            to using ``ticket.epic_key`` as-is.
        add_ai_generated_label: If True, the JiraSink's ``create`` adds the
            ``ai-generated`` label automatically (the jira_task_agent
            convention). If False, only the labels in ``ticket.labels`` are
            sent (f2j's bug-ticket convention).
        filter_components: If True, fetches the project's live component
            list once and drops any ticket components that aren't on it.
            Set False only if the project has known components but you
            don't want filtering for some reason.
    """

    def __init__(
        self,
        *,
        client: JiraClient,
        project_key: str,
        assignee_resolver: AssigneeResolver | None = None,
        epic_router: EpicRouter | None = None,
        add_ai_generated_label: bool = True,
        filter_components: bool = True,
    ) -> None:
        self._client = client
        self.project_key = project_key
        self.assignee_resolver: AssigneeResolver = (
            assignee_resolver or PassthroughAssigneeResolver()
        )
        self.epic_router: EpicRouter = epic_router or NoOpStrategy()
        self.add_ai_generated_label = add_ai_generated_label
        self.filter_components = filter_components
        self._valid_components: set[str] | None = None  # lazy-loaded

    # ---- TicketSink protocol --------------------------------------------

    def create(self, ticket: Ticket) -> str:
        extra = self._build_extra_fields(ticket)
        epic_link = self.epic_router.route(ticket)
        # Epic Link routing only applies to non-Sub-task issues; sub-tasks
        # use the parent_key field directly.
        is_subtask = ticket.type.lower() in ("sub-task", "subtask")
        resp = self._client.create_issue(
            project_key=self.project_key,
            summary=ticket.summary,
            description=ticket.description,
            issue_type=ticket.type,
            epic_link=None if is_subtask else epic_link,
            parent_key=ticket.parent_key if is_subtask else None,
            extra_fields=extra or None,
            add_ai_generated_label=self.add_ai_generated_label,
        )
        key = resp.get("key", "")
        if not key:
            raise RuntimeError(f"Jira create_issue returned no key: {resp!r}")
        return key

    def update(self, key: str, ticket: Ticket) -> None:
        fields: dict[str, Any] = {}
        if ticket.summary:
            fields["summary"] = ticket.summary
        if ticket.description is not None:
            fields["description"] = ticket.description
        if ticket.priority:
            fields["priority"] = {"name": ticket.priority}
        if ticket.labels:
            labels = list(ticket.labels)
            if self.add_ai_generated_label and "ai-generated" not in labels:
                labels.append("ai-generated")
            fields["labels"] = labels
        if ticket.assignee:
            username = self.assignee_resolver.resolve(ticket.assignee)
            if username:
                fields["assignee"] = {"name": username}
        if ticket.components:
            allowed = self._allowed_components()
            filtered = [{"name": c} for c in ticket.components if c in allowed]
            if filtered:
                fields["components"] = filtered
        # custom_fields: leave as-is for callers that pre-translated
        # field-name → customfield_id; otherwise the JiraClient handles
        # well-known names internally.
        if ticket.custom_fields:
            fields.update(ticket.custom_fields)
        if fields:
            self._client.update_issue(key, fields)

    def find_existing(
        self,
        ticket: Ticket,
        strategy: IdentificationStrategy,
    ) -> str | None:
        return strategy.find(self, ticket)

    def comment(self, key: str, body: str) -> None:
        self._client.post_comment(key, body)

    def transition(self, key: str, status: str) -> bool:
        return self._client.transition_issue(key, status)

    def search(self, query: str) -> Iterable[dict[str, Any]]:
        return self._client.search(query)

    def get_issue(self, key: str) -> dict[str, Any]:
        return self._client.get(f"/issue/{key}")

    def get_issue_normalized(self, key: str) -> dict[str, Any]:
        """Return the normalized form (drive's flat shape: top-level
        ``status``, ``assignee_username``, ``priority``, etc.) of issue
        ``key``.

        Use this for plan-building (drive's reconciler reads ``status`` to
        decide ``skip_completed_epic``). For raw REST output, use
        :meth:`get_issue` instead.
        """
        from .client import _ISSUE_FIELDS, _normalize_issue
        raw = self._client.get(
            f"/issue/{key}",
            params={"fields": ",".join(_ISSUE_FIELDS)},
        )
        return _normalize_issue(raw)

    def fetch_project_tree(
        self,
        project_key: str | None = None,
        *,
        log: Any = None,
    ) -> dict[str, Any]:
        """Return the project's epic tree (one paginated /search call).

        Defaults ``project_key`` to ``self.project_key`` if not given. Used
        by jira_task_agent's matcher to compute the topology hash.
        """
        from .project_tree import fetch_project_tree as _fetch
        return _fetch(self._client, project_key or self.project_key, log=log)

    # ---- Helpers exposed for strategy plug-ins --------------------------

    @property
    def client(self) -> JiraClient:
        """The underlying JiraClient — exposed so strategies (e.g. the
        PickerWithCache assignee resolver) can call ``search_user_picker``
        without needing to construct another client.
        """
        return self._client

    # ---- Internals ------------------------------------------------------

    def _build_extra_fields(self, ticket: Ticket) -> dict[str, Any]:
        """Translate Ticket-level extras (assignee, components, priority,
        labels, custom_fields) into the ``extra_fields`` dict that
        ``JiraClient.create_issue`` accepts."""
        extra: dict[str, Any] = {}
        if ticket.assignee:
            username = self.assignee_resolver.resolve(ticket.assignee)
            if username:
                extra["assignee"] = {"name": username}
        if ticket.priority:
            extra["priority"] = {"name": ticket.priority}
        if ticket.labels:
            base = list(ticket.labels)
            if self.add_ai_generated_label and "ai-generated" not in base:
                # JiraClient.create_issue adds it itself when the flag is
                # True; including the user's labels in extra_fields with
                # the ai-generated label appended preserves both.
                base.append("ai-generated")
            extra["labels"] = base
        if ticket.components:
            allowed = self._allowed_components()
            filtered = [{"name": c} for c in ticket.components if c in allowed]
            if filtered:
                extra["components"] = filtered
        if ticket.custom_fields:
            extra.update(ticket.custom_fields)
        return extra

    def _allowed_components(self) -> set[str]:
        """Lazy-fetch and cache the project's live component name set."""
        if not self.filter_components:
            return set()  # caller filters nothing — but JiraSink path won't reach here
        if self._valid_components is None:
            comps = self._client.get_components(self.project_key)
            self._valid_components = {
                str(c.get("name", "")) for c in comps if c.get("name")
            }
        return self._valid_components


class CapturingJiraSink(JiraSink):
    """:class:`JiraSink` variant that records writes to ``captured_writes``
    instead of sending them.

    Used by jira_task_agent's ``--capture`` mode: drives the same plan-build
    + reconcile path as :class:`JiraSink`, but writes that flow through the
    underlying client's ``.post`` / ``.put`` (i.e. ``create`` / ``update`` /
    ``comment`` / ``transition``) get appended to ``captured_writes`` and
    issue-create POSTs return synthetic ``CAPTURED-N`` keys.

    Reads (``search``, ``get_issue``, ``get_issue_normalized``,
    ``fetch_project_tree``, ``find_existing``) pass through to the underlying
    :class:`JiraClient` so the run sees the real Jira state.

    Implementation: rewires the underlying client's ``.post`` / ``.put`` to
    recorder closures. Any caller that constructs a ``CapturingJiraSink``
    around a live client must accept that the client's HTTP write methods are
    irreversibly redirected for the lifetime of that client.
    """

    def __init__(
        self,
        *,
        client: JiraClient,
        project_key: str,
        assignee_resolver: AssigneeResolver | None = None,
        epic_router: EpicRouter | None = None,
        add_ai_generated_label: bool = True,
        filter_components: bool = True,
    ) -> None:
        super().__init__(
            client=client,
            project_key=project_key,
            assignee_resolver=assignee_resolver,
            epic_router=epic_router,
            add_ai_generated_label=add_ai_generated_label,
            filter_components=filter_components,
        )
        self.captured_writes: list[dict[str, Any]] = []
        self._install_capture()

    def _install_capture(self) -> None:
        captured = self.captured_writes
        counter = {"n": 0}
        client = self._client

        def fake_post(path: str, json_body: dict[str, Any]) -> dict[str, Any]:
            counter["n"] += 1
            captured.append(
                {"method": "POST", "path": path, "body": json_body}
            )
            if path == "/issue":
                return {
                    "id": str(counter["n"]),
                    "key": f"CAPTURED-{counter['n']}",
                    "self": "(captured)",
                }
            return {}

        def fake_put(path: str, json_body: dict[str, Any]) -> None:
            counter["n"] += 1
            captured.append(
                {"method": "PUT", "path": path, "body": json_body}
            )

        client.post = fake_post  # type: ignore[method-assign]
        client.put = fake_put  # type: ignore[method-assign]
