"""LLM extractor: one task file (+ root context) -> {epic, [tasks]}.

Output schema (after validation):

  ExtractedEpic
    summary: str
    description: str   # ends with the agent marker

  ExtractedTask
    summary: str
    description: str   # contains '### Definition of Done', ends with marker
    source_anchor: str
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..drive.client import DriveFile
from ..llm.client import chat, load_prompt, models_extract, render_prompt

AGENT_MARKER = "<!-- managed-by:jira-task-agent v1 -->"
_SYSTEM_PROMPT = load_prompt("extractor")
_SYSTEM_PROMPT_MULTI = load_prompt("extractor_multi")
_SYSTEM_PROMPT_DIFF = load_prompt("extractor_diff_aware")


@dataclass
class ExtractedEpic:
    summary: str
    description: str
    assignee_name: str | None = None  # raw owner string from the source doc


@dataclass
class ExtractedTask:
    summary: str
    description: str
    source_anchor: str
    assignee_name: str | None = None  # raw owner string from the source doc


@dataclass
class ExtractionResult:
    file_id: str
    file_name: str
    epic: ExtractedEpic
    tasks: list[ExtractedTask]


@dataclass
class ExtractedEpicWithTasks:
    """Used for `multi_epic` files where one doc -> N self-contained epics."""

    summary: str
    description: str
    assignee_name: str | None
    tasks: list[ExtractedTask]


@dataclass
class MultiExtractionResult:
    file_id: str
    file_name: str
    epics: list[ExtractedEpicWithTasks]


@dataclass
class DiffExtractionResult:
    """Output of the diff-aware extractor.

    Carries only the parts of the file that changed since the cached
    extraction. The runner merges this with the cached extraction to
    produce the full extraction for the current run.

    epic_changed=False + epic=None means: reuse the cached epic verbatim.
    tasks is the list of tasks living in changed chunks (could be a
    re-extraction of an edited task, or a brand-new task added to a
    changed chunk). Tasks in unchanged chunks are NOT included here.
    """
    file_id: str
    file_name: str
    epic_changed: bool
    epic: ExtractedEpic | None
    tasks: list[ExtractedTask]


class ExtractionError(RuntimeError):
    pass


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _ensure_marker(text: str) -> str:
    text = (text or "").rstrip()
    if AGENT_MARKER in text:
        return text
    if not text:
        return AGENT_MARKER
    return f"{text}\n\n{AGENT_MARKER}"


_ASSIGNEE_ANNOTATION_RE = re.compile(r"\s*[(\[].*?[)\]]\s*$")
_COMPOSITE_OWNER_SPLIT_RE = re.compile(r"\s*(?:\+|/|&|,| and )\s*")


def _clean_assignee(raw: object) -> str | None:
    """Trim trailing `(50% …)` / `[on leave]` annotations from owner strings.

    Preserves composite forms unchanged: "Lior + Aviv", "Nick/Joe", etc.
    """
    if not raw:
        return None
    s = str(raw).strip()
    while True:
        new = _ASSIGNEE_ANNOTATION_RE.sub("", s).strip()
        if new == s:
            break
        s = new
    return s or None


def _split_co_owners(raw: object) -> tuple[str | None, list[str]]:
    """Return `(first_owner, [other_owners…])` for composite owner strings.

    "Lior + Aviv"            -> ("Lior", ["Aviv"])
    "Nick/Joe + Guy"         -> ("Nick", ["Joe", "Guy"])
    "Guy (50%) + Sharon"     -> ("Guy", ["Sharon"])  (each part is cleaned)
    "Sharon"                 -> ("Sharon", [])
    None / "" / "—"          -> (None, [])
    """
    cleaned = _clean_assignee(raw)
    if not cleaned:
        return (None, [])
    parts = [p.strip() for p in _COMPOSITE_OWNER_SPLIT_RE.split(cleaned) if p.strip()]
    # Strip per-part trailing-paren annotations too, so "Guy (50%) + Sharon"
    # yields ("Guy", ["Sharon"]) rather than ("Guy (50%)", ["Sharon"]).
    parts = [_clean_assignee(p) or "" for p in parts]
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return (parts[0] if parts else None, [])
    return (parts[0], parts[1:])


def _inject_co_owners(description: str, raw_owner: object) -> str:
    """If `raw_owner` is a composite, append a `Co-owners: ...` line to the
    description (just before the agent marker line, or at the end if the
    marker isn't present yet). No-op for single owners or empty.
    """
    _, others = _split_co_owners(raw_owner)
    if not others:
        return description
    co_line = f"Co-owners: {', '.join(others)}"
    if AGENT_MARKER in description:
        return description.replace(
            AGENT_MARKER, f"{co_line}\n\n{AGENT_MARKER}", 1
        )
    return description.rstrip() + f"\n\n{co_line}"


_BANNED_EPIC_CONNECTORS = (" and ", " & ", " + ", " with ", " plus ", " along with ")


def _validate_summary(
    s: str,
    what: str,
    *,
    min_len: int = 8,
    max_len: int = 120,
    ban_coord_connectors: bool = False,
) -> None:
    stripped = s.strip()
    n = len(stripped)
    if not (min_len <= n <= max_len):
        raise ExtractionError(
            f"{what} summary length out of range ({min_len}..{max_len}): "
            f"{n} chars: {s!r}"
        )
    if ban_coord_connectors:
        lowered = stripped.lower()
        for bad in _BANNED_EPIC_CONNECTORS:
            if bad in lowered:
                raise ExtractionError(
                    f"{what} summary contains banned coordinator '{bad.strip()}' — "
                    f"it joins two ideas; use a single umbrella term instead. "
                    f"Got: {s!r}"
                )


def _validate_task_description(desc: str, idx: int) -> None:
    if "### Definition of Done" not in desc:
        raise ExtractionError(
            f"Task #{idx + 1} description missing '### Definition of Done' heading"
        )
    # require at least 3 checklist items in DoD
    after = desc.split("### Definition of Done", 1)[1]
    items = [ln for ln in after.splitlines() if ln.strip().startswith("- [")]
    if len(items) < 3:
        raise ExtractionError(
            f"Task #{idx + 1} DoD has only {len(items)} checklist item(s); need >= 3"
        )


def extract_from_file(
    f: DriveFile,
    *,
    local_path: Path,
    root_context: str,
) -> ExtractionResult:
    user_msg = render_prompt(_SYSTEM_PROMPT, 
        task_file_name=f.name,
        last_modifying_user_name=f.last_modifying_user_name or "(unknown)",
        task_file_content=_read(local_path),
        root_context=root_context,
    )
    parsed, _ = chat(
        system="You output strict JSON only. Never include markdown fences or prose.",
        user=user_msg,
        models=models_extract(),
        temperature=0.1,
        json_mode=True,
    )
    if not isinstance(parsed, dict):
        raise ExtractionError("extractor did not return a JSON object")

    epic_d = parsed.get("epic") or {}
    tasks_d = parsed.get("tasks") or []
    if not isinstance(epic_d, dict) or not isinstance(tasks_d, list) or not tasks_d:
        raise ExtractionError(
            "extractor JSON missing or empty 'epic'/'tasks' fields"
        )

    epic = ExtractedEpic(
        summary=str(epic_d.get("summary", "")).strip(),
        description=_ensure_marker(
            _inject_co_owners(
                str(epic_d.get("description", "")),
                epic_d.get("assignee"),
            )
        ),
        assignee_name=_clean_assignee(epic_d.get("assignee")),
    )
    _validate_summary(
        epic.summary, "Epic", min_len=12, max_len=70, ban_coord_connectors=True
    )

    tasks: list[ExtractedTask] = []
    for i, t in enumerate(tasks_d):
        if not isinstance(t, dict):
            raise ExtractionError(f"Task #{i + 1} is not an object")
        summary = str(t.get("summary", "")).strip()
        description = _ensure_marker(
            _inject_co_owners(str(t.get("description", "")), t.get("assignee"))
        )
        anchor = str(t.get("source_anchor", "")).strip()[:60]
        _validate_summary(summary, f"Task #{i + 1}")
        _validate_task_description(description, i)
        tasks.append(
            ExtractedTask(
                summary=summary,
                description=description,
                source_anchor=anchor,
                assignee_name=_clean_assignee(t.get("assignee")),
            )
        )

    return ExtractionResult(
        file_id=f.id,
        file_name=f.name,
        epic=epic,
        tasks=tasks,
    )


def extract_multi_from_file(
    f: DriveFile,
    *,
    local_path: Path,
    root_context: str,
) -> MultiExtractionResult:
    """For `multi_epic` files: one doc -> N self-contained epics with children."""
    user_msg = render_prompt(
        _SYSTEM_PROMPT_MULTI,
        task_file_name=f.name,
        last_modifying_user_name=f.last_modifying_user_name or "(unknown)",
        task_file_content=_read(local_path),
        root_context=root_context,
    )
    parsed, _ = chat(
        system="You output strict JSON only. Never include markdown fences or prose.",
        user=user_msg,
        models=models_extract(),
        temperature=0.1,
        json_mode=True,
    )
    if not isinstance(parsed, dict):
        raise ExtractionError("multi-extractor did not return a JSON object")

    epics_d = parsed.get("epics") or []
    if not isinstance(epics_d, list) or not epics_d:
        raise ExtractionError("multi-extractor JSON missing or empty 'epics' array")

    epics: list[ExtractedEpicWithTasks] = []
    for ei, ed in enumerate(epics_d):
        if not isinstance(ed, dict):
            raise ExtractionError(f"Epic #{ei + 1} is not an object")
        epic_summary = str(ed.get("summary", "")).strip()
        epic_description = _ensure_marker(
            _inject_co_owners(str(ed.get("description", "")), ed.get("assignee"))
        )
        _validate_summary(
            epic_summary,
            f"Epic #{ei + 1}",
            min_len=12,
            max_len=70,
            ban_coord_connectors=True,
        )

        sub_tasks_d = ed.get("tasks") or []
        if not isinstance(sub_tasks_d, list) or not sub_tasks_d:
            raise ExtractionError(
                f"Epic #{ei + 1} ({epic_summary!r}) has no tasks"
            )

        sub_tasks: list[ExtractedTask] = []
        for ti, t in enumerate(sub_tasks_d):
            if not isinstance(t, dict):
                raise ExtractionError(
                    f"Epic #{ei + 1} task #{ti + 1} is not an object"
                )
            summary = str(t.get("summary", "")).strip()
            description = _ensure_marker(
                _inject_co_owners(str(t.get("description", "")), t.get("assignee"))
            )
            anchor = str(t.get("source_anchor", "")).strip()[:60]
            _validate_summary(summary, f"Epic #{ei + 1} task #{ti + 1}")
            _validate_task_description(description, ti)
            sub_tasks.append(
                ExtractedTask(
                    summary=summary,
                    description=description,
                    source_anchor=anchor,
                    assignee_name=_clean_assignee(t.get("assignee")),
                )
            )

        epics.append(
            ExtractedEpicWithTasks(
                summary=epic_summary,
                description=epic_description,
                assignee_name=_clean_assignee(ed.get("assignee")),
                tasks=sub_tasks,
            )
        )

    return MultiExtractionResult(
        file_id=f.id,
        file_name=f.name,
        epics=epics,
    )


# ----------------------------------------------------------------------
# Diff-aware extractor
# ----------------------------------------------------------------------


def _serialize_cached_for_prompt(cached: dict | None) -> str:
    """Compact representation of the cached extraction passed to the
    diff-aware prompt. We pass enough for the LLM to know what was
    already extracted (so it doesn't re-emit), but not full bodies —
    it can see those in the file directly. Saves output tokens.
    """
    if not cached:
        return "{}"
    if cached.get("type") == "multi_epic":
        compact = {
            "type": "multi_epic",
            "epics": [
                {
                    "summary": e.get("summary", ""),
                    "tasks": [
                        {
                            "summary": t.get("summary", ""),
                            "source_anchor": t.get("source_anchor", ""),
                        }
                        for t in (e.get("tasks") or [])
                    ],
                }
                for e in (cached.get("epics") or [])
            ],
        }
    else:
        compact = {
            "type": "single_epic",
            "epic": {"summary": (cached.get("epic") or {}).get("summary", "")},
            "tasks": [
                {
                    "summary": t.get("summary", ""),
                    "source_anchor": t.get("source_anchor", ""),
                }
                for t in (cached.get("tasks") or [])
            ],
        }
    return json.dumps(compact, ensure_ascii=False, indent=2)


def extract_diff_aware(
    f: DriveFile,
    *,
    local_path: Path,
    root_context: str,
    cached_extraction: dict | None,
    changed_chunk_ids: list[str],
) -> DiffExtractionResult:
    """Re-extract ONLY items in `changed_chunk_ids`.

    The LLM gets the full current file + root context + a compact view
    of the cached extraction + the list of changed chunk headings. It
    returns just the items in changed chunks — the runner merges those
    with cached items from unchanged chunks.

    This saves output tokens (small response) while preserving full
    input context for quality (no extraction-quality regression).

    If the LLM produces malformed output, raises `ExtractionError`. The
    runner catches this and falls back to a full re-extract.
    """
    if not changed_chunk_ids:
        # Nothing to do — entire file unchanged. Caller shouldn't have
        # invoked us, but handle defensively.
        return DiffExtractionResult(
            file_id=f.id,
            file_name=f.name,
            epic_changed=False,
            epic=None,
            tasks=[],
        )

    user_msg = render_prompt(
        _SYSTEM_PROMPT_DIFF,
        task_file_name=f.name,
        last_modifying_user_name=f.last_modifying_user_name or "(unknown)",
        file_text=_read(local_path),
        root_context=root_context,
        cached_extraction_json=_serialize_cached_for_prompt(cached_extraction),
        changed_chunks_json=json.dumps(changed_chunk_ids, ensure_ascii=False),
    )
    parsed, _ = chat(
        system="You output strict JSON only. Never include markdown fences or prose.",
        user=user_msg,
        models=models_extract(),
        temperature=0.1,
        json_mode=True,
    )
    if not isinstance(parsed, dict):
        raise ExtractionError("diff extractor did not return a JSON object")

    epic_changed = bool(parsed.get("epic_changed"))
    epic_d = parsed.get("epic")
    tasks_d = parsed.get("tasks") or []
    if not isinstance(tasks_d, list):
        raise ExtractionError("diff extractor 'tasks' is not a list")

    epic_obj: ExtractedEpic | None = None
    if epic_changed:
        if not isinstance(epic_d, dict):
            raise ExtractionError(
                "diff extractor reported epic_changed=true but did not "
                "return an 'epic' object"
            )
        epic_obj = ExtractedEpic(
            summary=str(epic_d.get("summary", "")).strip(),
            description=_ensure_marker(
                _inject_co_owners(
                    str(epic_d.get("description", "")),
                    epic_d.get("assignee"),
                )
            ),
            assignee_name=_clean_assignee(epic_d.get("assignee")),
        )
        _validate_summary(
            epic_obj.summary,
            "Epic",
            min_len=12,
            max_len=70,
            ban_coord_connectors=True,
        )

    tasks: list[ExtractedTask] = []
    for i, t in enumerate(tasks_d):
        if not isinstance(t, dict):
            raise ExtractionError(f"diff Task #{i + 1} is not an object")
        summary = str(t.get("summary", "")).strip()
        description = _ensure_marker(
            _inject_co_owners(str(t.get("description", "")), t.get("assignee"))
        )
        anchor = str(t.get("source_anchor", "")).strip()[:60]
        _validate_summary(summary, f"Diff task #{i + 1}")
        _validate_task_description(description, i)
        tasks.append(
            ExtractedTask(
                summary=summary,
                description=description,
                source_anchor=anchor,
                assignee_name=_clean_assignee(t.get("assignee")),
            )
        )

    return DiffExtractionResult(
        file_id=f.id,
        file_name=f.name,
        epic_changed=epic_changed,
        epic=epic_obj,
        tasks=tasks,
    )
