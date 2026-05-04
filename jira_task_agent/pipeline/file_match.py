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
    epic_candidates_from_tree,
    file_epic_result_from_json,
    file_epic_result_to_json,
    match,
    match_grouped,
    run_matcher,
    task_candidates_from_children,
)


logger = logging.getLogger(__name__)

Extraction = ExtractionResult | MultiExtractionResult
EPIC_DIRTY_PREFIX = "<epic>:"


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
    processed = _processed_section_indexes(
        sections, cached_frs, dirty_epic_idxs, dirty_task_anchors,
    )
    if not processed:
        return None

    s1_decisions = _stage1_match(sections, processed, project_tree)
    candidates_by_key = {
        e.get("key"): task_candidates_from_children(e.get("children") or [])
        for e in (project_tree.get("epics") or []) if e.get("key")
    }

    out_frs: list[FileEpicResult] = []
    for i, section in enumerate(sections):
        if i in processed:
            cached = cached_frs[i] if i < len(cached_frs) else None
            out_frs.append(_build_processed_section(
                ext, i, section,
                s1_decision=s1_decisions[i], cached_fr=cached,
                dirty_task_anchors=dirty_task_anchors,
                candidates_by_key=candidates_by_key,
                batch_size=batch_size, max_workers=max_workers,
            ))
        else:
            out_frs.append(cached_frs[i])

    _persist_match(
        cache, drive_file, ext.file_id,
        content_sha=content_sha, prompt_sha=prompt_sha,
        topology_sha=topology_sha, results=out_frs,
    )
    logger.info(
        "matcher partial: %s — %d processed section(s)",
        ext.file_name, len(processed),
    )
    return out_frs


def _processed_section_indexes(
    sections: list[_Section],
    cached_frs: list[FileEpicResult],
    dirty_epic_idxs: set[int],
    dirty_task_anchors: set[str],
) -> set[int]:
    """A section is processed if its epic or any of its tasks is in
    dirty, or it is brand new (beyond the cached state)."""
    out: set[int] = set()
    for i, section in enumerate(sections):
        if i >= len(cached_frs) or i in dirty_epic_idxs:
            out.add(i)
            continue
        if any(
            t.source_anchor and t.source_anchor in dirty_task_anchors
            for t in section.tasks
        ):
            out.add(i)
    return out


def _stage1_match(
    sections: list[_Section],
    processed: set[int],
    project_tree: dict,
) -> dict[int, MatchDecision]:
    """Re-pair every processed section's epic against the full project
    tree. Brand-new sections, dirty-epic sections, and sections with
    only dirty tasks all flow through here so the matcher always sees
    the section's epic context plus the live tree."""
    indexes = sorted(processed)
    items = [
        MatchInput(summary=sections[i].summary, description=sections[i].description)
        for i in indexes
    ]
    decisions = match(
        items=items, candidates=epic_candidates_from_tree(project_tree), kind="epic",
    )
    return dict(zip(indexes, decisions))


def _build_processed_section(
    ext: Extraction,
    idx: int,
    section: _Section,
    *,
    s1_decision: MatchDecision,
    cached_fr: FileEpicResult | None,
    dirty_task_anchors: set[str],
    candidates_by_key: dict[str, list[MatchInput]],
    batch_size: int,
    max_workers: int,
) -> FileEpicResult:
    matched_key = s1_decision.candidate_key
    task_anchors = [t.source_anchor or None for t in section.tasks]

    if matched_key is None:
        return _file_epic_result(
            ext, idx, section, s1_decision,
            task_decisions=[
                MatchDecision(j, None, 0.0, "no matched epic")
                for j in range(len(section.tasks))
            ],
            task_anchors=task_anchors,
            orphan_keys=[],
        )

    candidates = candidates_by_key.get(matched_key, [])
    epic_unchanged = (
        cached_fr is not None and cached_fr.matched_jira_key == matched_key
    )
    if epic_unchanged:
        task_decisions = _stage2_partial(
            tasks=section.tasks, cached_fr=cached_fr,
            candidates=candidates, dirty_task_anchors=dirty_task_anchors,
            file_id=ext.file_id, idx=idx, matched_key=matched_key,
            batch_size=batch_size, max_workers=max_workers,
        )
    else:
        task_decisions = _stage2_full(
            section.tasks, candidates, ext.file_id, idx, matched_key,
            batch_size=batch_size, max_workers=max_workers,
        )
    used = {d.candidate_key for d in task_decisions if d.candidate_key}
    return _file_epic_result(
        ext, idx, section, s1_decision,
        task_decisions=task_decisions,
        task_anchors=task_anchors,
        orphan_keys=[c.key for c in candidates if c.key not in used],
    )


def _file_epic_result(
    ext: Extraction,
    idx: int,
    section: _Section,
    s1_decision: MatchDecision,
    *,
    task_decisions: list[MatchDecision],
    task_anchors: list[str | None],
    orphan_keys: list[str],
) -> FileEpicResult:
    return FileEpicResult(
        file_id=ext.file_id,
        file_name=ext.file_name,
        section_index=idx,
        extracted_epic_summary=section.summary,
        extracted_epic_description=section.description,
        extracted_epic_assignee_raw=section.assignee_name,
        matched_jira_key=s1_decision.candidate_key,
        epic_match_confidence=s1_decision.confidence,
        epic_match_reason=s1_decision.reason,
        task_decisions=task_decisions,
        task_anchors=task_anchors,
        orphan_keys=orphan_keys,
    )


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
