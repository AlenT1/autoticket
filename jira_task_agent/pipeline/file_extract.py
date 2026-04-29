"""One-file extraction step with full caching (Tier 2 + diff-aware).

Encapsulates the decision tree the runner used to inline:

  1. Tier 2 hit (content_sha unchanged)               → reuse cached extraction
  2. Diff-aware path (cache exists but content changed) → re-extract only
                                                          changed chunks
  3. Cold path (no usable cache)                       → full re-extract

On success, updates cache (extraction payload + per-chunk diff payload)
and returns the extraction. On unrecoverable failure, returns None and
appends an error to the report.

Side effects on the cache + report are intentional — the runner just
calls this helper per file and counts on the cache being up-to-date.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from ..drive.client import DriveFile
from ..cache import (
    Cache,
    deserialize_extraction,
    serialize_extraction,
)
from .anchor_locator import locate_tasks
from .chunker import chunk_markdown
from .classifier import ClassifyResult
from .extractor import (
    DiffExtractionResult,
    ExtractedTask,
    ExtractionError,
    ExtractionResult,
    MultiExtractionResult,
    extract_diff_aware,
    extract_from_file,
    extract_multi_from_file,
)


logger = logging.getLogger(__name__)


# Type alias for the report counter callbacks the runner passes in.
# Keeps this module decoupled from the runner's RunReport dataclass.
ReportHook = Callable[[str], None]


def _all_tasks(
    ext: ExtractionResult | MultiExtractionResult,
) -> list[ExtractedTask]:
    if isinstance(ext, MultiExtractionResult):
        out: list[ExtractedTask] = []
        for e in ext.epics:
            out.extend(e.tasks)
        return out
    return list(ext.tasks)


def _compute_diff_payload(
    ext: ExtractionResult | MultiExtractionResult, file_text: str
) -> dict:
    """After a fresh extraction, compute per-chunk shas and per-task
    chunk ownership for storage in cache.diff_payload."""
    chunks = chunk_markdown(file_text)
    chunk_shas = {c.chunk_id: c.body_sha for c in chunks}
    task_anchors: dict[str, dict] = {}
    tasks = _all_tasks(ext)
    locations = locate_tasks(tasks, chunks)
    for t, loc in zip(tasks, locations):
        if not t.source_anchor or loc.chunk_id is None:
            continue
        task_anchors[t.source_anchor] = {
            "chunk_id": loc.chunk_id,
            "body_sha": loc.body_sha,
        }
    return {"chunks": chunk_shas, "task_anchors": task_anchors}


def _changed_chunk_ids(
    cached_chunks: dict[str, str], current_chunks: dict[str, str]
) -> list[str]:
    """Return the chunk_ids whose bodies differ from cached, plus
    chunk_ids that are NEW in the current file. Cached chunks missing
    from current are not returned (deletion is detected by the runner
    via task anchors that no longer locate)."""
    changed: list[str] = []
    for cid, cur_sha in current_chunks.items():
        if cached_chunks.get(cid) != cur_sha:
            changed.append(cid)
    return changed


def _try_diff_aware_extract(
    drive_file: DriveFile,
    *,
    local_path: Path,
    content_sha: str,
    root_context: str,
    cache: Cache,
) -> ExtractionResult | MultiExtractionResult | None:
    """Attempt the diff-aware path. Returns the merged extraction on
    success, or None if the path is not applicable (no cached state,
    no changed chunks, malformed LLM output) — caller falls back to
    full extract.

    The merge logic:
      - Tasks in unchanged chunks → reuse cached task bodies (the cached
        extraction has them).
      - Tasks in changed chunks → take fresh tasks from the diff
        extractor's output.
      - Epic → reuse cached unless diff extractor reports
        epic_changed=true.
    """
    cached_payload = cache.get_extraction(drive_file.id, content_sha)
    # Tier 2 hits before this is called, so a True hit here means the
    # caller is being defensive. Either way return None to fall through.
    if cached_payload is not None:
        return None

    # We need an OLD cached extraction (the one before this content_sha
    # change) to reuse unchanged tasks. Look it up by the entry's stored
    # content_sha — i.e. whatever extraction is in the cache, regardless
    # of current content match.
    entry = cache.files.get(drive_file.id)
    if entry is None or entry.extraction_payload is None:
        return None
    cached_extraction = entry.extraction_payload
    cached_chunks = cache.get_chunks(drive_file.id)
    cached_anchors = cache.get_task_anchors(drive_file.id)

    if not cached_chunks or not cached_anchors:
        return None  # diff payload missing — fall back to full extract

    file_text = local_path.read_text(encoding="utf-8", errors="replace")
    current_chunks = chunk_markdown(file_text)
    current_shas = {c.chunk_id: c.body_sha for c in current_chunks}
    changed = _changed_chunk_ids(cached_chunks, current_shas)

    if not changed:
        # No chunk content actually changed even though content_sha differs.
        # Could be whitespace normalization or non-chunked tail edits.
        # Safest: fall back to full extract.
        return None

    logger.info(
        "diff-aware extract: %s — %d changed chunk(s): %s",
        drive_file.name, len(changed), changed,
    )

    try:
        diff_result: DiffExtractionResult = extract_diff_aware(
            drive_file,
            local_path=local_path,
            root_context=root_context,
            cached_extraction=cached_extraction,
            changed_chunk_ids=changed,
        )
    except ExtractionError as e:
        logger.warning(
            "diff-aware extract failed for %s (%s); falling back to full extract",
            drive_file.name, e,
        )
        return None

    # Reconstitute the full extraction by merging cached + fresh.
    merged = _merge_diff_into_cached(
        cached_extraction=cached_extraction,
        cached_anchors=cached_anchors,
        current_chunks={c.chunk_id: c for c in current_chunks},
        changed_chunk_ids=changed,
        diff_result=diff_result,
        drive_file=drive_file,
    )
    return merged


def _merge_diff_into_cached(
    *,
    cached_extraction: dict,
    cached_anchors: dict[str, dict],
    current_chunks: dict,
    changed_chunk_ids: list[str],
    diff_result: DiffExtractionResult,
    drive_file: DriveFile,
) -> ExtractionResult | MultiExtractionResult:
    """Merge: take cached items whose owning chunk is unchanged, plus
    fresh items from the diff extractor (which only emits items in
    changed chunks). The result is the full new-run extraction."""
    changed_set = set(changed_chunk_ids)
    cached_de = deserialize_extraction(cached_extraction)
    fresh_tasks = list(diff_result.tasks)

    if isinstance(cached_de, MultiExtractionResult):
        # Multi-epic: rebuild epics + tasks. We rebuild by walking the
        # cached extraction and dropping tasks whose anchor is in a
        # changed chunk; then we append fresh tasks.
        new_epics = []
        for epic in cached_de.epics:
            kept = [
                t for t in epic.tasks
                if cached_anchors.get(t.source_anchor, {}).get("chunk_id")
                not in changed_set
            ]
            new_epics.append(
                type(epic)(
                    summary=epic.summary,
                    description=epic.description,
                    assignee_name=epic.assignee_name,
                    tasks=kept,
                )
            )
        # Append fresh tasks under the most likely epic. Heuristic: the
        # epic whose summary is contained in the changed chunk_id, else
        # the first epic. This is best-effort; for clean per-epic
        # caching we'd need richer metadata. Acceptable for the common
        # case where multi-epic edits happen within one section.
        if fresh_tasks:
            target_idx = 0
            for i, epic in enumerate(new_epics):
                for cid in changed_chunk_ids:
                    if epic.summary.lower() in cid.lower() or cid.lower() in epic.summary.lower():
                        target_idx = i
                        break
            new_epics[target_idx] = type(new_epics[target_idx])(
                summary=new_epics[target_idx].summary,
                description=new_epics[target_idx].description,
                assignee_name=new_epics[target_idx].assignee_name,
                tasks=new_epics[target_idx].tasks + fresh_tasks,
            )
        return MultiExtractionResult(
            file_id=drive_file.id,
            file_name=drive_file.name,
            epics=new_epics,
        )

    # Single-epic
    kept_tasks = [
        t for t in cached_de.tasks
        if cached_anchors.get(t.source_anchor, {}).get("chunk_id")
        not in changed_set
    ]
    epic = (
        diff_result.epic
        if (diff_result.epic_changed and diff_result.epic is not None)
        else cached_de.epic
    )
    return ExtractionResult(
        file_id=drive_file.id,
        file_name=drive_file.name,
        epic=epic,
        tasks=kept_tasks + fresh_tasks,
    )


def extract_or_reuse(
    drive_file: DriveFile,
    *,
    classification: ClassifyResult,
    local_path: Path,
    content_sha: str,
    root_context: str,
    cache: Cache,
    use_cache: bool,
    on_extract_ok: Callable[[], None],
    on_extract_failed: Callable[[str], None],
    on_cache_hit_extract: Callable[[], None],
) -> ExtractionResult | MultiExtractionResult | None:
    """Top-level extraction step for one file.

    Returns the extraction (cached or fresh). None on hard failure.

    Decision order:
      1. Tier 2 cache hit (content_sha matches) → reuse.
      2. Diff-aware (cache has prior state + at least one chunk
         changed) → small LLM call, merge.
      3. Cold full extract.
    """
    # Tier 2.
    if use_cache:
        cached_payload = cache.get_extraction(drive_file.id, content_sha)
        if cached_payload is not None:
            try:
                ext = deserialize_extraction(cached_payload)
                on_cache_hit_extract()
                on_extract_ok()
                logger.info("cache: extract hit for %s", drive_file.name)
                return ext
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "cache: extract payload for %s unusable (%s); refreshing",
                    drive_file.name, e,
                )

    # Diff-aware path.
    if use_cache:
        diff_extract = _try_diff_aware_extract(
            drive_file,
            local_path=local_path,
            content_sha=content_sha,
            root_context=root_context,
            cache=cache,
        )
        if diff_extract is not None:
            on_extract_ok()
            _persist_extraction(
                cache, drive_file, content_sha, diff_extract, local_path
            )
            return diff_extract

    # Cold full extract.
    try:
        if classification.role == "single_epic":
            ext = extract_from_file(
                drive_file, local_path=local_path, root_context=root_context
            )
        elif classification.role == "multi_epic":
            ext = extract_multi_from_file(
                drive_file, local_path=local_path, root_context=root_context
            )
        else:
            return None
        on_extract_ok()
        _persist_extraction(cache, drive_file, content_sha, ext, local_path)
        return ext
    except (ExtractionError, Exception) as e:  # noqa: BLE001
        on_extract_failed(f"extract failed for {drive_file.name}: {e}")
        return None


def _persist_extraction(
    cache: Cache,
    drive_file: DriveFile,
    content_sha: str,
    ext: ExtractionResult | MultiExtractionResult,
    local_path: Path,
) -> None:
    try:
        cache.set_extraction(
            file_id=drive_file.id,
            modified_time=drive_file.modified_time.isoformat(),
            content_sha=content_sha,
            extraction_payload=serialize_extraction(ext),
        )
        # Also stamp the diff payload so the next run can do per-chunk diff.
        file_text = local_path.read_text(encoding="utf-8", errors="replace")
        diff = _compute_diff_payload(ext, file_text)
        cache.set_diff_payload(
            file_id=drive_file.id,
            chunks=diff["chunks"],
            task_anchors=diff["task_anchors"],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "cache: failed to persist extraction for %s: %s",
            drive_file.name, e,
        )
