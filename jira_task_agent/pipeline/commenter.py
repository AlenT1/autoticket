"""Compose Jira changelog comments for update actions.

Comment shape:

    [~assigneeUsername]

    This issue was updated by the doc-sync agent.

    Source change:
    - Doc: <name> (<webViewLink>)
    - Edited by: <last_modifying_user_name> at <iso>

    What changed:
    - <bullet from summarizer>
    - <bullet>
    ...

If the issue is unassigned the @-mention is replaced with a warning prefix.
"""
from __future__ import annotations

from ..drive.client import DriveFile
from ..llm.client import chat, load_prompt, models_summarize, render_prompt

_SYSTEM_PROMPT = load_prompt("summarize")


def _summarize_changes(
    *,
    before_summary: str | None,
    after_summary: str | None,
    before_description: str | None,
    after_description: str | None,
) -> str:
    user_msg = render_prompt(_SYSTEM_PROMPT, 
        before_summary=before_summary or "(empty)",
        after_summary=after_summary or "(empty)",
        before_description=before_description or "(empty)",
        after_description=after_description or "(empty)",
    )
    text, _ = chat(
        system="You output 2-5 short Markdown bullets. No JSON, no headings.",
        user=user_msg,
        models=models_summarize(),
        temperature=0.1,
        json_mode=False,
    )
    return str(text).strip()


def format_update_comment(
    *,
    assignee_username: str | None,
    fallback_username: str | None,
    drive_file: DriveFile,
    before_summary: str | None,
    after_summary: str | None,
    before_description: str | None,
    after_description: str | None,
) -> str:
    mention_user = assignee_username or fallback_username
    head = f"[~{mention_user}]" if mention_user else "(unassigned — no @mention available)"

    bullets = _summarize_changes(
        before_summary=before_summary,
        after_summary=after_summary,
        before_description=before_description,
        after_description=after_description,
    )

    parts = [head, "", "This issue was updated by the doc-sync agent.", ""]

    if before_summary != after_summary:
        parts.append(f"- Summary: {before_summary!r} → {after_summary!r}")
    parts.append(
        f"- Source doc: [{drive_file.name}]({drive_file.web_view_link or ''})"
    )
    parts.append(
        f"- Last edited by {drive_file.last_modifying_user_name or 'unknown'} "
        f"at {drive_file.modified_time.isoformat()}"
    )
    parts.append("")
    parts.append("What changed:")
    parts.append(bullets)

    return "\n".join(parts)
