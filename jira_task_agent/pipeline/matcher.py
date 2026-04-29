"""LLM-based comparator: pairs extracted items with existing Jira issues.

Two entry points:

  - `match(items, candidates, *, kind)`
        flat one-shot match. Used for Stage 1 epic matching: ALL
        extracted epics across all files vs the project's epic list.

  - `match_grouped(groups, *, kind, batch_size, max_workers)`
        multiple independent matching problems run in batched LLM calls.
        Used for Stage 2 task matching: per matched epic, the extracted
        tasks vs that epic's existing children. Several epic-groups are
        packed into one prompt; batches run in parallel via a small
        thread pool.

  - `run_matcher(extractions, project_tree, ...)`
        the orchestrator. Produces a `MatcherResult` covering Stage 1
        and Stage 2 in one shot.

No fuzz heuristic anywhere — the LLM is the comparator.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from ..llm.client import chat, load_prompt, models_classify, render_prompt

_SYSTEM_PROMPT = load_prompt("matcher")
_GROUPED_SYSTEM_PROMPT = load_prompt("matcher_grouped")
# Send a generous preview so the matcher can recognize "rollup" issues —
# Jira issues whose description summarizes multiple work items in bullets.
# Smaller previews caused us to miss rollup pairings (e.g. CENTPM-1239's
# "Implemented scope:" bullet list covered ~10 V0 step-tasks).
_DESCRIPTION_PREVIEW_CHARS = 3000
# Confidence floors are kind-aware. Adopting an existing epic is a highly
# destructive op (clobbers description, hangs new tasks under someone
# else's epic), so we require near-certainty. Adopting an existing task
# under an already-matched epic is bounded in blast radius and the floor
# can stay lower.
_MIN_CONFIDENCE_BY_KIND = {"epic": 0.90, "task": 0.70}
_DEFAULT_MIN_CONFIDENCE = 0.70


def _min_confidence(kind: str) -> float:
    return _MIN_CONFIDENCE_BY_KIND.get(kind, _DEFAULT_MIN_CONFIDENCE)


@dataclass
class MatchDecision:
    item_index: int
    candidate_key: str | None
    confidence: float
    reason: str


@dataclass
class MatchInput:
    """Generic shape for either an extracted item or a candidate."""
    summary: str
    description: str = ""
    # Optional Jira key for candidates; not used for items.
    key: str | None = None
    # For epic-kind candidates: a compact list of the epic's direct
    # children, so the matcher can disambiguate generic-summary epics
    # ("UI Fixes for Production") whose children make their actual scope
    # clear. Each entry: {"key", "summary", "status"}. Empty list means
    # "no children" — itself a signal (a thin parent with nothing under
    # it should rarely be confidently adopted).
    children: list[dict] = field(default_factory=list)


def _serialize_items(items: list[MatchInput]) -> str:
    out = []
    for i, it in enumerate(items):
        out.append(
            {
                "index": i,
                "summary": it.summary,
                "description_preview": (it.description or "")[:_DESCRIPTION_PREVIEW_CHARS],
            }
        )
    return json.dumps(out, ensure_ascii=False, indent=2)


def _serialize_candidates(candidates: list[MatchInput]) -> str:
    out = []
    for c in candidates:
        entry: dict = {
            "key": c.key,
            "summary": c.summary,
            "description_preview": (c.description or "")[:_DESCRIPTION_PREVIEW_CHARS],
        }
        # Only emit children when present (epic candidates). Compact form:
        # one line per child; status included so a candidate full of Done
        # children is a stronger negative signal than one with active work.
        if c.children:
            entry["children"] = [
                {
                    "key": ch.get("key"),
                    "summary": (ch.get("summary") or "").strip(),
                    "status": ch.get("status"),
                }
                for ch in c.children
                if ch.get("key")
            ]
        out.append(entry)
    return json.dumps(out, ensure_ascii=False, indent=2)


def match(
    items: list[MatchInput],
    candidates: list[MatchInput],
    *,
    kind: str,
) -> list[MatchDecision]:
    """For each item, decide which candidate (if any) is the same work.

    `kind` is "epic" or "task". Affects the prompt's domain hints, not
    the structure.

    Returns a list of `MatchDecision` parallel to `items` (same length,
    same order). A decision with `candidate_key=None` means "no match —
    create new". Confidence below the module threshold is normalized to
    None as well.

    Empty items or empty candidates → no matches (every item is new).
    """
    if not items:
        return []
    if not candidates:
        return [
            MatchDecision(
                item_index=i,
                candidate_key=None,
                confidence=0.0,
                reason="no candidates available",
            )
            for i in range(len(items))
        ]

    user_msg = render_prompt(
        _SYSTEM_PROMPT,
        kind=kind,
        items_json=_serialize_items(items),
        candidates_json=_serialize_candidates(candidates),
    )
    parsed, _ = chat(
        system="You output strict JSON only. No prose, no markdown.",
        user=user_msg,
        models=models_classify(),
        temperature=0.0,
        json_mode=True,
    )

    raw_matches = (parsed or {}).get("matches") or []
    by_index: dict[int, dict] = {
        int(m.get("item_index", -1)): m for m in raw_matches if isinstance(m, dict)
    }

    used_candidates: set[str] = set()
    out: list[MatchDecision] = []
    floor = _min_confidence(kind)
    for i, _ in enumerate(items):
        m = by_index.get(i, {})
        key = m.get("candidate_key")
        confidence = float(m.get("confidence") or 0.0)
        reason = str(m.get("reason") or "")
        # Apply threshold.
        if not key or confidence < floor:
            out.append(
                MatchDecision(
                    item_index=i,
                    candidate_key=None,
                    confidence=confidence,
                    reason=reason or "no confident match",
                )
            )
            continue
        # Enforce no-double-mapping: if this candidate already used, drop.
        if key in used_candidates:
            out.append(
                MatchDecision(
                    item_index=i,
                    candidate_key=None,
                    confidence=confidence,
                    reason=f"{reason} (candidate {key} already mapped to a prior item)",
                )
            )
            continue
        used_candidates.add(key)
        out.append(
            MatchDecision(
                item_index=i,
                candidate_key=key,
                confidence=confidence,
                reason=reason,
            )
        )
    return out


# ----------------------------------------------------------------------
# Grouped matching (Stage 2: multiple independent task-match problems
# packed into batched LLM calls, run with a small thread pool)
# ----------------------------------------------------------------------


@dataclass
class GroupInput:
    """One independent matching problem inside a grouped LLM call."""
    group_id: str
    items: list[MatchInput]
    candidates: list[MatchInput]


@dataclass
class GroupResult:
    group_id: str
    decisions: list[MatchDecision]


def _serialize_group(g: GroupInput) -> dict:
    return {
        "group_id": g.group_id,
        "items": [
            {
                "index": i,
                "summary": it.summary,
                "description_preview": (it.description or "")[:_DESCRIPTION_PREVIEW_CHARS],
            }
            for i, it in enumerate(g.items)
        ],
        "candidates": [
            {
                "key": c.key,
                "summary": c.summary,
                "description_preview": (c.description or "")[:_DESCRIPTION_PREVIEW_CHARS],
            }
            for c in g.candidates
        ],
    }


def _parse_group_decisions(
    group: GroupInput, raw_matches: list[dict], *, kind: str = "task"
) -> GroupResult:
    """Parse one group's LLM-returned matches with the threshold check.

    NOTE: unlike the flat matcher (Stage 1, kind="epic"), we DO NOT drop
    a decision when its candidate_key collides with a prior one in the
    same group. The rollup pattern is legitimate at the task level:
    one Jira issue's description can cover several extracted tasks, and
    the matcher prompt explicitly allows multi-cite. The reconciler then
    detects rollup groupings and emits `covered_by_rollup` instead of
    duplicate creates.
    """
    by_index: dict[int, dict] = {
        int(m.get("item_index", -1)): m
        for m in raw_matches
        if isinstance(m, dict)
    }
    decisions: list[MatchDecision] = []
    floor = _min_confidence(kind)
    for i in range(len(group.items)):
        m = by_index.get(i, {})
        key = m.get("candidate_key")
        confidence = float(m.get("confidence") or 0.0)
        reason = str(m.get("reason") or "")
        if not key or confidence < floor:
            decisions.append(
                MatchDecision(
                    item_index=i,
                    candidate_key=None,
                    confidence=confidence,
                    reason=reason or "no confident match",
                )
            )
            continue
        decisions.append(
            MatchDecision(
                item_index=i,
                candidate_key=key,
                confidence=confidence,
                reason=reason,
            )
        )
    return GroupResult(group_id=group.group_id, decisions=decisions)


def _run_one_grouped_batch(
    batch: list[GroupInput], *, kind: str
) -> list[GroupResult]:
    """Single LLM call covering several groups in one prompt."""
    # Filter out empty groups (no items) — nothing to match.
    non_empty = [g for g in batch if g.items]
    if not non_empty:
        return [GroupResult(group_id=g.group_id, decisions=[]) for g in batch]

    user_msg = render_prompt(
        _GROUPED_SYSTEM_PROMPT,
        kind=kind,
        groups_json=json.dumps(
            [_serialize_group(g) for g in non_empty],
            ensure_ascii=False,
            indent=2,
        ),
    )
    parsed, _ = chat(
        system="You output strict JSON only. No prose, no markdown.",
        user=user_msg,
        models=models_classify(),
        temperature=0.0,
        json_mode=True,
    )

    raw_groups = (parsed or {}).get("groups") or []
    by_id: dict[str, list[dict]] = {}
    for g in raw_groups:
        if not isinstance(g, dict):
            continue
        gid = g.get("group_id")
        if gid is None:
            continue
        by_id[str(gid)] = g.get("matches") or []

    out: list[GroupResult] = []
    for g in batch:
        if not g.items:
            out.append(GroupResult(group_id=g.group_id, decisions=[]))
            continue
        out.append(_parse_group_decisions(g, by_id.get(g.group_id, []), kind=kind))
    return out


def match_grouped(
    groups: list[GroupInput],
    *,
    kind: str = "task",
    batch_size: int = 4,
    max_workers: int = 3,
) -> list[GroupResult]:
    """Run grouped matching across many groups, batched into LLM calls.

    Splits `groups` into chunks of `batch_size`, sends each chunk to one
    LLM call, runs up to `max_workers` calls concurrently. Output order
    matches input order.

    Empty groups (no items) are returned with empty decisions; no LLM
    call is made for a batch that contains only empty groups.
    """
    if not groups:
        return []

    batches = [
        groups[i : i + batch_size] for i in range(0, len(groups), batch_size)
    ]

    if max_workers <= 1 or len(batches) == 1:
        out: list[GroupResult] = []
        for batch in batches:
            out.extend(_run_one_grouped_batch(batch, kind=kind))
        return out

    results_by_index: dict[int, list[GroupResult]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(_run_one_grouped_batch, batch, kind=kind): i
            for i, batch in enumerate(batches)
        }
        for f in as_completed(futures):
            idx = futures[f]
            results_by_index[idx] = f.result()

    out = []
    for i in range(len(batches)):
        out.extend(results_by_index[i])
    return out


# ----------------------------------------------------------------------
# Top-level orchestrator: run_matcher
# ----------------------------------------------------------------------


@dataclass
class FileEpicResult:
    """One extracted epic (from one file or one section of a multi-epic
    file) and the matcher's decisions for it.
    """
    file_id: str
    file_name: str
    section_index: int
    extracted_epic_summary: str
    extracted_epic_description: str
    extracted_epic_assignee_raw: str | None
    # Per-task: parallel to `extracted_tasks` from the source extraction.
    # Each MatchDecision references a child of `matched_jira_key` in
    # project_tree (or None for tasks with no Jira counterpart).
    matched_jira_key: str | None
    epic_match_confidence: float
    epic_match_reason: str
    task_decisions: list[MatchDecision] = field(default_factory=list)
    # Existing-but-not-in-doc: Jira children of the matched epic that no
    # extracted task paired with.
    orphan_keys: list[str] = field(default_factory=list)


@dataclass
class MatcherResult:
    file_results: list[FileEpicResult]


def run_matcher(
    extractions: list[tuple[object, object]],
    project_tree: dict,
    *,
    batch_size: int = 4,
    max_workers: int = 3,
) -> MatcherResult:
    """Two-stage match across all files in one shot.

    `extractions` is a list of `(drive_file, extraction)` pairs, where
    `extraction` is either an `ExtractionResult` (single_epic) or a
    `MultiExtractionResult` (multi_epic) — both from
    `pipeline.extractor`. We don't import them here to keep this module
    free of cyclic imports; we duck-type on `.epic` / `.epics`.

    `project_tree` is the dict produced by `scripts/list_project_tree.py`
    (`{"epics": [{"key", "summary", "description", "children": [...]}, ...]}`).

    Returns one `FileEpicResult` per extracted epic. For multi-epic
    files, you get N results (one per sub-epic).
    """
    # Build per-file/section list of epic items.
    file_results: list[FileEpicResult] = []
    epic_match_inputs: list[MatchInput] = []
    extracted_tasks_per_result: list[list[object]] = []  # parallel to file_results

    for drive_file, ext in extractions:
        if hasattr(ext, "epics"):  # MultiExtractionResult
            for i, epic in enumerate(ext.epics):
                file_results.append(
                    FileEpicResult(
                        file_id=ext.file_id,
                        file_name=ext.file_name,
                        section_index=i,
                        extracted_epic_summary=epic.summary,
                        extracted_epic_description=epic.description,
                        extracted_epic_assignee_raw=epic.assignee_name,
                        matched_jira_key=None,
                        epic_match_confidence=0.0,
                        epic_match_reason="",
                    )
                )
                epic_match_inputs.append(
                    MatchInput(summary=epic.summary, description=epic.description)
                )
                extracted_tasks_per_result.append(list(epic.tasks))
        elif hasattr(ext, "epic"):  # ExtractionResult (single_epic)
            file_results.append(
                FileEpicResult(
                    file_id=ext.file_id,
                    file_name=ext.file_name,
                    section_index=0,
                    extracted_epic_summary=ext.epic.summary,
                    extracted_epic_description=ext.epic.description,
                    extracted_epic_assignee_raw=ext.epic.assignee_name,
                    matched_jira_key=None,
                    epic_match_confidence=0.0,
                    epic_match_reason="",
                )
            )
            epic_match_inputs.append(
                MatchInput(
                    summary=ext.epic.summary, description=ext.epic.description
                )
            )
            extracted_tasks_per_result.append(list(ext.tasks))

    if not file_results:
        return MatcherResult(file_results=[])

    # ---- STAGE 1: epic match (one flat call) ----
    project_epics_raw = project_tree.get("epics") or []
    epic_candidates = [
        MatchInput(
            key=e.get("key"),
            summary=(e.get("summary") or "").strip(),
            description=(e.get("description") or "")[:_DESCRIPTION_PREVIEW_CHARS],
            children=[
                {
                    "key": c.get("key"),
                    "summary": (c.get("summary") or "").strip(),
                    "status": c.get("status"),
                }
                for c in (e.get("children") or [])
                if c.get("key")
            ],
        )
        for e in project_epics_raw
        if e.get("key") and e.get("summary")
    ]

    epic_decisions = match(
        items=epic_match_inputs,
        candidates=epic_candidates,
        kind="epic",
    )

    for fr, dec in zip(file_results, epic_decisions):
        fr.matched_jira_key = dec.candidate_key
        fr.epic_match_confidence = dec.confidence
        fr.epic_match_reason = dec.reason

    # ---- STAGE 2: grouped task match for matched epics only ----
    children_by_key: dict[str, list[dict]] = {
        e.get("key"): (e.get("children") or [])
        for e in project_epics_raw
        if e.get("key")
    }

    matched_groups: list[GroupInput] = []
    matched_indexes: list[int] = []
    for idx, fr in enumerate(file_results):
        if fr.matched_jira_key is None:
            continue
        tasks = extracted_tasks_per_result[idx]
        if not tasks:
            continue
        children = children_by_key.get(fr.matched_jira_key, [])
        items = [
            MatchInput(summary=t.summary, description=t.description) for t in tasks
        ]
        candidates = [
            MatchInput(
                key=c.get("key"),
                summary=(c.get("summary") or "").strip(),
                description=(c.get("description") or "")[:_DESCRIPTION_PREVIEW_CHARS],
            )
            for c in children
            if c.get("key")
        ]
        # Unique group_id per (file, section, matched_key) — protects
        # against ever having two extracted epics map to the same Jira key
        # (Stage-1 prevents this, but the unique id is a cheap safety net).
        gid = f"{fr.file_id}#{fr.section_index}@{fr.matched_jira_key}"
        matched_groups.append(
            GroupInput(group_id=gid, items=items, candidates=candidates)
        )
        matched_indexes.append(idx)

    if matched_groups:
        group_results = match_grouped(
            matched_groups,
            kind="task",
            batch_size=batch_size,
            max_workers=max_workers,
        )
        results_by_id = {gr.group_id: gr for gr in group_results}
        for idx, gi in zip(matched_indexes, matched_groups):
            fr = file_results[idx]
            gr = results_by_id.get(gi.group_id)
            if gr is None:
                fr.task_decisions = [
                    MatchDecision(
                        item_index=i,
                        candidate_key=None,
                        confidence=0.0,
                        reason="no group result returned",
                    )
                    for i in range(len(gi.items))
                ]
                continue
            fr.task_decisions = gr.decisions
            used_keys = {d.candidate_key for d in gr.decisions if d.candidate_key}
            fr.orphan_keys = [
                c.key for c in gi.candidates if c.key and c.key not in used_keys
            ]

    return MatcherResult(file_results=file_results)
