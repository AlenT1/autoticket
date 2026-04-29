"""Bundle root files into a single length-bounded context blob.

Used as background context for the extractor. We keep this dumb:
concatenate root file contents, separated by clear delimiters, and trim
to a character budget to stay under the model's context window.
"""
from __future__ import annotations

from pathlib import Path

# Conservative budget; bigger models can take more, but extractor prompts
# are also long, so leave room. Override per-run if needed.
DEFAULT_BUDGET_CHARS = 30_000


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def bundle_root_context(
    root_files: list[tuple[str, Path]],
    *,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
) -> str:
    """Concatenate `(name, path)` pairs into one delimited blob.

    Files are included most-recent-first by caller-supplied order.
    The blob is truncated at `budget_chars`, breaking on file boundaries
    when possible.
    """
    if not root_files:
        return "(no root context provided)"

    parts: list[str] = []
    used = 0
    for name, path in root_files:
        body = _read(path).strip()
        if not body:
            continue
        section = (
            f"\n===== ROOT FILE: {name} =====\n{body}\n===== END {name} =====\n"
        )
        if used + len(section) > budget_chars:
            remaining = budget_chars - used
            if remaining > 200:
                parts.append(section[:remaining] + "\n…[truncated]")
            break
        parts.append(section)
        used += len(section)
    return "".join(parts) if parts else "(no usable root content)"
