"""Build ReconcilePlans from a list of `DirtySection`.

Pure logic. For each section emits one `EpicGroup` whose epic_action is
one of `create_epic` / `update_epic` / `skip_completed_epic` / `noop`,
plus per-task `create_task` / `update_task` / `covered_by_rollup`
actions and any orphan keys from the matcher. The doc is the source
of truth: every dirty item produces a write action carrying the
extracted body.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..jira.client import JiraClient, get_issue
from .dirty_filter import DirtySection, DirtyTask
from .matcher import MatchDecision


@dataclass
class ProjectEpicsIndex:
    """Lazy cache of project epics + their remote links."""
    client: JiraClient
    project_key: str
    _epics: list[dict] | None = None
    _remote_links_cache: dict[str, list[dict]] = field(default_factory=dict)

    def epics(self) -> list[dict]:
        if self._epics is None:
            jql = (
                f'project = "{self.project_key}" '
                f"AND issuetype = Epic ORDER BY updated DESC"
            )
            self._epics = self.client.search(jql, fields=["summary"])
        return self._epics

    def remote_links(self, key: str) -> list[dict]:
        if key not in self._remote_links_cache:
            self._remote_links_cache[key] = self.client.get_remote_links(key)
        return self._remote_links_cache[key]


@dataclass
class Action:
    kind: str
    target_key: str | None = None
    epic_key: str | None = None
    epic_anchor: str | None = None
    summary: str | None = None
    description: str | None = None
    assignee_username: str | None = None
    source_anchor: str | None = None
    match_confidence: float | None = None
    match_reason: str | None = None
    note: str | None = None


@dataclass
class EpicGroup:
    epic_action: Action
    task_actions: list[Action] = field(default_factory=list)


@dataclass
class ReconcilePlan:
    file_id: str
    file_name: str
    role: str
    groups: list[EpicGroup] = field(default_factory=list)

    @property
    def actions(self) -> list[Action]:
        out: list[Action] = []
        for g in self.groups:
            out.append(g.epic_action)
            out.extend(g.task_actions)
        return out


_COMPLETED_EPIC_STATUSES = frozenset({
    "In Staging", "In Review", "Done", "Closed", "Resolved",
    "Cancelled", "Won't Do", "Won't Fix",
})


def build_plans_from_dirty(
    sections: list[DirtySection],
    *,
    client: JiraClient,
) -> list[ReconcilePlan]:
    plans_by_file: dict[str, ReconcilePlan] = {}
    for section in sections:
        plan = plans_by_file.get(section.file_id)
        if plan is None:
            plan = ReconcilePlan(
                file_id=section.file_id,
                file_name=section.file_name,
                role=section.role,
            )
            plans_by_file[section.file_id] = plan
        plan.groups.append(_build_epic_group(section, client))
    return list(plans_by_file.values())


def _build_epic_group(section: DirtySection, client: JiraClient) -> EpicGroup:
    epic_action = _build_epic_action(section, client)
    if epic_action.kind == "skip_completed_epic":
        return EpicGroup(epic_action=epic_action, task_actions=[])
    epic_key = (
        epic_action.target_key
        if epic_action.kind in ("update_epic", "noop")
        else None
    )
    task_actions = _build_task_actions(section, client, epic_key)
    return EpicGroup(epic_action=epic_action, task_actions=task_actions)


def _build_epic_action(section: DirtySection, client: JiraClient) -> Action:
    epic_anchor = f"{section.file_id}#{section.section_index}"

    if section.matched_jira_key is None:
        return Action(
            kind="create_epic",
            epic_anchor=epic_anchor,
            summary=section.extracted_epic_summary,
            description=section.extracted_epic_description,
            assignee_username=_resolve_assignee(
                client, section.extracted_epic_assignee_raw,
            ),
            note="no existing epic matched",
        )

    live_status = get_issue(section.matched_jira_key, client=client).get("status")
    if live_status in _COMPLETED_EPIC_STATUSES:
        return Action(
            kind="skip_completed_epic",
            target_key=section.matched_jira_key,
            epic_anchor=epic_anchor,
            match_confidence=section.epic_match_confidence,
            match_reason=section.epic_match_reason,
            note=(
                f"matched epic {section.matched_jira_key} is in completed "
                f"status {live_status!r}; agent will not propose changes here."
            ),
        )

    if not section.epic_dirty:
        return Action(
            kind="noop",
            target_key=section.matched_jira_key,
            epic_anchor=epic_anchor,
            match_confidence=section.epic_match_confidence,
            match_reason=section.epic_match_reason,
            note="epic body unchanged; processing dirty tasks under it",
        )

    return Action(
        kind="update_epic",
        target_key=section.matched_jira_key,
        epic_anchor=epic_anchor,
        summary=section.extracted_epic_summary,
        description=section.extracted_epic_description,
        assignee_username=_resolve_assignee(
            client, section.extracted_epic_assignee_raw,
        ),
        match_confidence=section.epic_match_confidence,
        match_reason=section.epic_match_reason,
    )


def _build_task_actions(
    section: DirtySection,
    client: JiraClient,
    epic_key: str | None,
) -> list[Action]:
    epic_anchor = f"{section.file_id}#{section.section_index}"
    matches_per_key = _count_matches(section.tasks)
    actions = [
        _build_task_action(t, client, epic_key, epic_anchor, matches_per_key)
        for t in section.tasks
    ]
    consumed = set(matches_per_key.keys())
    for k in section.orphan_keys:
        if k in consumed:
            continue
        actions.append(Action(
            kind="orphan",
            target_key=k,
            epic_key=epic_key,
            epic_anchor=epic_anchor,
            note="existing child has no match in current extraction",
        ))
    return actions


def _build_task_action(
    t: DirtyTask,
    client: JiraClient,
    epic_key: str | None,
    epic_anchor: str,
    matches_per_key: dict[str, int],
) -> Action:
    decision = t.decision
    matched_key = decision.candidate_key
    task_assignee = _resolve_assignee(client, t.extracted.assignee_name)

    if matched_key is None:
        return Action(
            kind="create_task",
            epic_key=epic_key,
            epic_anchor=epic_anchor,
            summary=t.extracted.summary,
            description=t.extracted.description,
            assignee_username=task_assignee,
            source_anchor=t.extracted.source_anchor,
            match_confidence=decision.confidence,
            match_reason=decision.reason,
        )
    if matches_per_key.get(matched_key, 0) > 1:
        return Action(
            kind="covered_by_rollup",
            target_key=matched_key,
            epic_key=epic_key,
            epic_anchor=epic_anchor,
            summary=t.extracted.summary,
            source_anchor=t.extracted.source_anchor,
            match_confidence=decision.confidence,
            match_reason=decision.reason,
            note=(
                f"this extracted task is one of "
                f"{matches_per_key[matched_key]} doc-tasks already "
                f"covered by the rollup issue {matched_key}"
            ),
        )
    return Action(
        kind="update_task",
        target_key=matched_key,
        epic_key=epic_key,
        epic_anchor=epic_anchor,
        summary=t.extracted.summary,
        description=t.extracted.description,
        assignee_username=task_assignee,
        source_anchor=t.extracted.source_anchor,
        match_confidence=decision.confidence,
        match_reason=decision.reason,
    )


def _count_matches(tasks: list[DirtyTask]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tasks:
        key = t.decision.candidate_key
        if key:
            out[key] = out.get(key, 0) + 1
    return out


def _resolve_assignee(client: JiraClient, raw: str | None) -> str | None:
    return client.resolve_assignee_username(raw) if raw else None
