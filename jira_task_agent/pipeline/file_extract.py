"""Per-file extraction with caching.

`extract_or_reuse` is the entrypoint. Three branches:

  - content_sha matches cached       → reuse cached extraction, dirty=∅
  - cached file_text + content diff  → diff path (labels + targeted bodies)
  - no usable cache                   → cold full extract, dirty=None

`dirty_anchors` — None means "process every item downstream" (cold).
∅ means "nothing changed" (Tier 2 hit). A populated set names the
exact identifiers (`source_anchor` for tasks, `"<epic>:N"` for epics)
the doc changed.
"""
from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path
from typing import Callable

from ..cache import Cache, deserialize_extraction, serialize_extraction
from ..drive.client import DriveFile
from .classifier import ClassifyResult
from .extractor import (
    AGENT_MARKER,
    DiffLabels,
    ExtractedEpic,
    ExtractedEpicWithTasks,
    ExtractedTask,
    ExtractionError,
    ExtractionResult,
    MultiExtractionResult,
    TargetedBodies,
    extract_diff,
    extract_from_file,
    extract_multi_from_file,
    extract_targeted,
)


logger = logging.getLogger(__name__)

Extraction = ExtractionResult | MultiExtractionResult

EPIC_DIRTY_PREFIX = "<epic>:"


def epic_dirty_token(section_index: int) -> str:
    """Stable identifier for an epic / sub-epic in `dirty_anchors`."""
    return f"{EPIC_DIRTY_PREFIX}{section_index}"


# ----------------------------------------------------------------------
# entrypoint
# ----------------------------------------------------------------------


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
) -> tuple[Extraction | None, set[str] | None]:
    if use_cache:
        hit = _try_tier2_hit(cache, drive_file, content_sha)
        if hit is not None:
            on_cache_hit_extract()
            on_extract_ok()
            return hit, set()

        diff_pair = _diff_extract(
            drive_file,
            local_path=local_path,
            root_context=root_context,
            cache=cache,
        )
        if diff_pair is not None:
            ext, dirty = diff_pair
            on_extract_ok()
            _persist(cache, drive_file, content_sha, ext, local_path)
            return ext, dirty

    try:
        ext = _cold_extract(
            drive_file,
            classification=classification,
            local_path=local_path,
            root_context=root_context,
        )
    except Exception as e:  # noqa: BLE001
        on_extract_failed(f"extract failed for {drive_file.name}: {e}")
        return None, None

    if ext is None:
        return None, None

    on_extract_ok()
    _persist(cache, drive_file, content_sha, ext, local_path)
    return ext, None


# ----------------------------------------------------------------------
# branches
# ----------------------------------------------------------------------


def _try_tier2_hit(
    cache: Cache, drive_file: DriveFile, content_sha: str,
) -> Extraction | None:
    cached = cache.get_extraction(drive_file.id, content_sha)
    if cached is None:
        return None
    try:
        ext = deserialize_extraction(cached)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "cache: extract payload for %s unusable (%s); refreshing",
            drive_file.name, e,
        )
        return None
    logger.info("cache: extract hit for %s", drive_file.name)
    return ext


def _cold_extract(
    drive_file: DriveFile,
    *,
    classification: ClassifyResult,
    local_path: Path,
    root_context: str,
) -> Extraction | None:
    if classification.role == "single_epic":
        return extract_from_file(
            drive_file, local_path=local_path, root_context=root_context,
        )
    if classification.role == "multi_epic":
        return extract_multi_from_file(
            drive_file, local_path=local_path, root_context=root_context,
        )
    return None


def _diff_extract(
    drive_file: DriveFile,
    *,
    local_path: Path,
    root_context: str,
    cache: Cache,
) -> tuple[Extraction, set[str]] | None:
    entry = cache.files.get(drive_file.id)
    if entry is None or entry.extraction_payload is None:
        return None
    cached_text = cache.get_file_text(drive_file.id)
    if not cached_text:
        return None

    current_text = local_path.read_text(encoding="utf-8", errors="replace")
    diff = compute_unified_diff(cached_text, current_text)
    if not diff:
        return None

    logger.info(
        "diff-extract: %s — %d diff line(s)",
        drive_file.name, diff.count("\n"),
    )

    try:
        labels = extract_diff(
            drive_file,
            cached_extraction=entry.extraction_payload,
            unified_diff=diff,
            current_file_text=current_text,
        )
    except ExtractionError as e:
        logger.warning(
            "diff extract failed for %s (%s); falling back to cold",
            drive_file.name, e,
        )
        return None

    cached_ext = deserialize_extraction(entry.extraction_payload)
    labels = _sanitize_labels_against_source(
        labels, cached_ext, cached_text, current_text,
    )
    targets = labels_to_targets(labels, cached_ext)
    try:
        bodies = extract_targeted(
            drive_file, local_path=local_path,
            targets=targets, root_context=root_context,
        )
    except ExtractionError as e:
        logger.warning(
            "targeted extract failed for %s (%s); falling back to cold",
            drive_file.name, e,
        )
        return None

    merged = apply_changes(cached_ext, labels, bodies, drive_file)
    return merged, compute_dirty(cached_ext, merged)


# ----------------------------------------------------------------------
# diff plumbing — pure Python
# ----------------------------------------------------------------------


def compute_unified_diff(cached_text: str, current_text: str) -> str:
    return "".join(
        difflib.unified_diff(
            cached_text.splitlines(keepends=True),
            current_text.splitlines(keepends=True),
            fromfile="cached", tofile="current", n=3,
        )
    )


def _sanitize_labels_against_source(
    labels: DiffLabels,
    cached: Extraction,
    cached_text: str,
    current_text: str,
) -> DiffLabels:
    """Drop `modified_anchors` and `epic_changed` when the source-doc
    bullet (or epic intro) is byte-identical between cached and current.

    The LLM diff extractor sometimes over-claims which sections changed.
    This deterministic check is authoritative: if the source text for a
    given task/epic didn't change, that task/epic isn't modified — no
    matter what the LLM says.
    """
    cached_bullets = _extract_task_bullets(cached_text, cached)
    return DiffLabels(
        modified_anchors=_filter_real_modified(
            labels.modified_anchors, cached_bullets, current_text,
        ),
        removed_anchors=labels.removed_anchors,
        added=labels.added,
        new_subepics=labels.new_subepics,
        epic_changed=_real_epic_changed(
            labels.epic_changed, cached_text, current_text,
        ),
    )


def _filter_real_modified(
    anchors: list[str], cached_bullets: dict[str, str], current_text: str,
) -> list[str]:
    """Drop anchors whose cached bullet is byte-identical in current
    text (LLM over-emission), AND drop anchors that don't match any
    cached bullet at all (LLM hallucinated an anchor name)."""
    real: list[str] = []
    for a in anchors:
        bullet = cached_bullets.get(a)
        if bullet is None:
            logger.info(
                "diff-extract: dropping unknown modified_anchor %r "
                "(not in cached extraction)", a,
            )
            continue
        if bullet in current_text:
            logger.info(
                "diff-extract: dropping spurious modified_anchor %r "
                "(source bullet unchanged)", a,
            )
            continue
        real.append(a)
    return real


def _real_epic_changed(
    claimed: bool, cached_text: str, current_text: str,
) -> bool:
    if not claimed:
        return False
    epic_intro = _extract_epic_intro(cached_text)
    if epic_intro and epic_intro in current_text:
        logger.info(
            "diff-extract: dropping spurious epic_changed "
            "(source intro unchanged)",
        )
        return False
    return True


def _extract_task_bullets(
    cached_text: str, cached: Extraction,
) -> dict[str, str]:
    """For each cached task with a `source_anchor`, locate its bullet
    block in `cached_text` and return `{anchor: bullet_text}`. The
    bullet block is the substring from the line where the anchor first
    appears up to (but not including) the next bullet of the same
    indent, or end of file."""
    out: dict[str, str] = {}
    lines = cached_text.splitlines(keepends=True)
    anchors = [t.source_anchor for t in _all_tasks(cached) if t.source_anchor]

    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        for a in anchors:
            if a and a in line:
                starts.append((i, a))
                break
    starts.sort()
    for j, (start_i, anchor) in enumerate(starts):
        end_i = starts[j + 1][0] if j + 1 < len(starts) else len(lines)
        out[anchor] = "".join(lines[start_i:end_i]).rstrip() + "\n"
    return out


def _extract_epic_intro(cached_text: str) -> str:
    """Everything in `cached_text` before the first markdown bullet."""
    lines = cached_text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if re.match(r"^\s*-\s", line):
            return "".join(lines[:i]).rstrip() + "\n"
    return cached_text


def labels_to_targets(labels: DiffLabels, cached: Extraction) -> dict:
    """Build the `targets` payload for the targeted extractor: every
    summary it must produce a fresh body for. Modified anchors look up
    their cached summary; added items use their LLM-given summary; epic
    targets cover single-epic body change + new sub-epics."""
    cached_by_anchor = {
        t.source_anchor: t for t in _all_tasks(cached) if t.source_anchor
    }
    section_for_anchor: dict[str, str] = {}
    if isinstance(cached, MultiExtractionResult):
        for e in cached.epics:
            for t in e.tasks:
                if t.source_anchor:
                    section_for_anchor[t.source_anchor] = e.summary

    task_targets: list[dict] = []
    for anchor in labels.modified_anchors:
        ct = cached_by_anchor.get(anchor)
        if ct is None:
            continue
        target: dict = {"summary": ct.summary}
        section = section_for_anchor.get(anchor)
        if section:
            target["section"] = section
        task_targets.append(target)
    for added in labels.added:
        target = {"summary": added["summary"]}
        if added.get("section"):
            target["section"] = added["section"]
        task_targets.append(target)

    epic_targets: list[dict] = []
    if labels.epic_changed and isinstance(cached, ExtractionResult):
        epic_targets.append({"summary": cached.epic.summary})
    for ne in labels.new_subepics:
        epic_targets.append({"summary": ne["summary"], "section": ne["summary"]})

    return {"tasks": task_targets, "epics": epic_targets}


def apply_changes(
    cached: Extraction,
    labels: DiffLabels,
    bodies: TargetedBodies,
    drive_file: DriveFile,
) -> Extraction:
    """Build the merged extraction.

    Defensive against LLM hallucination in `extract_targeted`: bodies
    are matched to targets by `source_anchor`, not by position. A body
    whose anchor is in `cached` but NOT in `labels.modified_anchors`
    is dropped (the LLM offered an unsolicited rewrite of an unchanged
    item); a body with a fresh anchor is treated as added.
    """
    body_by_anchor, added_bodies = _partition_task_bodies(
        bodies.tasks, cached, labels,
    )
    epic_body, new_subepic_bodies = _split_epic_bodies(labels, bodies)
    removed = set(labels.removed_anchors)

    def replace_or_keep(t: ExtractedTask) -> ExtractedTask | None:
        if t.source_anchor in removed:
            return None
        fresh = body_by_anchor.get(t.source_anchor)
        if fresh is None:
            return t
        return ExtractedTask(
            summary=fresh.summary or t.summary,
            description=fresh.description,
            source_anchor=t.source_anchor,
            assignee_name=fresh.assignee_name,
        )

    if isinstance(cached, MultiExtractionResult):
        return _merge_multi(
            cached, drive_file,
            replace_or_keep=replace_or_keep,
            new_subepic_bodies=new_subepic_bodies,
            added_labels=labels.added, added_bodies=added_bodies,
        )

    kept = [r for r in (replace_or_keep(t) for t in cached.tasks) if r is not None]
    kept.extend(added_bodies)
    return ExtractionResult(
        file_id=drive_file.id, file_name=drive_file.name,
        epic=epic_body or cached.epic, tasks=kept,
    )


def _partition_task_bodies(
    task_bodies: list[ExtractedTask],
    cached: Extraction,
    labels: DiffLabels,
) -> tuple[dict[str, ExtractedTask], list[ExtractedTask]]:
    """Partition `task_bodies` into (modified-by-anchor, added).

    Anchor-keyed match: a body whose anchor is in `cached` but NOT in
    `labels.modified_anchors` is a hallucination — the LLM rewrote
    an unchanged item we didn't ask about. Drop it. A body with a
    fresh anchor (not in cached) is treated as added.
    """
    cached_anchors = {
        t.source_anchor for t in _all_tasks(cached) if t.source_anchor
    }
    modified_set = set(labels.modified_anchors)
    by_anchor: dict[str, ExtractedTask] = {}
    added: list[ExtractedTask] = []
    for body in task_bodies:
        a = body.source_anchor
        if a and a in cached_anchors:
            if a in modified_set and a not in by_anchor:
                by_anchor[a] = body
            else:
                logger.info(
                    "diff-extract: dropping hallucinated body for "
                    "unsolicited cached anchor %r", a,
                )
            continue
        added.append(body)
    return by_anchor, added


def _split_epic_bodies(
    labels: DiffLabels, bodies: TargetedBodies,
) -> tuple[ExtractedEpic | None, list[ExtractedEpicWithTasks]]:
    """Match epic bodies to what the labels actually requested.

    Defensive against LLM hallucination: if `bodies.epics` has more
    entries than `labels.epic_changed + len(labels.new_subepics)` told
    it to produce, the extras are dropped.
    """
    if not bodies.epics:
        return None, []
    expected = (1 if labels.epic_changed else 0) + len(labels.new_subepics)
    if expected == 0:
        logger.info(
            "diff-extract: dropping %d hallucinated epic body(ies) "
            "(neither epic_changed nor new_subepics requested)",
            len(bodies.epics),
        )
        return None, []
    epics = bodies.epics[:expected]
    if labels.epic_changed:
        e0 = epics[0]
        single = ExtractedEpic(
            summary=e0.summary, description=e0.description,
            assignee_name=e0.assignee_name,
        )
        return single, list(epics[1:])
    return None, list(epics)


def _merge_multi(
    cached: MultiExtractionResult,
    drive_file: DriveFile,
    *,
    replace_or_keep: Callable[[ExtractedTask], ExtractedTask | None],
    new_subepic_bodies: list[ExtractedEpicWithTasks],
    added_labels: list[dict],
    added_bodies: list[ExtractedTask],
) -> MultiExtractionResult:
    new_epics: list[ExtractedEpicWithTasks] = []
    for e in cached.epics:
        kept = [r for r in (replace_or_keep(t) for t in e.tasks) if r is not None]
        new_epics.append(
            ExtractedEpicWithTasks(
                summary=e.summary, description=e.description,
                assignee_name=e.assignee_name, tasks=kept,
            )
        )
    new_epics.extend(new_subepic_bodies)

    by_section = {_normalize_section(ep.summary): i for i, ep in enumerate(new_epics)}
    for label, fresh in zip(added_labels, added_bodies):
        section = label.get("section")
        idx = by_section.get(_normalize_section(section)) if section else None
        if idx is None:
            logger.warning(
                "added task %r section=%r unmapped; routing to first sub-epic",
                (fresh.summary if fresh else label.get("summary"))[:60],
                section,
            )
            idx = 0
        new_epics[idx] = ExtractedEpicWithTasks(
            summary=new_epics[idx].summary,
            description=new_epics[idx].description,
            assignee_name=new_epics[idx].assignee_name,
            tasks=new_epics[idx].tasks + [fresh],
        )

    return MultiExtractionResult(
        file_id=drive_file.id, file_name=drive_file.name, epics=new_epics,
    )


# ----------------------------------------------------------------------
# compute_dirty
# ----------------------------------------------------------------------


def compute_dirty(cached: Extraction, merged: Extraction) -> set[str]:
    """Return identifiers of every node in `merged` that differs from
    `cached` (or is absent from it). Tasks identified by `source_anchor`,
    epics by `epic_dirty_token(section_index)`."""
    dirty: set[str] = set()
    dirty.update(_dirty_tasks(cached, merged))
    dirty.update(_dirty_epics(cached, merged))
    return dirty


def _dirty_tasks(cached: Extraction, merged: Extraction) -> set[str]:
    cached_by_anchor = {
        t.source_anchor: t for t in _all_tasks(cached) if t.source_anchor
    }
    out: set[str] = set()
    for t in _all_tasks(merged):
        if not t.source_anchor:
            continue
        prior = cached_by_anchor.get(t.source_anchor)
        if prior is None or _task_changed(prior, t):
            out.add(t.source_anchor)
    return out


def _dirty_epics(cached: Extraction, merged: Extraction) -> set[str]:
    out: set[str] = set()
    if isinstance(cached, MultiExtractionResult) and isinstance(merged, MultiExtractionResult):
        for i, ne in enumerate(merged.epics):
            if i >= len(cached.epics) or _epic_changed(cached.epics[i], ne):
                out.add(epic_dirty_token(i))
    elif isinstance(cached, ExtractionResult) and isinstance(merged, ExtractionResult):
        if _epic_changed(cached.epic, merged.epic):
            out.add(epic_dirty_token(0))
    return out


def _all_tasks(ext: Extraction) -> list[ExtractedTask]:
    if isinstance(ext, MultiExtractionResult):
        return [t for e in ext.epics for t in e.tasks]
    return list(ext.tasks)


def _norm(s: str | None) -> str:
    """Strict equality after stripping the agent marker and trimming."""
    return (s or "").replace(AGENT_MARKER, "").strip()


def _norm_loose(s: str | None) -> str:
    """Cosmetic-equivalence key: strip marker, collapse all whitespace
    runs to single spaces, lowercase. Two bodies normalizing to the
    same string say the same thing in the same words — different only
    in formatting. LLM paraphrase still produces different keys here,
    so this is a *byte-equivalence-after-formatting* check, not a
    semantic one."""
    base = (s or "").replace(AGENT_MARKER, "")
    return " ".join(base.lower().split())


def _normalize_section(s: str | None) -> str:
    """Case- and whitespace-insensitive key for matching section names
    across the diff prompt's `section` field and the targeted prompt's
    sub-epic `summary`. The two LLM calls don't always echo identical
    casing/spacing for the same section."""
    return " ".join((s or "").lower().split())


def _task_changed(prev: ExtractedTask, curr: ExtractedTask) -> bool:
    if prev.summary != curr.summary:
        return True
    if _norm(prev.description) == _norm(curr.description):
        return False
    # Defensive: bodies differ only in whitespace / casing → drop.
    return _norm_loose(prev.description) != _norm_loose(curr.description)


def _epic_changed(
    prev: ExtractedEpic | ExtractedEpicWithTasks,
    curr: ExtractedEpic | ExtractedEpicWithTasks,
) -> bool:
    if prev.summary != curr.summary:
        return True
    if _norm(prev.description) == _norm(curr.description):
        return False
    return _norm_loose(prev.description) != _norm_loose(curr.description)


# ----------------------------------------------------------------------
# persistence
# ----------------------------------------------------------------------


def _persist(
    cache: Cache,
    drive_file: DriveFile,
    content_sha: str,
    ext: Extraction,
    local_path: Path,
) -> None:
    try:
        cache.set_extraction(
            file_id=drive_file.id,
            modified_time=drive_file.modified_time.isoformat(),
            content_sha=content_sha,
            extraction_payload=serialize_extraction(ext),
        )
        cache.set_file_text(
            drive_file.id,
            local_path.read_text(encoding="utf-8", errors="replace"),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "cache: failed to persist extraction for %s: %s",
            drive_file.name, e,
        )
