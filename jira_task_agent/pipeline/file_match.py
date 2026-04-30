"""Per-file matcher with caching.

Two branches behind one entrypoint:

  - cache hit: file content + matcher prompt + project topology unchanged
    AND every cached `matched_jira_key` still exists in the project tree.
    Reuse the cached `FileEpicResult`s verbatim.
  - miss: run the full two-stage matcher (`run_matcher`) and persist.
"""
from __future__ import annotations

import logging
from typing import Callable

from ..cache import Cache
from .extractor import ExtractionResult, MultiExtractionResult
from .matcher import (
    FileEpicResult,
    MatcherResult,
    compute_matcher_prompt_sha,
    compute_project_topology_sha,
    file_epic_result_from_json,
    file_epic_result_to_json,
    run_matcher,
)


logger = logging.getLogger(__name__)

Extraction = ExtractionResult | MultiExtractionResult


def match_with_cache(
    extractions: list[tuple[object, Extraction]],
    project_tree: dict,
    cache: Cache,
    *,
    content_shas: dict[str, str],
    use_cache: bool,
    matcher_batch_size: int,
    matcher_max_workers: int,
    on_cache_hits_match: Callable[[], None],
) -> MatcherResult:
    prompt_sha = compute_matcher_prompt_sha()
    topology_sha = compute_project_topology_sha(project_tree)
    live_epic_keys = {
        e.get("key") for e in (project_tree.get("epics") or []) if e.get("key")
    }

    cached_results, fresh = _split_by_cache(
        extractions, cache,
        content_shas=content_shas, use_cache=use_cache,
        prompt_sha=prompt_sha, live_epic_keys=live_epic_keys,
        on_hit=on_cache_hits_match,
    )

    fresh_results = _run_fresh(
        fresh, project_tree, cache,
        prompt_sha=prompt_sha, topology_sha=topology_sha,
        use_cache=use_cache,
        batch_size=matcher_batch_size, max_workers=matcher_max_workers,
    )
    return MatcherResult(file_results=cached_results + fresh_results)


def _split_by_cache(
    extractions: list[tuple[object, Extraction]],
    cache: Cache,
    *,
    content_shas: dict[str, str],
    use_cache: bool,
    prompt_sha: str,
    live_epic_keys: set,
    on_hit: Callable[[], None],
) -> tuple[list[FileEpicResult], list[tuple[object, Extraction, str]]]:
    cached: list[FileEpicResult] = []
    fresh: list[tuple[object, Extraction, str]] = []
    for drive_file, ext in extractions:
        csha = content_shas.get(ext.file_id, "")
        hit = (
            _try_cache_hit(cache, ext.file_id, csha, prompt_sha,
                           live_epic_keys, ext.file_name)
            if use_cache and csha else None
        )
        if hit is None:
            fresh.append((drive_file, ext, csha))
        else:
            cached.extend(hit)
            on_hit()
    return cached, fresh


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
    if _has_stale_epic_key(raw, live_epic_keys, file_name, cache, file_id):
        return None
    try:
        return [file_epic_result_from_json(r) for r in raw]
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "matcher cache: failed to deserialize %s (%s); re-matching",
            file_id, e,
        )
        return None


def _has_stale_epic_key(
    raw: list[dict],
    live_epic_keys: set,
    file_name: str,
    cache: Cache,
    file_id: str,
) -> bool:
    stale = next(
        (
            r.get("matched_jira_key")
            for r in raw
            if r.get("matched_jira_key")
            and r["matched_jira_key"] not in live_epic_keys
        ),
        None,
    )
    if stale is None:
        return False
    logger.info(
        "matcher cache STALE for %s (%s missing); re-matching",
        file_name, stale,
    )
    cache.drop_match(file_id)
    return True


def _run_fresh(
    fresh: list[tuple[object, Extraction, str]],
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
        per_file: dict[str, list[dict]] = {}
        for fr in result.file_results:
            per_file.setdefault(fr.file_id, []).append(file_epic_result_to_json(fr))
        for df, ext, csha in fresh:
            if csha:
                cache.set_match(
                    file_id=ext.file_id,
                    modified_time=df.modified_time.isoformat(),
                    content_sha=csha,
                    prompt_sha=prompt_sha,
                    topology_sha=topology_sha,
                    results=per_file.get(ext.file_id, []),
                )
    return list(result.file_results)
