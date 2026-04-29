"""LLM file-role classifier: each Drive file -> single_epic / multi_epic / root.

Three roles only — no "skip". Anything that isn't an actionable task list
(slides, changelogs, drafts, plans without their own tasks) is "root" and
is used as context for the extractor.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..drive.client import DriveFile
from ..llm.client import chat, load_prompt, models_classify, render_prompt

# Big enough to see top-level section structure (which is the multi_epic
# signal). The biggest task doc in this folder is ~13 KB.
_MAX_CONTENT_CHARS = 15_000
_SYSTEM_PROMPT = load_prompt("classifier")


@dataclass
class ClassifyResult:
    file_id: str
    role: str  # "single_epic" | "multi_epic" | "root"
    confidence: float
    reason: str


def _read_truncated(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    if len(text) <= _MAX_CONTENT_CHARS:
        return text
    return text[:_MAX_CONTENT_CHARS] + "\n…[truncated]"


def classify_file(
    f: DriveFile,
    *,
    local_path: Path,
    neighbor_names: list[str],
) -> ClassifyResult:
    user_msg = render_prompt(_SYSTEM_PROMPT, 
        file_name=f.name,
        neighbor_names="\n".join(f"- {n}" for n in neighbor_names if n != f.name),
        file_content=_read_truncated(local_path),
    )
    parsed, _ = chat(
        system="You output strict JSON only. No prose, no markdown.",
        user=user_msg,
        models=models_classify(),
        temperature=0.0,
        json_mode=True,
    )
    role = parsed.get("role", "root")
    # Backward-compat: older runs returned "task" for the single-epic role.
    if role == "task":
        role = "single_epic"
    if role not in {"single_epic", "multi_epic", "root"}:
        role = "root"  # default fallback: treat unknown as context
    return ClassifyResult(
        file_id=f.id,
        role=role,
        confidence=float(parsed.get("confidence", 0.0) or 0.0),
        reason=str(parsed.get("reason", "")),
    )
