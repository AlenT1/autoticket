"""Snapshot a Jira project as a single tree (epics + their direct children).

One paginated `/search` call instead of one call per epic — much faster
for projects with many epics. Sub-tasks are excluded since the agent
operates only on the epic-task level.

Used by:
  - `runner.run_once` (per-run live snapshot fed into `pipeline.matcher`)
  - `scripts/list_project_tree.py` (write the snapshot to JSON for tests)
"""
from __future__ import annotations

import time
from typing import Any

import requests

from .client import JiraClient

# Issue types the agent matches on. Sub-tasks belong to a Task/Story/Bug,
# not directly to an Epic, so they're excluded.
_WANTED_TYPES = ("Epic", "Task", "Story", "Bug")
_PAGE_SIZE = 100


def _normalize_min(issue: dict, epic_link_id: str | None) -> dict:
    """Compact normalized form for one issue. Only the fields the
    comparator actually needs."""
    f = issue.get("fields") or {}
    assignee = f.get("assignee") or {}
    status = f.get("status") or {}
    issuetype = f.get("issuetype") or {}
    return {
        "key": issue.get("key"),
        "summary": f.get("summary"),
        "description": f.get("description"),
        "issue_type": issuetype.get("name"),
        "status": status.get("name"),
        "assignee_name": assignee.get("displayName"),
        "assignee_username": assignee.get("name"),
        "epic_link": f.get(epic_link_id) if epic_link_id else None,
        "labels": f.get("labels") or [],
        "created": f.get("created"),
        "updated": f.get("updated"),
    }


def fetch_project_tree(
    client: JiraClient,
    project_key: str,
    *,
    page_size: int = _PAGE_SIZE,
    log: Any = None,
) -> dict:
    """Return the project's epic tree as:

        {
          "project_key": "...",
          "epic_count": int,
          "child_count": int,
          "orphan_count": int,
          "epics": [{...epic..., "children": [...]}, ...],
        }

    `log` is an optional file-like for progress messages (e.g. sys.stderr).
    """
    epic_link_id = client.epic_link_field_id()

    types_clause = ", ".join(f'"{t}"' for t in _WANTED_TYPES)
    jql = f'project = "{project_key}" AND issuetype in ({types_clause})'
    fields_csv = ",".join(
        [
            "summary", "description", "status", "assignee", "issuetype",
            "labels", "created", "updated",
            epic_link_id or "summary",
        ]
    )

    issues: list[dict] = []
    start = 0
    total_expected: int | None = None
    started = time.monotonic()
    while True:
        resp = requests.get(
            f"{client.api_base}/search",
            headers=client._headers(),
            params={
                "jql": jql,
                "startAt": start,
                "maxResults": page_size,
                "fields": fields_csv,
            },
            timeout=60,
            verify=client.verify_ssl,
        )
        resp.raise_for_status()
        data = resp.json()
        page = data.get("issues") or []
        if total_expected is None:
            total_expected = data.get("total", 0)
            if log:
                print(f"    fetch_project_tree: server reports total = {total_expected} issue(s)", file=log)
        issues.extend(page)
        if log:
            print(
                f"    fetch_project_tree: page startAt={start}: got {len(page)} "
                f"(running {len(issues)}/{total_expected})",
                file=log,
            )
        if not page or len(issues) >= total_expected:
            break
        start += len(page)

    elapsed = time.monotonic() - started

    # Group locally.
    by_key: dict[str, dict] = {}
    epics: list[dict] = []
    for raw in issues:
        norm = _normalize_min(raw, epic_link_id)
        by_key[norm["key"]] = norm
        if norm["issue_type"] == "Epic":
            norm["children"] = []
            epics.append(norm)

    orphan_count = 0
    for norm in by_key.values():
        if norm["issue_type"] == "Epic":
            continue
        ek = norm.get("epic_link")
        if ek and ek in by_key and by_key[ek]["issue_type"] == "Epic":
            by_key[ek]["children"].append(norm)
        else:
            orphan_count += 1

    total_children = sum(len(e["children"]) for e in epics)
    if log:
        print(
            f"    fetch_project_tree: {len(epics)} epics + {total_children} children "
            f"in {elapsed:.1f}s (orphans={orphan_count})",
            file=log,
        )

    return {
        "project_key": project_key,
        "epic_count": len(epics),
        "child_count": total_children,
        "orphan_count": orphan_count,
        "epics": epics,
    }
