"""Offline tests for `_shared.io.sinks.jira.field_discovery`.

Ported from the legacy `tests/file_to_jira/test_jira.py` field-discovery
tests, adjusted for the new home and the shared client's `client.get(...)`
call shape (the legacy tests used `client._client.get(...)` on the
atlassian-python-api wrapper).
"""
from __future__ import annotations

from typing import Any

from _shared.io.sinks.jira.field_discovery import (
    FieldInfo,
    FieldMap,
    build_field_map,
    discover_create_meta,
    discover_fields_from_issue,
)


# ----------------------------------------------------------------------
# Test fakes
# ----------------------------------------------------------------------


class _FakeClient:
    """Minimal stand-in for `JiraClient` — `field_discovery` only ever
    calls `client.get(...)` and `client.create_meta(...)`."""

    def __init__(
        self,
        get_responses: dict[str, Any] | None = None,
        create_meta_response: dict | None = None,
    ) -> None:
        self._get = get_responses or {}
        self._create_meta = create_meta_response or {}
        self.get_calls: list[tuple[str, dict | None]] = []
        self.create_meta_calls: list[str] = []

    def get(self, path: str, params: dict | None = None) -> Any:
        self.get_calls.append((path, params))
        if path in self._get:
            return self._get[path]
        # Fallback for endpoints with params variations
        for k, v in self._get.items():
            if path.startswith(k):
                return v
        return {}

    def create_meta(self, project_key: str) -> dict:
        self.create_meta_calls.append(project_key)
        return self._create_meta


# ----------------------------------------------------------------------
# discover_create_meta
# ----------------------------------------------------------------------


def test_discover_create_meta_returns_field_info_indexed_by_name_and_id():
    create_meta = {
        "projects": [
            {
                "key": "DEMO",
                "issuetypes": [
                    {
                        "name": "Bug",
                        "fields": {
                            "summary": {
                                "name": "Summary",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                            "customfield_10005": {
                                "name": "Epic Link",
                                "required": False,
                                "schema": {"type": "any"},
                                "allowedValues": [],
                            },
                            "priority": {
                                "name": "Priority",
                                "required": False,
                                "schema": {"type": "priority"},
                                "allowedValues": [
                                    {"name": "P0 - Must have"},
                                    {"name": "P1 - Should have"},
                                ],
                            },
                        },
                    },
                ],
            },
        ],
    }
    client = _FakeClient(create_meta_response=create_meta)
    fields = discover_create_meta(client, "DEMO", "Bug")

    # Indexed by both display name (lowercased) and field_id (lowercased).
    assert "summary" in fields
    assert fields["summary"].field_id == "summary"
    assert fields["summary"].required is True
    assert "epic link" in fields
    assert "customfield_10005" in fields
    assert fields["customfield_10005"].name == "Epic Link"
    # Priority's allowedValues become a simple list of names.
    assert fields["priority"].allowed_values == [
        "P0 - Must have", "P1 - Should have",
    ]
    assert client.create_meta_calls == ["DEMO"]


def test_discover_create_meta_missing_project_returns_empty():
    create_meta = {"projects": []}
    client = _FakeClient(create_meta_response=create_meta)
    fields = discover_create_meta(client, "DEMO", "Bug")
    assert fields == {}


def test_discover_create_meta_missing_issue_type_returns_empty():
    create_meta = {
        "projects": [
            {"key": "DEMO", "issuetypes": [{"name": "Story", "fields": {}}]},
        ],
    }
    client = _FakeClient(create_meta_response=create_meta)
    fields = discover_create_meta(client, "DEMO", "Bug")
    assert fields == {}


# ----------------------------------------------------------------------
# discover_fields_from_issue
# ----------------------------------------------------------------------


def test_discover_fields_from_issue_combines_global_catalog_and_issue_snapshot():
    """Issue-mode discovery merges the global /field catalog with per-issue
    expand=names,schema. Priority's current name surfaces as allowed_values."""
    global_fields_response = [
        {"id": "customfield_99999", "name": "Severity", "schema": {"type": "string"}},
    ]
    issue_response = {
        "fields": {
            "summary": "live summary",
            "priority": {"name": "P1 - Should have"},
        },
        "names": {
            "summary": "Summary",
            "priority": "Priority",
        },
        "schema": {
            "summary": {"type": "string"},
            "priority": {"type": "priority"},
        },
    }
    client = _FakeClient(get_responses={
        "/issue/DEMO-1": issue_response,
        "/field": global_fields_response,
    })
    fields = discover_fields_from_issue(client, "DEMO-1")

    # Global catalog entry survives.
    assert "severity" in fields
    assert fields["severity"].field_id == "customfield_99999"
    # Issue-mode entries are present.
    assert fields["summary"].schema_type == "string"
    # Priority surfaces its current display name as allowed_values.
    assert fields["priority"].allowed_values == ["P1 - Should have"]
    # Issue endpoint is called with expand=names,schema,renderedFields.
    issue_call = next(c for c in client.get_calls if c[0] == "/issue/DEMO-1")
    assert issue_call[1] == {"expand": "names,schema,renderedFields"}


def test_discover_fields_from_issue_tolerates_missing_global_fields_endpoint():
    """If `/field` raises (e.g. 403 on locked-down instances), the discovery
    still returns the per-issue subset — never propagates."""
    issue_response = {
        "fields": {"summary": "x"},
        "names": {"summary": "Summary"},
        "schema": {"summary": {"type": "string"}},
    }

    class _RaisingClient(_FakeClient):
        def get(self, path: str, params: dict | None = None) -> Any:
            if path == "/field":
                raise RuntimeError("403 Forbidden")
            return super().get(path, params)

    client = _RaisingClient(get_responses={"/issue/DEMO-1": issue_response})
    fields = discover_fields_from_issue(client, "DEMO-1")
    # `summary` from the issue snapshot survives even without /field.
    assert "summary" in fields


# ----------------------------------------------------------------------
# build_field_map
# ----------------------------------------------------------------------


def test_build_field_map_resolves_known_logicals_flags_unknown():
    available = {
        "summary": FieldInfo("summary", "Summary", True, "string", None),
        "epic link": FieldInfo("customfield_10005", "Epic Link", False, "any", None),
        "customfield_10005": FieldInfo("customfield_10005", "Epic Link", False, "any", None),
    }
    fm = build_field_map(
        "DEMO", "Bug",
        user_field_map={
            "summary": "summary",
            "epic_link": "Epic Link",
            "made_up_field": "customfield_NONEXISTENT",
        },
        available=available,
    )
    assert isinstance(fm, FieldMap)
    assert fm.project_key == "DEMO"
    assert fm.issue_type == "Bug"
    assert fm.by_logical_name == {
        "summary": "summary",
        "epic_link": "customfield_10005",
    }
    assert fm.missing == ["made_up_field=customfield_NONEXISTENT"]


def test_build_field_map_accepts_id_or_display_name():
    """User-supplied ids can be either the customfield_NNN literal or
    the display name — both should resolve to the same FieldInfo."""
    available = {
        "epic link": FieldInfo("customfield_10005", "Epic Link", False, "any", None),
        "customfield_10005": FieldInfo("customfield_10005", "Epic Link", False, "any", None),
    }
    by_name = build_field_map("DEMO", "Bug", {"x": "Epic Link"}, available)
    by_id = build_field_map("DEMO", "Bug", {"x": "customfield_10005"}, available)
    assert by_name.by_logical_name == by_id.by_logical_name == {
        "x": "customfield_10005",
    }
