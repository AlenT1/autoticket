"""Per-file matcher with caching.

Three branches behind one entrypoint:

  - hit     : file content + matcher prompt unchanged AND every cached
              `matched_jira_key` still exists in Jira → reuse cached
              `FileEpicResult`s verbatim.
  - partial : file changed but cached match results exist and the caller
              supplied `dirty_anchors`. Refresh Stage 1 only for dirty
              epics and Stage 2 only for dirty tasks; reuse cached
              decisions for everything else.
  - fresh   : no usable cache → run the full two-stage matcher.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from ..cache import Cache
from ..drive.client import DriveFile
from .extractor import (
    ExtractedTask,
    ExtractionResult,
    MultiExtractionResult,
)
from .matcher import (
    FileEpicResult,
    GroupInput,
    MatchDecision,
    MatchInput,
    MatcherResult,
    compute_matcher_prompt_sha,
    compute_project_topology_sha,
    file_epic_result_from_json,
    file_epic_result_to_json,
    match,
    match_grouped,
    run_matcher,
)


logger = logging.getLogger(__name__)

Extraction = ExtractionResult | MultiExtractionResult
EPIC_DIRTY_PREFIX = "<epic>:"
_DESCRIPTION_PREVIEW_CHARS = 3000


@dataclass(frozen=True)
class _Section:
    """One sub-epic's view: headline fields + its task list."""
    summary: str
    description: str
    assignee_name: str | None
    tasks: list[ExtractedTask]


def match_with_cache(
    extractions: list[tuple[DriveFile, Extraction]],
    project_tree: dict,
    cache: Cache,
    *,
    content_shas: dict[str, str],
    use_cache: bool,
    matcher_batch_size: int,
    matcher_max_workers: int,
    on_cache_hits_match: Callable[[], None],
    dirty_anchors_per_file: dict[str, set[str] | None] | None = None,
) -> MatcherResult:
    prompt_sha = compute_matcher_prompt_sha()
    topology_sha = compute_project_topology_sha(project_tree)
    live_epic_keys = {
        e.get("key") for e in (project_tree.get("epics") or []) if e.get("key")
    }
    dirty_per_file = dirty_anchors_per_file or {}

    results: list[FileEpicResult] = []
    fresh: list[tuple[DriveFile, Extraction, str]] = []

    for drive_file, ext in extractions:
        csha = content_shas.get(ext.file_id, "")
        if not (use_cache and csha):
            fresh.append((drive_file, ext, csha))
            continue

        hit = _try_cache_hit(
            cache, ext.file_id, csha, prompt_sha, live_epic_keys, ext.file_name,
        )
        if hit is not None:
            results.extend(hit)
            on_cache_hits_match()
            continue

        partial = _try_partial(
            cache, drive_file, ext,
            content_sha=csha,
            prompt_sha=prompt_sha,
            topology_sha=topology_sha,
            project_tree=project_tree,
            live_epic_keys=live_epic_keys,
            dirty=dirty_per_file.get(ext.file_id),
            batch_size=matcher_batch_size,
            max_workers=matcher_max_workers,
        )
        if partial is not None:
            results.extend(partial)
            on_cache_hits_match()
            continue

        fresh.append((drive_file, ext, csha))

    results.extend(_run_fresh(
        fresh, project_tree, cache,
        prompt_sha=prompt_sha, topology_sha=topology_sha,
        use_cache=use_cache,
        batch_size=matcher_batch_size, max_workers=matcher_max_workers,
    ))
    return MatcherResult(file_results=results)


def _try_cache_hit(
    cache: Cache,
    file_id: str,
    content_sha: str,
    prompt_sha: str,
    live_epic_keys: set,
    file_name: str,
) -> list[FileEpicResult] | None:
    raw = cache.get_match(file_id, content_sha=content_sha, prompt_sha=prompt_sha)
    if raw is None:
        return None
    stale = _stale_matched_key(raw, live_epic_keys)
    if stale is not None:
        logger.info(
            "matcher cache STALE for %s (%s missing); re-matching",
            file_name, stale,
        )
        cache.drop_match(file_id)
        return None
    try:
        return [file_epic_result_from_json(r) for r in raw]
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "matcher cache: failed to deserialize %s (%s); re-matching",
            file_id, e,
        )
        return None


def _try_partial(
    cache: Cache,
    drive_file: DriveFile,
    ext: Extraction,
    *,
    content_sha: str,
    prompt_sha: str,
    topology_sha: str,
    project_tree: dict,
    live_epic_keys: set,
    dirty: set[str] | None,
    batch_size: int,
    max_workers: int,
) -> list[FileEpicResult] | None:
    if not dirty:
        return None
    cached_frs = _load_cached_frs(cache, drive_file.id, live_epic_keys)
    if cached_frs is None:
        return None

    dirty_epic_idxs, dirty_task_anchors = _split_dirty(dirty)
    sections = _sections_of(ext)
    s1_decisions = _refresh_dirty_epics(
        sections, cached_frs, dirty_epic_idxs, project_tree,
    )
    candidates_by_key = {
        e.get("key"): _task_candidates(e.get("children") or [])
        for e in (project_tree.get("epics") or []) if e.get("key")
    }

    out_frs = [
        _build_section_result(
            ext=ext, idx=i, section=section,
            cached_frs=cached_frs,
            s1_decisions=s1_decisions,
            dirty_task_anchors=dirty_task_anchors,
            candidates_by_key=candidates_by_key,
            batch_size=batch_size, max_workers=max_workers,
        )
        for i, section in enumerate(sections)
    ]
    _persist_match(
        cache, drive_file, ext.file_id,
        content_sha=content_sha, prompt_sha=prompt_sha,
        topology_sha=topology_sha, results=out_frs,
    )
    logger.info(
        "matcher partial: %s — %d dirty epic(s), %d dirty task(s)",
        ext.file_name, len(dirty_epic_idxs), len(dirty_task_anchors),
    )
    return out_frs


def _run_fresh(
    fresh: list[tuple[DriveFile, Extraction, str]],
    project_tree: dict,
    cache: Cache,
    *,
    prompt_sha: str,
    topology_sha: str,
    use_cache: bool,
    batch_size: int,
    max_workers: int,
) -> list[FileEpicResult]:
    if not fresh:
        return []
    result = run_matcher(
        [(df, ext) for df, ext, _ in fresh],
        project_tree,
        batch_size=batch_size, max_workers=max_workers,
    )
    if use_cache:
        per_file: dict[str, list[FileEpicResult]] = {}
        for fr in result.file_results:
            per_file.setdefault(fr.file_id, []).append(fr)
        for df, ext, csha in fresh:
            if csha:
                _persist_match(
                    cache, df, ext.file_id,
                    content_sha=csha, prompt_sha=prompt_sha,
                    topology_sha=topology_sha,
                    results=per_file.get(ext.file_id, []),
                )
    return list(result.file_results)


def _load_cached_frs(
    cache: Cache, file_id: str, live_epic_keys: set,
) -> list[FileEpicResult] | None:
    entry = cache.files.get(file_id)
    if entry is None or entry.matcher_payload is None:
        return None
    raw = entry.matcher_payload.get("results") or []
    if not raw:
        return None
    try:
        cached_frs = [file_epic_result_from_json(r) for r in raw]
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "matcher cache: failed to deserialize %s (%s); re-matching",
            file_id, e,
        )
        return None
    if _stale_matched_key(raw, live_epic_keys) is not None:
        cache.drop_match(file_id)
        return None
    return cached_frs


def _stale_matched_key(raw: list[dict], live_epic_keys: set) -> str | None:
    return next(
        (
            r.get("matched_jira_key")
            for r in raw
            if r.get("matched_jira_key")
            and r["matched_jira_key"] not in live_epic_keys
        ),
        None,
    )


def _split_dirty(dirty: set[str]) -> tuple[set[int], set[str]]:
    epic_idxs = {
        int(d[len(EPIC_DIRTY_PREFIX):]) for d in dirty
        if d.startswith(EPIC_DIRTY_PREFIX)
    }
    task_anchors = {d for d in dirty if not d.startswith(EPIC_DIRTY_PREFIX)}
    return epic_idxs, task_anchors


def _sections_of(ext: Extraction) -> list[_Section]:
    if isinstance(ext, MultiExtractionResult):
        return [
            _Section(e.summary, e.description, e.assignee_name, list(e.tasks))
            for e in ext.epics
        ]
    return [_Section(
        ext.epic.summary, ext.epic.description,
        ext.epic.assignee_name, list(ext.tasks),
    )]


def _epic_candidates(project_tree: dict) -> list[MatchInput]:
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


def _task_candidates(children: list[dict]) -> list[MatchInput]:
    return [
        MatchInput(
            key=c.get("key"),
            summary=(c.get("summary") or "").strip(),
            description=(c.get("description") or "")[:_DESCRIPTION_PREVIEW_CHARS],
        )
        for c in children
        if c.get("key")
    ]


def _refresh_dirty_epics(
    sections: list[_Section],
    cached_frs: list[FileEpicResult],
    dirty_epic_idxs: set[int],
    project_tree: dict,
) -> dict[int, MatchDecision]:
    indexes = [
        i for i in range(len(sections))
        if i in dirty_epic_idxs or i >= len(cached_frs)
    ]
    if not indexes:
        return {}
    items = [
        MatchInput(summary=sections[i].summary, description=sections[i].description)
        for i in indexes
    ]
    decisions = match(items=items, candidates=_epic_candidates(project_tree), kind="epic")
    return dict(zip(indexes, decisions))


def _build_section_result(
    *,
    ext: Extraction,
    idx: int,
    section: _Section,
    cached_frs: list[FileEpicResult],
    s1_decisions: dict[int, MatchDecision],
    dirty_task_anchors: set[str],
    candidates_by_key: dict[str, list[MatchInput]],
    batch_size: int,
    max_workers: int,
) -> FileEpicResult:
    fresh_decision = s1_decisions.get(idx)
    if fresh_decision is not None:
        matched_key = fresh_decision.candidate_key
        epic_conf = fresh_decision.confidence
        epic_reason = fresh_decision.reason
    else:
        cached = cached_frs[idx]
        matched_key = cached.matched_jira_key
        epic_conf = cached.epic_match_confidence
        epic_reason = cached.epic_match_reason

    task_anchors = [t.source_anchor or None for t in section.tasks]
    if matched_key is None:
        task_decisions = [
            MatchDecision(
                item_index=j, candidate_key=None,
                confidence=0.0, reason="no matched epic",
            )
            for j in range(len(section.tasks))
        ]
        orphan_keys: list[str] = []
    else:
        candidates = candidates_by_key.get(matched_key, [])
        epic_freshly_matched = idx in s1_decisions or idx >= len(cached_frs)
        if epic_freshly_matched:
            task_decisions = _stage2_full(
                section.tasks, candidates, ext.file_id, idx, matched_key,
                batch_size=batch_size, max_workers=max_workers,
            )
        else:
            task_decisions = _stage2_partial(
                tasks=section.tasks,
                cached_fr=cached_frs[idx],
                candidates=candidates,
                dirty_task_anchors=dirty_task_anchors,
                file_id=ext.file_id, idx=idx, matched_key=matched_key,
                batch_size=batch_size, max_workers=max_workers,
            )
        used = {d.candidate_key for d in task_decisions if d.candidate_key}
        orphan_keys = [c.key for c in candidates if c.key not in used]

    return FileEpicResult(
        file_id=ext.file_id, file_name=ext.file_name, section_index=idx,
        extracted_epic_summary=section.summary,
        extracted_epic_description=section.description,
        extracted_epic_assignee_raw=section.assignee_name,
        matched_jira_key=matched_key,
        epic_match_confidence=epic_conf,
        epic_match_reason=epic_reason,
        task_decisions=task_decisions,
        task_anchors=task_anchors,
        orphan_keys=orphan_keys,
    )


def _stage2_full(
    tasks: list[ExtractedTask],
    candidates: list[MatchInput],
    file_id: str,
    idx: int,
    matched_key: str,
    *,
    batch_size: int,
    max_workers: int,
) -> list[MatchDecision]:
    if not tasks:
        return []
    items = [MatchInput(summary=t.summary, description=t.description) for t in tasks]
    gid = f"{file_id}#{idx}@{matched_key}"
    grs = match_grouped(
        [GroupInput(group_id=gid, items=items, candidates=candidates)],
        kind="task", batch_size=batch_size, max_workers=max_workers,
    )
    return [
        MatchDecision(
            item_index=d.item_index, candidate_key=d.candidate_key,
            confidence=d.confidence, reason=d.reason,
        )
        for d in (grs[0].decisions if grs else [])
    ]


def _stage2_partial(
    *,
    tasks: list[ExtractedTask],
    cached_fr: FileEpicResult,
    candidates: list[MatchInput],
    dirty_task_anchors: set[str],
    file_id: str,
    idx: int,
    matched_key: str,
    batch_size: int,
    max_workers: int,
) -> list[MatchDecision]:
    by_anchor = {
        a: d
        for a, d in zip(cached_fr.task_anchors, cached_fr.task_decisions)
        if a
    }
    dirty_idxs = [
        j for j, t in enumerate(tasks)
        if t.source_anchor and t.source_anchor in dirty_task_anchors
    ]
    refreshed: dict[int, MatchDecision] = {}
    if dirty_idxs:
        items = [
            MatchInput(summary=tasks[j].summary, description=tasks[j].description)
            for j in dirty_idxs
        ]
        gid = f"{file_id}#{idx}@{matched_key}#partial"
        grs = match_grouped(
            [GroupInput(group_id=gid, items=items, candidates=candidates)],
            kind="task", batch_size=batch_size, max_workers=max_workers,
        )
        for d in (grs[0].decisions if grs else []):
            refreshed[dirty_idxs[d.item_index]] = d

    decisions: list[MatchDecision] = []
    for j, t in enumerate(tasks):
        d = refreshed.get(j) or (by_anchor.get(t.source_anchor) if t.source_anchor else None)
        if d is None:
            decisions.append(MatchDecision(
                item_index=j, candidate_key=None,
                confidence=0.0, reason="task without cached or fresh decision",
            ))
            continue
        decisions.append(MatchDecision(
            item_index=j, candidate_key=d.candidate_key,
            confidence=d.confidence, reason=d.reason,
        ))
    return decisions


def _persist_match(
    cache: Cache,
    drive_file: DriveFile,
    file_id: str,
    *,
    content_sha: str,
    prompt_sha: str,
    topology_sha: str,
    results: list[FileEpicResult],
) -> None:
    cache.set_match(
        file_id=file_id,
        modified_time=drive_file.modified_time.isoformat(),
        content_sha=content_sha,
        prompt_sha=prompt_sha,
        topology_sha=topology_sha,
        results=[file_epic_result_to_json(fr) for fr in results],
    )
