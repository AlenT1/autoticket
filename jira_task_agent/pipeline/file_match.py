"""Matcher step with per-task caching.

Composes today's two-stage matcher with two cache layers:

  Tier 3 (file-level)  — file's content_sha + matcher prompt unchanged
                         since cached → reuse the entire FileEpicResult
                         set for the file. (Existing.)

  Per-task             — file's content changed but only some chunks did.
                         For tasks whose owning chunk's body_sha matches
                         the cached anchor entry, reuse cached
                         MatchDecision. Only tasks in changed chunks go
                         through Stage 2.

The helper handles both single_epic and multi_epic. For multi_epic, a
sub-epic's children-set is a subset of the matched parent epic's
children — so per-task cache reuse is safe within an epic.
"""
from __future__ import annotations

import logging
from typing import Callable

from ..cache import Cache
from .anchor_locator import locate_tasks
from .chunker import chunk_markdown
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
    match_grouped,
    run_matcher,
)


logger = logging.getLogger(__name__)


def match_with_cache(
    extractions: list[tuple[object, ExtractionResult | MultiExtractionResult]],
    project_tree: dict,
    cache: Cache,
    *,
    content_shas: dict[str, str],
    local_paths: dict[str, "Path"],  # noqa: F821
    use_cache: bool,
    matcher_batch_size: int,
    matcher_max_workers: int,
    on_cache_hits_match: Callable[[], None],
) -> MatcherResult:
    """Run the matcher across all extractions with maximum cache reuse.

    Returns one `FileEpicResult` per extracted (sub-)epic, in extraction
    order. On any cache trouble, falls through to a fresh full match.
    """
    topology_sha = compute_project_topology_sha(project_tree)
    prompt_sha = compute_matcher_prompt_sha()
    live_epic_keys = {
        e.get("key") for e in (project_tree.get("epics") or []) if e.get("key")
    }

    cached_results: list[FileEpicResult] = []
    fresh_extractions: list[tuple[object, object, str]] = []  # (df, ext, sha)

    for drive_file, ext in extractions:
        fid = ext.file_id
        csha = content_shas.get(fid, "")

        # Tier 3 — file-level cache hit (whole file unchanged AND prompts
        # unchanged AND no orphaned matched_jira_keys). Existing path.
        if use_cache and csha:
            tier3 = cache.get_match(fid, content_sha=csha, prompt_sha=prompt_sha)
            if tier3 is not None:
                stale = next(
                    (
                        r.get("matched_jira_key")
                        for r in tier3
                        if r.get("matched_jira_key")
                        and r["matched_jira_key"] not in live_epic_keys
                    ),
                    None,
                )
                if stale is not None:
                    logger.info(
                        "matcher cache STALE for %s (%s missing); re-matching",
                        ext.file_name, stale,
                    )
                    cache.drop_match(fid)
                else:
                    try:
                        for r in tier3:
                            cached_results.append(file_epic_result_from_json(r))
                        on_cache_hits_match()
                        logger.info(
                            "matcher cache HIT (file-level): %s — %d result(s)",
                            ext.file_name, len(tier3),
                        )
                        continue
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "matcher cache: failed to deserialize %s (%s); "
                            "re-matching",
                            fid, e,
                        )

        # Per-task cache — file content changed; only changed-chunk tasks
        # need fresh matcher decisions.
        if use_cache and csha:
            partial = _try_partial_match(
                drive_file=drive_file,
                ext=ext,
                content_sha=csha,
                local_path=local_paths.get(fid),
                cache=cache,
                project_tree=project_tree,
                prompt_sha=prompt_sha,
                topology_sha=topology_sha,
                batch_size=matcher_batch_size,
                max_workers=matcher_max_workers,
            )
            if partial is not None:
                cached_results.extend(partial)
                # Persist refreshed decisions for this file.
                _persist_match(
                    cache, fid, drive_file, csha, prompt_sha, topology_sha, partial
                )
                logger.info(
                    "matcher cache HIT (per-task): %s — %d sub-epic(s) merged",
                    ext.file_name, len(partial),
                )
                continue

        fresh_extractions.append((drive_file, ext, csha))

    # Files with no usable cache → run the full matcher.
    if fresh_extractions:
        fresh_results = run_matcher(
            [(df, ext) for df, ext, _ in fresh_extractions],
            project_tree,
            batch_size=matcher_batch_size,
            max_workers=matcher_max_workers,
        )
        # Group serialized results per file for cache write-back.
        per_file: dict[str, list[dict]] = {}
        for fr in fresh_results.file_results:
            per_file.setdefault(fr.file_id, []).append(file_epic_result_to_json(fr))
        if use_cache:
            for df, ext, csha in fresh_extractions:
                if csha:
                    cache.set_match(
                        file_id=ext.file_id,
                        modified_time=df.modified_time.isoformat(),
                        content_sha=csha,
                        prompt_sha=prompt_sha,
                        topology_sha=topology_sha,
                        results=per_file.get(ext.file_id, []),
                    )
        cached_results.extend(fresh_results.file_results)
    else:
        logger.info(
            "matcher: 0 LLM calls — all files served from cache"
        )

    return MatcherResult(file_results=cached_results)


def _try_partial_match(
    *,
    drive_file,
    ext: ExtractionResult | MultiExtractionResult,
    content_sha: str,
    local_path,
    cache: Cache,
    project_tree: dict,
    prompt_sha: str,
    topology_sha: str,
    batch_size: int,
    max_workers: int,
) -> list[FileEpicResult] | None:
    """If the file changed but most chunks didn't, run Stage 2 only on
    tasks in changed chunks. Returns merged `FileEpicResult`s on
    success; None if not applicable (caller falls back to full match).

    Requires:
      - Cached matcher_payload exists with results (we'll reuse epic
        match + most task decisions from there).
      - Cached chunks + task_anchors exist (so we know which tasks
        moved).
      - Local file readable to compute current chunk shas.
    """
    fid = ext.file_id
    entry = cache.files.get(fid)
    if entry is None or entry.matcher_payload is None:
        return None
    cached_results_raw = entry.matcher_payload.get("results") or []
    if not cached_results_raw:
        return None

    cached_chunks = cache.get_chunks(fid)
    cached_anchors = cache.get_task_anchors(fid)
    if not cached_chunks or not cached_anchors:
        return None
    if not local_path:
        return None

    file_text = local_path.read_text(encoding="utf-8", errors="replace")
    current_chunks = chunk_markdown(file_text)
    current_shas = {c.chunk_id: c.body_sha for c in current_chunks}
    changed_set = {
        cid for cid, sha in current_shas.items()
        if cached_chunks.get(cid) != sha
    }
    if not changed_set:
        # Defensive: Tier 3 should have caught this. Fall through.
        return None

    # Reconstitute cached FileEpicResults so we can mutate task_decisions.
    cached_frs = [
        file_epic_result_from_json(r) for r in cached_results_raw
    ]

    # For each FileEpicResult, identify which extracted-task indexes have
    # dirty chunks. Build per-epic group of dirty tasks for Stage 2.
    children_by_key: dict[str, list[dict]] = {
        e.get("key"): (e.get("children") or [])
        for e in (project_tree.get("epics") or [])
        if e.get("key")
    }

    is_multi = isinstance(ext, MultiExtractionResult)
    if is_multi:
        if len(cached_frs) != len(ext.epics):
            # Schema mismatch (e.g., epic added/removed in a non-cached
            # path) — fall back to full match.
            return None
        epics_in_order = ext.epics
    else:
        if len(cached_frs) != 1:
            return None
        epics_in_order = [None]  # placeholder for the single epic

    groups: list[GroupInput] = []
    group_meta: list[tuple[int, list[int]]] = []  # (fr_idx, dirty_task_indexes)

    for fr_idx, fr in enumerate(cached_frs):
        # Reconstruct the per-section task list parallel to fr.task_decisions.
        if is_multi:
            section_tasks: list[ExtractedTask] = list(epics_in_order[fr_idx].tasks)
        else:
            section_tasks = list(ext.tasks)

        # Identify dirty tasks: anchor's cached chunk is in changed_set,
        # OR anchor not in cache (newly added).
        dirty_idxs: list[int] = []
        for i, t in enumerate(section_tasks):
            anchor_info = cached_anchors.get(t.source_anchor)
            if anchor_info is None:
                # New task without a cached anchor → dirty.
                dirty_idxs.append(i)
                continue
            cached_chunk_id = anchor_info.get("chunk_id")
            if cached_chunk_id in changed_set:
                dirty_idxs.append(i)

        if not dirty_idxs:
            continue  # nothing to refresh in this epic group

        if fr.matched_jira_key is None:
            # No matched epic to feed Stage 2 against. The dirty tasks
            # remain `create_task` candidates with no candidate match.
            # We mark them as such by overwriting their decisions to
            # candidate_key=None. Continue without LLM call.
            for i in dirty_idxs:
                if i < len(fr.task_decisions):
                    fr.task_decisions[i] = MatchDecision(
                        item_index=i,
                        candidate_key=None,
                        confidence=0.0,
                        reason="no matched epic; new task in changed chunk",
                    )
            continue

        # Build a Stage 2 group containing ONLY the dirty tasks.
        items = [
            MatchInput(
                summary=section_tasks[i].summary,
                description=section_tasks[i].description,
            )
            for i in dirty_idxs
        ]
        children = children_by_key.get(fr.matched_jira_key, [])
        candidates = [
            MatchInput(
                key=c.get("key"),
                summary=(c.get("summary") or "").strip(),
                description=(c.get("description") or "")[:3000],
            )
            for c in children if c.get("key")
        ]
        gid = f"{fr.file_id}#{fr.section_index}@{fr.matched_jira_key}#partial"
        groups.append(GroupInput(group_id=gid, items=items, candidates=candidates))
        group_meta.append((fr_idx, dirty_idxs))

    if groups:
        results = match_grouped(
            groups, kind="task", batch_size=batch_size, max_workers=max_workers
        )
        # Splice each fresh decision back into its FileEpicResult at the
        # original index (item_index in the group is the position within
        # dirty_idxs, not within the section).
        results_by_id = {r.group_id: r for r in results}
        for (fr_idx, dirty_idxs), gi in zip(group_meta, groups):
            fr = cached_frs[fr_idx]
            gr = results_by_id.get(gi.group_id)
            if gr is None:
                continue
            for d in gr.decisions:
                section_idx = dirty_idxs[d.item_index]
                if section_idx < len(fr.task_decisions):
                    fr.task_decisions[section_idx] = MatchDecision(
                        item_index=section_idx,
                        candidate_key=d.candidate_key,
                        confidence=d.confidence,
                        reason=d.reason,
                    )

    return cached_frs


def _persist_match(
    cache: Cache,
    file_id: str,
    drive_file,
    content_sha: str,
    prompt_sha: str,
    topology_sha: str,
    file_results: list[FileEpicResult],
) -> None:
    serialized = [file_epic_result_to_json(fr) for fr in file_results]
    try:
        cache.set_match(
            file_id=file_id,
            modified_time=drive_file.modified_time.isoformat(),
            content_sha=content_sha,
            prompt_sha=prompt_sha,
            topology_sha=topology_sha,
            results=serialized,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("cache: failed to persist match for %s: %s", file_id, e)
