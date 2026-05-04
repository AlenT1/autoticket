"""Custom-field discovery and validation against project createmeta."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import JiraClient


@dataclass
class FieldInfo:
    field_id: str           # e.g. "customfield_12345" or "summary"
    name: str               # human-friendly
    required: bool
    schema_type: str | None
    allowed_values: list[Any] | None


@dataclass
class FieldMap:
    """Resolved mapping for one issue type within one project."""

    project_key: str
    issue_type: str
    by_logical_name: dict[str, str]   # our name → Jira field id
    available: dict[str, FieldInfo]   # name (lowercased) → FieldInfo
    missing: list[str]                # logical names referenced but not on createmeta


def _index_field_info(out: dict[str, FieldInfo], info: FieldInfo) -> None:
    """Index a FieldInfo by both its display name and its field_id (lowercased)."""
    if info.name:
        out[info.name.lower()] = info
    if info.field_id:
        out[info.field_id.lower()] = info


def _global_field_info(entry: dict[str, Any]) -> FieldInfo:
    fid = entry.get("id") or entry.get("key") or ""
    name = entry.get("name", fid)
    schema = (entry.get("schema") or {}).get("type")
    return FieldInfo(
        field_id=fid, name=name, required=False,
        schema_type=schema, allowed_values=None,
    )


def _issue_field_info(
    fid: str, value: Any, names: dict[str, str], schemas: dict[str, dict]
) -> FieldInfo:
    name = names.get(fid, fid)
    schema = (schemas.get(fid) or {}).get("type")
    # Surface priority's current display name as an "allowedValues" hint so
    # operators know what to put in `priority_values` mapping.
    allowed: list[Any] | None = None
    if fid == "priority" and isinstance(value, dict) and value.get("name"):
        allowed = [value["name"]]
    return FieldInfo(
        field_id=fid, name=name, required=False,
        schema_type=schema, allowed_values=allowed,
    )


def discover_fields_from_issue(
    client: JiraClient, issue_key: str
) -> dict[str, FieldInfo]:
    """Discover field IDs by inspecting an existing issue.

    Useful when ``createmeta`` is restricted (NVIDIA jirasw returns "Issue
    Does Not Exist" if the caller lacks Create Issues permission). The trade-off:
    we can see which fields HAVE values on this ticket, but we don't learn
    `required` flags or full `allowedValues` lists. We do get the priority's
    current display value, which is usually enough to seed `priority_values`.

    Combines two API calls:
      GET /rest/api/2/issue/{key}?expand=names,schema   (per-issue snapshot)
      GET /rest/api/2/field                              (global field catalog)
    """
    issue_resp = client._client.get(
        f"rest/api/2/issue/{issue_key}",
        params={"expand": "names,schema,renderedFields"},
    )
    issue_fields = issue_resp.get("fields", {}) or {}
    issue_names = issue_resp.get("names", {}) or {}
    issue_schema = issue_resp.get("schema", {}) or {}

    try:
        global_fields = client._client.get("rest/api/2/field")
    except Exception:  # noqa: BLE001
        global_fields = []
    if not isinstance(global_fields, list):
        global_fields = []

    out: dict[str, FieldInfo] = {}
    # Pass 1 — entries from the global catalog (all customfield IDs visible).
    for entry in global_fields:
        _index_field_info(out, _global_field_info(entry))
    # Pass 2 — overlay per-issue info (priority's current value, schema types).
    for fid, value in issue_fields.items():
        _index_field_info(
            out, _issue_field_info(fid, value, issue_names, issue_schema)
        )
    return out


def _to_field_info(fid: str, fdata: dict[str, Any]) -> FieldInfo:
    """Convert one createmeta field entry into a FieldInfo."""
    allowed = fdata.get("allowedValues")
    allowed_simple = (
        [a.get("name", a.get("value", str(a))) for a in allowed]
        if allowed
        else None
    )
    return FieldInfo(
        field_id=fid,
        name=fdata.get("name", fid),
        required=bool(fdata.get("required")),
        schema_type=(fdata.get("schema") or {}).get("type"),
        allowed_values=allowed_simple,
    )


def discover_create_meta(
    client: JiraClient, project_key: str, issue_type: str
) -> dict[str, FieldInfo]:
    """Discover the field schema for one issue type in a project."""
    raw = client.create_meta(project_key)
    out: dict[str, FieldInfo] = {}
    project = next(
        (p for p in raw.get("projects", []) if p.get("key") == project_key),
        None,
    )
    if project is None:
        return out
    issue_type_data = next(
        (it for it in project.get("issuetypes", []) if it.get("name") == issue_type),
        None,
    )
    if issue_type_data is None:
        return out
    for fid, fdata in (issue_type_data.get("fields") or {}).items():
        _index_field_info(out, _to_field_info(fid, fdata))
    return out


def build_field_map(
    project_key: str,
    issue_type: str,
    user_field_map: dict[str, str],
    available: dict[str, FieldInfo],
) -> FieldMap:
    """Cross-reference a user-supplied logical→id map against discovered fields.

    Logical names that don't exist on the project's createmeta go in
    ``missing``; the caller can decide whether to treat that as fatal.
    """
    missing: list[str] = []
    by_logical: dict[str, str] = {}
    for logical, fid in user_field_map.items():
        # Accept either a human name like "summary" or a literal id like "customfield_NNNNN".
        info = available.get(fid.lower())
        if info is None:
            missing.append(f"{logical}={fid}")
            continue
        by_logical[logical] = info.field_id
    return FieldMap(
        project_key=project_key,
        issue_type=issue_type,
        by_logical_name=by_logical,
        available=available,
        missing=missing,
    )
