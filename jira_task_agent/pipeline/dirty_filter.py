"""Filter matcher output + extractions to only items the doc changed.

Produces a flat list of `DirtySection`s. Each section carries the
matcher's pairing for its epic and only the dirty tasks (with their
extracted bodies + decisions). Sections / tasks the doc didn't change
do not appear in the output — the reconciler iterates only what should
be processed.

Conventions for `dirty_anchors_per_file[file_id]`:
  - missing entry / None     → cold path: every section + every task is kept
  - empty set                → file unchanged: nothing kept
  - populated set            → only items whose identifier is in the set
                               are kept; epic identifiers are `<epic>:N`
                               where N is the section index
"""
from __future__ import annotations

from dataclasses import dataclass

from ..drive.client import DriveFile
from .extractor import (
    ExtractedTask,
    ExtractionResult,
    MultiExtractionResult,
)
from .matcher import FileEpicResult, MatchDecision, MatcherResult


Extraction = ExtractionResult | MultiExtractionResult
EPIC_DIRTY_PREFIX = "<epic>:"


@dataclass
class DirtyTask:
    extracted: ExtractedTask
    decision: MatchDecision


@dataclass
class DirtySection:
    drive_file: DriveFile
    file_id: str
    file_name: str
    section_index: int
    role: str
    matched_jira_key: str | None
    epic_match_confidence: float
    epic_match_reason: str
    extracted_epic_summary: str
    extracted_epic_description: str
    extracted_epic_assignee_raw: str | None
    epic_dirty: bool
    tasks: list[DirtyTask]
    orphan_keys: list[str]


def filter_dirty(
    matcher_result: MatcherResult,
    extractions: list[tuple[DriveFile, Extraction]],
    dirty_anchors_per_file: dict[str, set[str] | None] | None = None,
) -> list[DirtySection]:
    dirty_per_file = dirty_anchors_per_file or {}
    extraction_by_id = {ext.file_id: (df, ext) for df, ext in extractions}

    frs_by_file: dict[str, list[FileEpicResult]] = {}
    for fr in matcher_result.file_results:
        frs_by_file.setdefault(fr.file_id, []).append(fr)

    out: list[DirtySection] = []
    for file_id, frs in frs_by_file.items():
        if file_id not in extraction_by_id:
            continue
        drive_file, ext = extraction_by_id[file_id]
        is_multi = isinstance(ext, MultiExtractionResult)
        role = "multi_epic" if is_multi else "single_epic"
        file_dirty = dirty_per_file.get(file_id)
        if file_dirty == set():
            continue

        frs.sort(key=lambda fr: fr.section_index)
        for fr in frs:
            section_tasks = _section_tasks(ext, fr.section_index, is_multi)
            if section_tasks is None:
                continue
            kept = _kept_tasks(fr, section_tasks, file_dirty)
            if kept is None:
                continue
            epic_dirty = (
                file_dirty is None
                or f"{EPIC_DIRTY_PREFIX}{fr.section_index}" in file_dirty
            )
            out.append(DirtySection(
                drive_file=drive_file,
                file_id=file_id,
                file_name=fr.file_name,
                section_index=fr.section_index,
                role=role,
                matched_jira_key=fr.matched_jira_key,
                epic_match_confidence=fr.epic_match_confidence,
                epic_match_reason=fr.epic_match_reason,
                extracted_epic_summary=fr.extracted_epic_summary,
                extracted_epic_description=fr.extracted_epic_description,
                extracted_epic_assignee_raw=fr.extracted_epic_assignee_raw,
                epic_dirty=epic_dirty,
                tasks=kept,
                orphan_keys=list(fr.orphan_keys),
            ))
    return out


def _section_tasks(
    ext: Extraction, section_index: int, is_multi: bool,
) -> list[ExtractedTask] | None:
    if is_multi:
        if section_index >= len(ext.epics):
            return None
        return list(ext.epics[section_index].tasks)
    return list(ext.tasks)


def _kept_tasks(
    fr: FileEpicResult,
    section_tasks: list[ExtractedTask],
    file_dirty: set[str] | None,
) -> list[DirtyTask] | None:
    """Return the DirtyTasks to process for this section, or None when
    the entire section should be skipped."""
    if file_dirty is None:
        return [
            DirtyTask(extracted=t, decision=d)
            for t, d in zip(section_tasks, fr.task_decisions)
        ]
    epic_token = f"{EPIC_DIRTY_PREFIX}{fr.section_index}"
    has_dirty_task = any(
        t.source_anchor and t.source_anchor in file_dirty
        for t in section_tasks
    )
    if not (epic_token in file_dirty or has_dirty_task):
        return None
    return [
        DirtyTask(extracted=t, decision=d)
        for t, d in zip(section_tasks, fr.task_decisions)
        if t.source_anchor and t.source_anchor in file_dirty
    ]
