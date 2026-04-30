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
_SYSTEM_PROMPT_DIFF = load_prompt("extractor_diff")
_SYSTEM_PROMPT_TARGETED = load_prompt("extractor_targeted")


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
# Unified-diff extractor
@dataclass
class DiffLabels:
    """Labels-only verdict from the diff prompt."""
    modified_anchors: list[str]
    removed_anchors: list[str]
    added: list[dict]            # [{summary, section?}]
    new_subepics: list[dict]     # [{summary}]
    epic_changed: bool


@dataclass
class TargetedBodies:
    """Full-body extracts for items the diff prompt flagged."""
    tasks: list[ExtractedTask]
    task_sections: list[str | None]
    epics: list[ExtractedEpicWithTasks]
    epic_sections: list[str | None]


def _serialize_cached_for_diff(cached: dict | None) -> tuple[str, str, str]:
    if not cached:
        return "[]", "null", "[]"
    if cached.get("type") == "multi_epic":
        items, sections = [], []
        for e in cached.get("epics") or []:
            sections.append({"summary": e.get("summary", "")})
            for t in e.get("tasks") or []:
                items.append({
                    "summary": t.get("summary", ""),
                    "source_anchor": t.get("source_anchor", ""),
                    "section_summary": e.get("summary", ""),
                })
        return (
            json.dumps(items, ensure_ascii=False, indent=2),
            "null",
            json.dumps(sections, ensure_ascii=False, indent=2),
        )
    items = [
        {"summary": t.get("summary", ""), "source_anchor": t.get("source_anchor", "")}
        for t in (cached.get("tasks") or [])
    ]
    epic = cached.get("epic") or {}
    return (
        json.dumps(items, ensure_ascii=False, indent=2),
        json.dumps({"summary": epic.get("summary", "")}, ensure_ascii=False, indent=2),
        "[]",
    )


def _cached_anchor_set(cached: dict | None) -> set[str]:
    if not cached:
        return set()
    out: set[str] = set()
    if cached.get("type") == "multi_epic":
        for e in cached.get("epics") or []:
            for t in e.get("tasks") or []:
                a = t.get("source_anchor")
                if a:
                    out.add(a)
        return out
    for t in cached.get("tasks") or []:
        a = t.get("source_anchor")
        if a:
            out.add(a)
    return out


def extract_diff(
    f: DriveFile,
    *,
    cached_extraction: dict,
    unified_diff: str,
    current_file_text: str,
) -> DiffLabels:
    """Label what changed between the cached and current file. No bodies."""
    if not unified_diff.strip():
        return DiffLabels([], [], [], [], False)

    items_json, epic_json, sections_json = _serialize_cached_for_diff(cached_extraction)
    user_msg = render_prompt(
        _SYSTEM_PROMPT_DIFF,
        task_file_name=f.name,
        cached_items_json=items_json,
        cached_epic_json=epic_json,
        cached_sections_json=sections_json,
        unified_diff=unified_diff,
        current_file=current_file_text,
    )
    parsed, _ = chat(
        system="You output strict JSON only. Never include markdown fences or prose.",
        user=user_msg,
        models=models_extract(),
        temperature=0.0,
        json_mode=True,
    )
    if not isinstance(parsed, dict):
        raise ExtractionError("diff extractor did not return a JSON object")

    cached_anchors = _cached_anchor_set(cached_extraction)
    return DiffLabels(
        modified_anchors=[
            a for a in (parsed.get("modified_anchors") or [])
            if isinstance(a, str) and (not cached_anchors or a in cached_anchors)
        ],
        removed_anchors=[
            a for a in (parsed.get("removed_anchors") or [])
            if isinstance(a, str) and (not cached_anchors or a in cached_anchors)
        ],
        added=[
            {
                "summary": str(t.get("summary", "")).strip(),
                "section": str(t.get("section", "")).strip() or None,
            }
            for t in (parsed.get("added") or [])
            if isinstance(t, dict) and str(t.get("summary", "")).strip()
        ],
        new_subepics=[
            {"summary": str(e.get("summary", "")).strip()}
            for e in (parsed.get("new_subepics") or [])
            if isinstance(e, dict) and str(e.get("summary", "")).strip()
        ],
        epic_changed=bool(parsed.get("epic_changed")),
    )


def extract_targeted(
    f: DriveFile,
    *,
    local_path: Path,
    targets: dict,
    root_context: str,
) -> TargetedBodies:
    """Produce full Jira-quality bodies for the items in `targets`.
    `targets = {"tasks": [{summary, section?}], "epics": [{summary, section?}]}`.
    Empty targets → no LLM call."""
    if not (targets.get("tasks") or targets.get("epics")):
        return TargetedBodies([], [], [], [])

    user_msg = render_prompt(
        _SYSTEM_PROMPT_TARGETED,
        task_file_name=f.name,
        last_modifying_user_name=f.last_modifying_user_name or "(unknown)",
        task_file_content=_read(local_path),
        targets_json=json.dumps(targets, ensure_ascii=False, indent=2),
        root_context=root_context,
    )
    parsed, _ = chat(
        system="You output strict JSON only. Never include markdown fences or prose.",
        user=user_msg,
        models=models_extract(),
        temperature=0.0,
        json_mode=True,
    )
    if not isinstance(parsed, dict):
        raise ExtractionError("targeted extractor did not return a JSON object")

    tasks, task_sections = _parse_targeted_tasks(parsed.get("tasks") or [])
    epics, epic_sections = _parse_targeted_epics(parsed.get("epics") or [])
    return TargetedBodies(
        tasks=tasks, task_sections=task_sections,
        epics=epics, epic_sections=epic_sections,
    )


def _parse_targeted_tasks(
    raw: list,
) -> tuple[list[ExtractedTask], list[str | None]]:
    tasks: list[ExtractedTask] = []
    sections: list[str | None] = []
    for i, t in enumerate(raw):
        if not isinstance(t, dict):
            continue
        summary = str(t.get("summary", "")).strip()
        description = _ensure_marker(
            _inject_co_owners(str(t.get("description", "")), t.get("assignee"))
        )
        anchor = str(t.get("source_anchor", "")).strip()[:60]
        _validate_summary(summary, f"Targeted task #{i + 1}")
        _validate_task_description(description, i)
        tasks.append(
            ExtractedTask(
                summary=summary, description=description,
                source_anchor=anchor,
                assignee_name=_clean_assignee(t.get("assignee")),
            )
        )
        sec = t.get("section")
        sections.append(str(sec).strip() if sec else None)
    return tasks, sections


def _parse_targeted_epics(
    raw: list,
) -> tuple[list[ExtractedEpicWithTasks], list[str | None]]:
    epics: list[ExtractedEpicWithTasks] = []
    sections: list[str | None] = []
    for i, e in enumerate(raw):
        if not isinstance(e, dict):
            continue
        summary = str(e.get("summary", "")).strip()
        description = _ensure_marker(
            _inject_co_owners(str(e.get("description", "")), e.get("assignee"))
        )
        try:
            _validate_summary(
                summary, f"Targeted epic #{i + 1}",
                min_len=12, max_len=70, ban_coord_connectors=True,
            )
        except ExtractionError:
            continue
        epics.append(
            ExtractedEpicWithTasks(
                summary=summary, description=description,
                assignee_name=_clean_assignee(e.get("assignee")),
                tasks=[],
            )
        )
        sec = e.get("section")
        sections.append(str(sec).strip() if sec else None)
    return epics, sections
