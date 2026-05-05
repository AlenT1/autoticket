"""LLM-based comparator: pairs extracted items with existing Jira issues.

Also exposes:
  - `compute_project_topology_sha(project_tree)`
  - `compute_matcher_prompt_sha()`
used by the runner cache (Tier 3) to validate that a cached matcher
decision is still applicable to the current Jira state and prompt set.


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

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field

from ..llm.client import chat, load_prompt, models_classify, render_prompt

_SYSTEM_PROMPT = load_prompt("match/epic")
_GROUPED_SYSTEM_PROMPT = load_prompt("match/issue")
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


def epic_candidates_from_tree(project_tree: dict) -> list["MatchInput"]:
    """`MatchInput`s for every epic in the project tree, with children
    summaries + statuses inlined for Stage 1 disambiguation."""
    return [
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
        for e in (project_tree.get("epics") or [])
        if e.get("key") and e.get("summary")
    ]


def task_candidates_from_children(children: list[dict]) -> list["MatchInput"]:
    """`MatchInput`s for an epic's children, used as Stage 2 candidates."""
    return [
        MatchInput(
            key=c.get("key"),
            summary=(c.get("summary") or "").strip(),
            description=(c.get("description") or "")[:_DESCRIPTION_PREVIEW_CHARS],
        )
        for c in children
        if c.get("key")
    ]


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
    """One extracted epic and the matcher's decisions for its tasks."""
    file_id: str
    file_name: str
    section_index: int
    extracted_epic_summary: str
    extracted_epic_description: str
    extracted_epic_assignee_raw: str | None
    matched_jira_key: str | None
    epic_match_confidence: float
    epic_match_reason: str
    # Per-task decisions. Each decision's child key references a child
    # of `matched_jira_key` in project_tree (or None for unmatched).
    task_decisions: list[MatchDecision] = field(default_factory=list)
    # Source anchors of the extracted tasks, parallel to `task_decisions`.
    # Lets the partial-match path splice cached decisions back to fresh
    # tasks by anchor without needing the original extraction.
    task_anchors: list[str | None] = field(default_factory=list)
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

    def _add_section(
        ext_obj, section_idx: int,
        epic_summary: str, epic_description: str, epic_assignee: str | None,
        tasks: list,
    ) -> None:
        file_results.append(FileEpicResult(
            file_id=ext_obj.file_id,
            file_name=ext_obj.file_name,
            section_index=section_idx,
            extracted_epic_summary=epic_summary,
            extracted_epic_description=epic_description,
            extracted_epic_assignee_raw=epic_assignee,
            matched_jira_key=None,
            epic_match_confidence=0.0,
            epic_match_reason="",
            task_anchors=[t.source_anchor or None for t in tasks],
        ))
        epic_match_inputs.append(
            MatchInput(summary=epic_summary, description=epic_description)
        )
        extracted_tasks_per_result.append(list(tasks))

    for _, ext in extractions:
        if hasattr(ext, "epics"):
            for i, epic in enumerate(ext.epics):
                _add_section(
                    ext, i, epic.summary, epic.description,
                    epic.assignee_name, epic.tasks,
                )
        elif hasattr(ext, "epic"):
            _add_section(
                ext, 0, ext.epic.summary, ext.epic.description,
                ext.epic.assignee_name, ext.tasks,
            )

    if not file_results:
        return MatcherResult(file_results=[])

    project_epics_raw = project_tree.get("epics") or []
    epic_decisions = match(
        items=epic_match_inputs,
        candidates=epic_candidates_from_tree(project_tree),
        kind="epic",
    )

    for fr, dec in zip(file_results, epic_decisions):
        fr.matched_jira_key = dec.candidate_key
        fr.epic_match_confidence = dec.confidence
        fr.epic_match_reason = dec.reason

    _run_stage2(
        file_results, extracted_tasks_per_result, project_epics_raw,
        batch_size=batch_size, max_workers=max_workers,
    )
    return MatcherResult(file_results=file_results)


def _run_stage2(
    file_results: list[FileEpicResult],
    extracted_tasks_per_result: list[list[object]],
    project_epics_raw: list[dict],
    *,
    batch_size: int,
    max_workers: int,
) -> None:
    """Populate `file_results[*].task_decisions` (and orphan_keys) via
    Stage 2. Sections whose Stage 1 returned no Jira pairing get default
    no-match decisions so the reconciler can still emit `create_task`."""
    children_by_key = {
        e.get("key"): (e.get("children") or [])
        for e in project_epics_raw
        if e.get("key")
    }

    matched_groups: list[GroupInput] = []
    matched_indexes: list[int] = []
    for idx, fr in enumerate(file_results):
        tasks = extracted_tasks_per_result[idx]
        if fr.matched_jira_key is None:
            fr.task_decisions = [
                MatchDecision(
                    item_index=j, candidate_key=None, confidence=0.0,
                    reason="no matched epic; will be created with new epic",
                )
                for j in range(len(tasks))
            ]
            continue
        if not tasks:
            continue
        gid = f"{fr.file_id}#{fr.section_index}@{fr.matched_jira_key}"
        matched_groups.append(GroupInput(
            group_id=gid,
            items=[
                MatchInput(summary=t.summary, description=t.description)
                for t in tasks
            ],
            candidates=task_candidates_from_children(
                children_by_key.get(fr.matched_jira_key, [])
            ),
        ))
        matched_indexes.append(idx)

    if not matched_groups:
        return

    results_by_id = {
        gr.group_id: gr for gr in match_grouped(
            matched_groups, kind="task",
            batch_size=batch_size, max_workers=max_workers,
        )
    }
    for idx, gi in zip(matched_indexes, matched_groups):
        fr = file_results[idx]
        gr = results_by_id.get(gi.group_id)
        if gr is None:
            fr.task_decisions = [
                MatchDecision(
                    item_index=i, candidate_key=None, confidence=0.0,
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


# ----------------------------------------------------------------------
# Tier 3 cache helpers — topology + prompt fingerprints, FileEpicResult
# round-trip to/from JSON.
# ----------------------------------------------------------------------


def compute_project_topology_sha(project_tree: dict) -> str:
    """Deterministic fingerprint of the Jira side of the matcher's input.

    Covers exactly what Stage 1 + Stage 2 see:
      - per epic: key, summary, description (truncated to preview window)
      - per child: key, summary, description (truncated), status

    Sorted by key so order doesn't matter. If the hash matches between
    runs, the matcher's Jira-side input was byte-identical and a cached
    decision keyed by this hash is reusable.

    Description content is included so a description rewrite invalidates
    the cache — descriptions can flip a borderline match.
    """
    parts: list = []
    for e in sorted(project_tree.get("epics") or [], key=lambda x: x.get("key") or ""):
        ek = e.get("key") or ""
        es = (e.get("summary") or "").strip()
        ed = (e.get("description") or "")[:_DESCRIPTION_PREVIEW_CHARS]
        children: list = []
        for c in sorted(e.get("children") or [], key=lambda x: x.get("key") or ""):
            children.append(
                (
                    c.get("key") or "",
                    (c.get("summary") or "").strip(),
                    c.get("status") or "",
                    (c.get("description") or "")[:_DESCRIPTION_PREVIEW_CHARS],
                )
            )
        parts.append((ek, es, ed, children))
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_matcher_prompt_sha() -> str:
    """Fingerprint of everything the matcher's behavior depends on:
    the two prompt templates and the model id used for matcher calls.

    Bumping the prompt or the model invalidates every cached decision,
    so we never silently reuse decisions made by a different brain.
    """
    parts = [
        _SYSTEM_PROMPT,
        _GROUPED_SYSTEM_PROMPT,
        os.environ.get("LLM_MODEL_CLASSIFY", ""),
        f"min_conf:{_MIN_CONFIDENCE_BY_KIND}",
    ]
    payload = "\n---\n".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_epic_result_to_json(fr: FileEpicResult) -> dict:
    return {
        "file_id": fr.file_id,
        "file_name": fr.file_name,
        "section_index": fr.section_index,
        "extracted_epic_summary": fr.extracted_epic_summary,
        "extracted_epic_description": fr.extracted_epic_description,
        "extracted_epic_assignee_raw": fr.extracted_epic_assignee_raw,
        "matched_jira_key": fr.matched_jira_key,
        "epic_match_confidence": fr.epic_match_confidence,
        "epic_match_reason": fr.epic_match_reason,
        "task_decisions": [asdict(d) for d in fr.task_decisions],
        "task_anchors": list(fr.task_anchors),
        "orphan_keys": list(fr.orphan_keys),
    }


def file_epic_result_from_json(data: dict) -> FileEpicResult:
    return FileEpicResult(
        file_id=data["file_id"],
        file_name=data["file_name"],
        section_index=int(data.get("section_index", 0)),
        extracted_epic_summary=data.get("extracted_epic_summary", ""),
        extracted_epic_description=data.get("extracted_epic_description", ""),
        extracted_epic_assignee_raw=data.get("extracted_epic_assignee_raw"),
        matched_jira_key=data.get("matched_jira_key"),
        epic_match_confidence=float(data.get("epic_match_confidence") or 0.0),
        epic_match_reason=data.get("epic_match_reason") or "",
        task_decisions=[
            MatchDecision(
                item_index=int(d.get("item_index", -1)),
                candidate_key=d.get("candidate_key"),
                confidence=float(d.get("confidence") or 0.0),
                reason=str(d.get("reason") or ""),
            )
            for d in data.get("task_decisions") or []
        ],
        task_anchors=list(data.get("task_anchors") or []),
        orphan_keys=list(data.get("orphan_keys") or []),
    )
