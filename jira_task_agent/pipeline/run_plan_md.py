"""Render a run-plan dict to human-readable Markdown.

Pure templating. Same input dict → same output string, byte-for-byte. No
LLM calls, no Jira/Drive reads, no clock reads. Clock + live state must
be supplied by the caller in the input dict.

The output is structured per source file. For each file we render a flat
list of *write* actions (creates + updates + rollup pointers). `noop`
epics are dropped — if their children have changes, those tasks render
on their own; if not, nothing about the epic appears.

Schema (top-level dict):

    {
      "generated_at":       str,
      "mode":               str,
      "source":             str,
      "files_scanned":      int,
      "files_with_changes": int,
      "totals": {kind: int, ..., "comments": int, "jira_ops": int},
      "files": [
        {
          "file_name":      str,
          "source_url":     str | null,
          "last_edited_by": str | null,
          "actions": [
            {
              "kind":             "create_epic"|"update_epic"|"create_task"
                                  |"update_task"|"skip_completed_epic"
                                  |"covered_by_rollup",
              "target_key":       str | null,
              "jira_url":         str | null,
              "summary":          str | null,
              "description":      str | null,
              "assignee_username":str | null,
              "source_anchor":    str | null,
              "parent_epic_key":  str | null,
              "parent_epic_url":  str | null,
              "parent_epic_summary": str | null,
              "live_summary":     str | null,
              "live_description": str | null,
              "rollup_target":    str | null,
            }
          ]
        }
      ]
    }
"""
from __future__ import annotations

from typing import Any


def render_run_plan_md(plan: dict[str, Any]) -> str:
    parts: list[str] = [_render_header(plan)]
    for f in plan.get("files", []):
        parts.append(_render_file(f))
    return "\n".join(parts).rstrip() + "\n"


def _render_header(plan: dict[str, Any]) -> str:
    totals = plan.get("totals") or {}
    out = [
        f"# Run plan — {plan.get('generated_at', '?')}",
        "",
        f"**Mode:** {plan.get('mode', '?')} · "
        f"**Source:** {plan.get('source', '?')} · "
        f"**Files scanned:** "
        f"{plan.get('files_with_changes', 0)} changed of "
        f"{plan.get('files_scanned', 0)}",
        "",
        "| Action | Count |",
        "|---|---|",
    ]
    for k in (
        "create_epic", "update_epic",
        "create_task", "update_task",
        "covered_by_rollup", "skip_completed_epic",
    ):
        v = totals.get(k, 0)
        if v:
            out.append(f"| {k} | {v} |")
    out.append("")
    out.append(
        f"**Total Jira ops:** {totals.get('jira_ops', 0)} writes + "
        f"{totals.get('comments', 0)} comments"
    )
    out.append("")
    return "\n".join(out)


def _render_file(f: dict[str, Any]) -> str:
    out: list[str] = ["---", "", f"## {f.get('file_name', '?')}"]
    meta_bits = []
    if f.get("source_url"):
        meta_bits.append(f"[Open source doc]({f['source_url']})")
    if f.get("last_edited_by"):
        meta_bits.append(f"last edited by **{f['last_edited_by']}**")
    if meta_bits:
        out.append("")
        out.append("> " + " · ".join(meta_bits))

    actions = f.get("actions") or []
    if not actions:
        out.append("")
        out.append("_No changes._")
        out.append("")
        return "\n".join(out)

    updates = [a for a in actions if a.get("kind") in {"update_task", "update_epic"}]
    creates = [a for a in actions if a.get("kind") in {"create_task", "create_epic"}]
    other = [a for a in actions if a.get("kind") in {"skip_completed_epic", "covered_by_rollup"}]

    if updates:
        out.append("")
        out.append(f"### Updates ({len(updates)})")
        for a in updates:
            out.append("")
            out.append(_render_update(a))

    if creates:
        out.append("")
        out.append(f"### Creates ({len(creates)})")
        for a in creates:
            out.append("")
            out.append(_render_create(a))

    if other:
        out.append("")
        out.append("### Notes")
        for a in other:
            out.append("")
            out.append(_render_note(a))

    out.append("")
    return "\n".join(out)


def _link(key: str | None, url: str | None) -> str:
    if not key:
        return "?"
    return f"[{key}]({url})" if url else f"`{key}`"


def _quote_block(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _render_update(a: dict[str, Any]) -> str:
    kind = a.get("kind")
    head = "UPDATE TASK" if kind == "update_task" else "UPDATE EPIC"
    out = [f"#### {head} — {_link(a.get('target_key'), a.get('jira_url'))}"]
    if a.get("source_anchor"):
        out.append(f"_anchor:_ `{a['source_anchor']}`")
    out.append("")

    if a.get("live_summary") and a.get("summary"):
        out.append(f"**Title:** {a['live_summary']}  →  **{a['summary']}**")
    elif a.get("summary"):
        out.append(f"**Title:** {a['summary']}")
    out.append("")

    if a.get("description"):
        out.append("**New description:**")
        out.append("")
        out.append(_quote_block(a["description"]))
        out.append("")

    if a.get("live_description") and a.get("description"):
        out.append("<details><summary>Diff vs live description</summary>")
        out.append("")
        out.append("```diff")
        for line in a["live_description"].splitlines():
            out.append(f"- {line}")
        for line in a["description"].splitlines():
            out.append(f"+ {line}")
        out.append("```")
        out.append("")
        out.append("</details>")
        out.append("")

    mention = (
        f"[~{a['assignee_username']}]"
        if a.get("assignee_username") else "(no assignee)"
    )
    out.append(
        f"**Comment that will be posted:** mentions {mention}, names the "
        f"source doc + last editor, and includes an LLM-summarized diff."
    )
    return "\n".join(out)


def _render_create(a: dict[str, Any]) -> str:
    kind = a.get("kind")
    head = "CREATE TASK" if kind == "create_task" else "CREATE EPIC"
    out = [f"#### {head}"]
    if a.get("source_anchor"):
        out.append(f"_anchor:_ `{a['source_anchor']}`")
    out.append("")

    if a.get("summary"):
        out.append(f"**Title:** {a['summary']}")
    if kind == "create_task":
        if a.get("parent_epic_key"):
            parent_label = _link(a["parent_epic_key"], a.get("parent_epic_url"))
            tail = f" — *{a['parent_epic_summary']}*" if a.get("parent_epic_summary") else ""
            out.append(f"**Under epic:** {parent_label}{tail}")
        else:
            out.append("**Under epic:** _new epic created in this run_")
    if a.get("assignee_username"):
        out.append(f"**Assignee:** `{a['assignee_username']}`")
    out.append("")

    if a.get("description"):
        out.append("**Description:**")
        out.append("")
        out.append(_quote_block(a["description"]))
        out.append("")

    return "\n".join(out)


def _render_note(a: dict[str, Any]) -> str:
    if a.get("kind") == "skip_completed_epic":
        link = _link(a.get("target_key"), a.get("jira_url"))
        return f"- **Skipping** {link} — epic is in a completed status; no writes."
    if a.get("kind") == "covered_by_rollup":
        target = _link(a.get("rollup_target") or a.get("target_key"), a.get("jira_url"))
        anchor = f" (anchor `{a['source_anchor']}`)" if a.get("source_anchor") else ""
        return f"- **Covered by rollup:** {target}{anchor}"
    return ""
