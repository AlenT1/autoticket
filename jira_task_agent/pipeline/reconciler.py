"""Build ReconcilePlans (create / update / no-op / orphan actions) from a
pre-computed MatcherResult.

The reconciler is now PURE LOGIC — no LLM calls. The matcher is run
once per `run_once()` ahead of time (see `pipeline.matcher.run_matcher`),
producing one `FileEpicResult` per extracted epic. The reconciler:

  1. Fetches live Jira issue content for matched epics + their children.
  2. Compares (after normalizing markdown→wiki on the extracted side).
  3. Emits Actions:
       create_epic, update_epic, noop                (epic-level)
       create_task, update_task, noop, orphan        (task-level)
       skip_completed_epic                           (status-guard)
       covered_by_rollup                             (rollup pattern)

This lets the runner separate "what pairs with what" (LLM, batched) from
"what to write to Jira given the pairing" (deterministic, per-file).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..drive.client import DriveFile
from ..jira.client import JiraClient, get_issue, list_epic_children
from .extractor import (
    AGENT_MARKER,
    ExtractedTask,
    ExtractionResult,
    MultiExtractionResult,
)
from .matcher import FileEpicResult, MatcherResult, MatchDecision


# ----------------------------------------------------------------------
# Per-run cache of project epics + their remote links (still useful for
# the runner to seed the matcher's project_tree, plus for remote-link
# back-pointers on adopted epics)
# ----------------------------------------------------------------------


@dataclass
class ProjectEpicsIndex:
    """Lazy cache of `project = X AND issuetype = Epic` results, plus the
    remote links of each candidate epic. Constructed once per run.

    Used by the runner to:
      - feed `pipeline.matcher.run_matcher` as candidates
      - look up an epic's remote links when deciding whether to attach a
        new back-pointer to the source doc
    """
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


# ----------------------------------------------------------------------
# Action / EpicGroup / ReconcilePlan dataclasses
# ----------------------------------------------------------------------


@dataclass
class Action:
    kind: str
    # kind values:
    #   create_epic         — new epic to be created
    #   update_epic         — existing epic, content differs, will be updated
    #   create_task         — new task to be created under an epic
    #   update_task         — existing task, content differs, will be updated
    #   noop                — matched, content equal — nothing to do
    #   orphan              — Jira child has no extracted-task counterpart
    #   skip_completed_epic — matched epic is in a completed status (Done /
    #                          In Staging / In Review / Closed / Cancelled /
    #                          Resolved). The agent treats the work as done
    #                          and proposes no creates / updates under this
    #                          epic for this run. Suppresses task actions.
    #   covered_by_rollup   — extracted task's match_key is shared with one
    #                          or more sibling tasks. Treated as "already
    #                          covered by a Jira rollup issue"; no Jira write.
    target_key: str | None = None
    epic_key: str | None = None
    epic_anchor: str | None = None
    summary: str | None = None
    description: str | None = None
    assignee_username: str | None = None
    before_summary: str | None = None
    before_description: str | None = None
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
    role: str  # "single_epic" | "multi_epic"
    groups: list[EpicGroup] = field(default_factory=list)

    @property
    def actions(self) -> list[Action]:
        out: list[Action] = []
        for g in self.groups:
            out.append(g.epic_action)
            out.extend(g.task_actions)
        return out


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


# Statuses that indicate the matched epic's work is effectively done or
# in flight to production. We don't propose changes under such epics —
# the planning doc is presumed stale relative to Jira.
_COMPLETED_EPIC_STATUSES = frozenset(
    {
        "In Staging",
        "In Review",
        "Done",
        "Closed",
        "Resolved",
        "Cancelled",
        "Won't Do",
        "Won't Fix",
    }
)


def _normalize(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace(AGENT_MARKER, "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _descriptions_equal(extracted_md: str | None, live_text: str | None) -> bool:
    """Compare extracted (markdown) vs live (Jira-wiki, post-conversion).

    Live is already in wiki form because the agent runs the conversion
    on every create/update. Extracted is markdown. We run extracted
    through the conversion to align both before normalizing.
    """
    extracted_wiki = JiraClient._md_to_jira_wiki(extracted_md or "")
    return _normalize(extracted_wiki) == _normalize(live_text)


def _resolve_assignee(client: JiraClient, raw: str | None) -> str | None:
    return client.resolve_assignee_username(raw) if raw else None


# ----------------------------------------------------------------------
# Action builders
# ----------------------------------------------------------------------


def _build_epic_action(
    *,
    epic_key: str | None,
    extracted_summary: str,
    extracted_description: str,
    epic_assignee: str | None,
    epic_anchor: str,
    client: JiraClient,
    match_confidence: float | None,
    match_reason: str | None,
    create_note: str,
) -> tuple[Action, dict | None]:
    """Returns (epic_action, live_issue_or_None).

    `live_issue_or_None` is exposed so callers can read the live status
    and decide whether to short-circuit task processing for completed
    epics.
    """
    if epic_key is None:
        return (
            Action(
                kind="create_epic",
                epic_anchor=epic_anchor,
                summary=extracted_summary,
                description=extracted_description,
                assignee_username=epic_assignee,
                note=create_note,
            ),
            None,
        )
    live = get_issue(epic_key, client=client)

    # Status guard: matched epic is effectively done in Jira → suppress.
    live_status = live.get("status")
    if live_status in _COMPLETED_EPIC_STATUSES:
        return (
            Action(
                kind="skip_completed_epic",
                target_key=epic_key,
                epic_anchor=epic_anchor,
                match_confidence=match_confidence,
                match_reason=match_reason,
                note=(
                    f"matched epic {epic_key} is in completed status "
                    f"{live_status!r}; agent will not propose changes here. "
                    f"The doc is likely stale relative to live Jira state."
                ),
            ),
            live,
        )

    # No-rename rule on adoption: if the matcher picked the wrong epic,
    # the title is the loudest visible signal. Renaming someone else's
    # epic is the worst-case false-positive blast. We always keep the
    # live summary; only the description (and tasks under it) may be
    # touched by an adoption.
    live_summary = live.get("summary") or ""
    if _descriptions_equal(extracted_description, live.get("description")):
        return (
            Action(
                kind="noop",
                target_key=epic_key,
                epic_anchor=epic_anchor,
                match_confidence=match_confidence,
                match_reason=match_reason,
                note="epic content unchanged (summary kept by no-rename rule)",
            ),
            live,
        )
    return (
        Action(
            kind="update_epic",
            target_key=epic_key,
            epic_anchor=epic_anchor,
            summary=live_summary,                       # no rename on adoption
            description=extracted_description,
            assignee_username=epic_assignee,
            before_summary=live_summary,
            before_description=live.get("description"),
            match_confidence=match_confidence,
            match_reason=match_reason,
            note=(
                f"adopting existing epic; summary kept as {live_summary!r} "
                f"(extractor proposed {extracted_summary!r}, ignored)"
            ),
        ),
        live,
    )


def _build_task_actions(
    *,
    extracted_tasks: list[ExtractedTask],
    task_decisions: list[MatchDecision],
    existing_children: list[dict],
    orphan_keys: list[str],
    epic_key: str | None,
    epic_anchor: str,
    client: JiraClient,
    dirty_anchors: set[str] | None = None,
) -> list[Action]:
    """Build per-task Actions given the matcher's decisions.

    Detects "rollup" pattern: when MULTIPLE extracted tasks share the same
    matched candidate_key, the candidate is treated as a Jira issue whose
    description covers several doc tasks. ALL such extracted tasks become
    `covered_by_rollup` actions (no Jira write). The candidate is treated
    as in-scope; it is NOT an orphan even though no single extracted task
    matched it 1:1.
    """
    children_by_key: dict[str, dict] = {c["key"]: c for c in existing_children if c.get("key")}

    # Pass 1: count how many extracted tasks share each matched key.
    matches_per_key: dict[str, int] = {}
    for d in task_decisions:
        if d.candidate_key:
            matches_per_key[d.candidate_key] = matches_per_key.get(d.candidate_key, 0) + 1

    actions: list[Action] = []

    # Pass 2: emit per-task Actions.
    for t, decision in zip(extracted_tasks, task_decisions):
        task_assignee = _resolve_assignee(client, t.assignee_name)
        matched_key = decision.candidate_key

        # Skip tasks the doc didn't change. `dirty_anchors=None` means
        # "no diff context" (cold path) → process all.
        if (
            dirty_anchors is not None
            and t.source_anchor
            and t.source_anchor not in dirty_anchors
        ):
            actions.append(
                Action(
                    kind="noop",
                    target_key=matched_key,
                    epic_key=epic_key,
                    epic_anchor=epic_anchor,
                    source_anchor=t.source_anchor,
                    match_confidence=decision.confidence,
                    match_reason=decision.reason,
                    note="task unchanged in this run",
                )
            )
            continue

        if matched_key is None:
            actions.append(
                Action(
                    kind="create_task",
                    epic_key=epic_key,
                    epic_anchor=epic_anchor,
                    summary=t.summary,
                    description=t.description,
                    assignee_username=task_assignee,
                    source_anchor=t.source_anchor,
                    match_confidence=decision.confidence,
                    match_reason=decision.reason,
                )
            )
            continue

        # Rollup: multiple extracted tasks pointed at this same Jira issue.
        # That candidate is presumed to cover all of them in its description.
        # No Jira write — just surface the pairing for the report.
        if matches_per_key.get(matched_key, 0) > 1:
            actions.append(
                Action(
                    kind="covered_by_rollup",
                    target_key=matched_key,
                    epic_key=epic_key,
                    epic_anchor=epic_anchor,
                    summary=t.summary,
                    source_anchor=t.source_anchor,
                    match_confidence=decision.confidence,
                    match_reason=decision.reason,
                    note=(
                        f"this extracted task is one of "
                        f"{matches_per_key[matched_key]} doc-tasks already "
                        f"covered by the rollup issue {matched_key}"
                    ),
                )
            )
            continue

        # Normal 1:1 path.
        live = children_by_key.get(matched_key, {})
        if (
            _normalize(live.get("summary")) == _normalize(t.summary)
            and _descriptions_equal(t.description, live.get("description"))
        ):
            actions.append(
                Action(
                    kind="noop",
                    target_key=matched_key,
                    epic_key=epic_key,
                    epic_anchor=epic_anchor,
                    match_confidence=decision.confidence,
                    match_reason=decision.reason,
                    note="task content unchanged",
                )
            )
        else:
            actions.append(
                Action(
                    kind="update_task",
                    target_key=matched_key,
                    epic_key=epic_key,
                    epic_anchor=epic_anchor,
                    summary=t.summary,
                    description=t.description,
                    assignee_username=task_assignee,
                    before_summary=live.get("summary"),
                    before_description=live.get("description"),
                    source_anchor=t.source_anchor,
                    match_confidence=decision.confidence,
                    match_reason=decision.reason,
                )
            )

    # Orphans: existing-but-not-paired children.
    # A candidate cited by any decision (even multiple times via rollup)
    # is considered "in scope" — not an orphan.
    consumed = set(matches_per_key.keys())
    for k in orphan_keys:
        if k in consumed:
            continue
        live = children_by_key.get(k, {})
        actions.append(
            Action(
                kind="orphan",
                target_key=k,
                epic_key=epic_key,
                epic_anchor=epic_anchor,
                summary=live.get("summary"),
                note="existing child has no match in current extraction",
            )
        )

    return actions


# ----------------------------------------------------------------------
# Public: build ReconcilePlans from a MatcherResult
# ----------------------------------------------------------------------


def build_plans_from_match(
    matcher_result: MatcherResult,
    extractions: list[tuple[DriveFile, ExtractionResult | MultiExtractionResult]],
    *,
    client: JiraClient,
    dirty_anchors_per_file: dict[str, set[str] | None] | None = None,
) -> list[ReconcilePlan]:
    """Group `matcher_result.file_results` by file_id and build one
    `ReconcilePlan` per file. Each plan contains one `EpicGroup` per
    extracted epic (1 for single_epic, N for multi_epic).

    `dirty_anchors_per_file[file_id]` gates write actions:
      - None entry (or missing) → process all tasks in that file.
      - empty set → no tasks changed; all tasks emit noop.
      - populated set → only items whose identifier is in the set emit
        write actions; the rest emit noop. Identifiers are task
        `source_anchor` strings or `"<epic>:N"` for the Nth section's epic.
    """
    extractions_by_file_id: dict[str, tuple[DriveFile, object]] = {
        ext.file_id: (df, ext) for df, ext in extractions
    }
    dirty_per_file = dirty_anchors_per_file or {}

    grouped_by_file: dict[str, list[FileEpicResult]] = {}
    for fr in matcher_result.file_results:
        grouped_by_file.setdefault(fr.file_id, []).append(fr)

    plans: list[ReconcilePlan] = []
    for file_id, file_results in grouped_by_file.items():
        if file_id not in extractions_by_file_id:
            # Stray result with no matching extraction; skip.
            continue
        df, ext = extractions_by_file_id[file_id]
        is_multi = hasattr(ext, "epics")
        role = "multi_epic" if is_multi else "single_epic"

        # Sort by section_index so multi_epic plans are stable
        file_results.sort(key=lambda fr: fr.section_index)

        groups: list[EpicGroup] = []
        for fr in file_results:
            tasks_for_section = (
                ext.epics[fr.section_index].tasks if is_multi else ext.tasks
            )
            epic_assignee = _resolve_assignee(
                client, fr.extracted_epic_assignee_raw
            )
            create_note = (
                "no existing epic matched"
                if fr.matched_jira_key is None
                else "matched via LLM matcher"
            )
            file_dirty = dirty_per_file.get(file_id)
            section_anchors = {
                t.source_anchor for t in tasks_for_section if t.source_anchor
            }
            epic_token = f"<epic>:{fr.section_index}"
            epic_is_dirty = (
                file_dirty is None
                or fr.matched_jira_key is None
                or bool(section_anchors & file_dirty)
                or epic_token in file_dirty
            )

            if not epic_is_dirty:
                epic_action = Action(
                    kind="noop",
                    target_key=fr.matched_jira_key,
                    epic_anchor=f"{fr.file_id}#{fr.section_index}",
                    match_confidence=fr.epic_match_confidence,
                    match_reason=fr.epic_match_reason,
                    note="section unchanged in this run",
                )
                task_actions: list[Action] = [
                    Action(
                        kind="noop",
                        target_key=decision.candidate_key,
                        epic_key=fr.matched_jira_key,
                        epic_anchor=f"{fr.file_id}#{fr.section_index}",
                        source_anchor=t.source_anchor,
                        match_confidence=decision.confidence,
                        match_reason=decision.reason,
                        note="task unchanged in this run",
                    )
                    for t, decision in zip(tasks_for_section, fr.task_decisions)
                ]
                groups.append(EpicGroup(epic_action=epic_action, task_actions=task_actions))
                continue

            epic_action, live_epic = _build_epic_action(
                epic_key=fr.matched_jira_key,
                extracted_summary=fr.extracted_epic_summary,
                extracted_description=fr.extracted_epic_description,
                epic_assignee=epic_assignee,
                epic_anchor=f"{fr.file_id}#{fr.section_index}",
                client=client,
                match_confidence=fr.epic_match_confidence,
                match_reason=fr.epic_match_reason,
                create_note=create_note,
            )

            # Suppress task-level actions when the matched epic is in
            # a completed status — the doc is presumed stale.
            if epic_action.kind == "skip_completed_epic":
                task_actions = []
            elif fr.matched_jira_key is None:
                task_actions = []
                for t in tasks_for_section:
                    if (
                        file_dirty is not None
                        and t.source_anchor
                        and t.source_anchor not in file_dirty
                    ):
                        task_actions.append(
                            Action(
                                kind="noop",
                                epic_key=None,
                                epic_anchor=f"{fr.file_id}#{fr.section_index}",
                                source_anchor=t.source_anchor,
                                note="task unchanged in this run",
                            )
                        )
                        continue
                    task_actions.append(
                        Action(
                            kind="create_task",
                            epic_key=None,
                            epic_anchor=f"{fr.file_id}#{fr.section_index}",
                            summary=t.summary,
                            description=t.description,
                            assignee_username=_resolve_assignee(client, t.assignee_name),
                            source_anchor=t.source_anchor,
                            note="part of new-epic creation",
                        )
                    )
            else:
                existing_children = list_epic_children(
                    fr.matched_jira_key, client=client
                )
                task_actions = _build_task_actions(
                    extracted_tasks=list(tasks_for_section),
                    task_decisions=fr.task_decisions,
                    existing_children=existing_children,
                    orphan_keys=fr.orphan_keys,
                    epic_key=fr.matched_jira_key,
                    epic_anchor=f"{fr.file_id}#{fr.section_index}",
                    client=client,
                    dirty_anchors=dirty_per_file.get(file_id),
                )

            groups.append(
                EpicGroup(epic_action=epic_action, task_actions=task_actions)
            )

        plans.append(
            ReconcilePlan(
                file_id=ext.file_id,
                file_name=ext.file_name,
                role=role,
                groups=groups,
            )
        )

    return plans
