"""Tests for `file_to_jira.upload_payload.build_issue_payload`.

These cover the payload-shape contracts (project, issuetype, summary,
description truncation, label merging, custom-field projection) ported
from the legacy `tests/file_to_jira/test_jira.py::test_payload_*` tests,
adapted to the new home and the c7 `_shared.io.sinks.jira.FieldMap`.

Assignee-routing tests at the bottom cover the priority chain in
`_resolve_assignee` (enriched.assignee_hint > parsed.hinted_assignee >
module routing > default_assignee) — that's f2j-body logic, not a sink
strategy. The `_MapResolver` stub stands in for any
`AssigneeResolver` (e.g. `PickerWithCacheStrategy` from `_shared`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from _shared.io.sinks.jira import FieldInfo, build_field_map

from file_to_jira.config import AppConfig, JiraConfig
from file_to_jira.models import (
    BugRecord,
    BugStage,
    EnrichedBug,
    EnrichmentMeta,
    ModuleContext,
    ParsedBug,
)
from file_to_jira.upload_payload import build_issue_payload


# ----------------------------------------------------------------------
# Stub resolver — minimal AssigneeResolver impl for offline tests
# ----------------------------------------------------------------------


class _MapResolver:
    """`AssigneeResolver` stub — display name → username via a fixed dict.

    Stand-in for `PickerWithCacheStrategy` in tests that don't need
    YAML cache or tracker-side picker behavior; just deterministic
    name-to-username translation.
    """

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._map = mapping or {}

    def resolve(self, display_name: str) -> str | None:
        return self._map.get(display_name)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _make_record(*, external_id: str = "CORE-CHAT-026") -> BugRecord:
    parsed = ParsedBug(
        bug_id="abc1234567890def",
        external_id=external_id,
        source_line_start=1,
        source_line_end=5,
        raw_title="Multi-domain composer",
        raw_body="symptom",
        hinted_priority="P0",
        labels=["timeout-suspected-stale"],
        inherited_module=ModuleContext(repo_alias="_core", branch="main"),
    )
    enriched = EnrichedBug(
        bug_id=parsed.bug_id,
        summary="Multi-domain composer fails to emit per-skill events",
        description_md="Symptom: ...",
        priority="P0",
        labels=["timeout-suspected-stale"],
        enrichment_meta=EnrichmentMeta(
            model="claude-sonnet-4-6",
            started_at="2026-05-03T12:00:00+00:00",
            finished_at="2026-05-03T12:01:00+00:00",
        ),
    )
    return BugRecord(stage=BugStage.ENRICHED, parsed=parsed, enriched=enriched)


def _make_cfg(**jira_overrides: Any) -> AppConfig:
    cfg = AppConfig()
    base = JiraConfig(
        url="https://jirasw.example.com",
        project_key="BUG",
        issue_type="Bug",
        priority_values={"P0": "Highest", "P1": "High", "P2": "Medium", "P3": "Low"},
        default_labels=["from-md"],
        module_to_component={"_core": "Core"},
    )
    for k, v in jira_overrides.items():
        setattr(base, k, v)
    cfg.jira = base
    return cfg


# ----------------------------------------------------------------------
# Payload shape: project / issuetype / summary / priority / labels / components
# ----------------------------------------------------------------------


def test_payload_includes_summary_priority_labels_and_routed_component(
    tmp_path: Path,
) -> None:
    """The full happy path: one bug record + a populated config produces
    the expected `{fields: {...}}` skeleton.

    Components routing: `_core` → "Core" via `module_to_component`. The
    component is kept because we explicitly mark "Core" as a valid
    project component — matching what real callers do (the f2j
    upload orchestrator passes the project's live component set).
    """
    cfg = _make_cfg()
    fm = build_field_map(
        "BUG", "Bug",
        {"summary": "summary", "description": "description", "priority": "priority"},
        {
            "summary": FieldInfo("summary", "Summary", True, "string", None),
            "description": FieldInfo(
                "description", "Description", False, "string", None,
            ),
            "priority": FieldInfo("priority", "Priority", False, "priority", None),
        },
    )
    resolver = _MapResolver()
    record = _make_record()
    payload = build_issue_payload(
        record, cfg, fm, resolver,
        label="f2j-id:abc",
        valid_components=frozenset({"Core"}),
    )
    fields = payload["fields"]
    assert fields["project"] == {"key": "BUG"}
    assert fields["issuetype"] == {"name": "Bug"}
    assert fields["summary"] == "Multi-domain composer fails to emit per-skill events"
    assert fields["priority"] == {"name": "Highest"}
    assert "f2j-id:abc" in fields["labels"]
    assert "from-md" in fields["labels"]
    assert "upstream:CORE-CHAT-026" in fields["labels"]
    assert {"name": "Core"} in fields["components"]


def test_payload_drops_components_when_not_in_project_valid_set(
    tmp_path: Path,
) -> None:
    """If the resolved component isn't on the project's live list, it's
    silently dropped (with a log line) — agent invented values must not
    crash the upload. This is the behavior that previously made the
    legacy test fragile on CENTPM (which has zero components)."""
    cfg = _make_cfg()
    fm = build_field_map("BUG", "Bug", {}, {})
    resolver = _MapResolver()
    record = _make_record()
    payload = build_issue_payload(
        record, cfg, fm, resolver,
        label="f2j-id:abc",
        valid_components=frozenset(),  # project has no components
    )
    assert "components" not in payload["fields"]


# ----------------------------------------------------------------------
# Payload shape: description truncation
# ----------------------------------------------------------------------


def test_payload_truncates_oversize_description(tmp_path: Path) -> None:
    """Jira DC's description cap is 32,767; we keep a safety margin and
    append a truncation marker so reviewers know the rest is in
    `state.json`."""
    cfg = _make_cfg()
    fm = build_field_map("BUG", "Bug", {}, {})
    resolver = _MapResolver()
    record = _make_record()
    record.enriched.description_md = "x" * 50_000
    payload = build_issue_payload(record, cfg, fm, resolver, label="f2j-id:abc")
    desc = payload["fields"]["description"]
    assert len(desc) < 50_000
    assert "truncated" in desc


# ----------------------------------------------------------------------
# Payload shape: external-id custom field
# ----------------------------------------------------------------------


def test_payload_includes_external_id_field_when_configured(
    tmp_path: Path,
) -> None:
    """`cfg.jira.external_id_field = customfield_NNN` ⇒ the bug's
    external id surfaces as that field's value."""
    cfg = _make_cfg(external_id_field="customfield_99999")
    fm = build_field_map("BUG", "Bug", {}, {})
    resolver = _MapResolver()
    record = _make_record(external_id="CORE-CHAT-026")
    payload = build_issue_payload(record, cfg, fm, resolver, label="f2j-id:abc")
    assert payload["fields"]["customfield_99999"] == "CORE-CHAT-026"


def test_payload_omits_external_id_field_when_not_configured(
    tmp_path: Path,
) -> None:
    """No `external_id_field` configured ⇒ no customfield in the payload."""
    cfg = _make_cfg()
    fm = build_field_map("BUG", "Bug", {}, {})
    resolver = _MapResolver()
    record = _make_record(external_id="CORE-CHAT-026")
    payload = build_issue_payload(record, cfg, fm, resolver, label="f2j-id:abc")
    assert not any(
        k.startswith("customfield_") for k in payload["fields"]
    ), f"unexpected customfields in payload: {list(payload['fields'])}"


# ----------------------------------------------------------------------
# Markdown → wiki conversion (smoke checks for the helper)
# ----------------------------------------------------------------------


def test_markdown_to_jira_wiki_converts_headings_and_bullets():
    from file_to_jira.upload_payload import markdown_to_jira_wiki

    md = "# Title\n\n- item one\n- item two\n\n**bold** and `code`."
    out = markdown_to_jira_wiki(md)
    assert "h1. Title" in out
    assert "* item one" in out
    assert "* item two" in out
    assert "*bold*" in out
    assert "{{code}}" in out


def test_markdown_to_jira_wiki_preserves_fenced_code_blocks():
    from file_to_jira.upload_payload import markdown_to_jira_wiki

    md = "```python\nprint('hi')\n```"
    out = markdown_to_jira_wiki(md)
    # Fence becomes {code:python}/{code}; body is preserved verbatim.
    assert "{code:python}" in out
    assert "print('hi')" in out
    assert "{code}" in out


# ----------------------------------------------------------------------
# Assignee priority chain (f2j-specific routing)
# ----------------------------------------------------------------------


def test_payload_assignee_routed_by_module() -> None:
    """`module_to_assignee[_core] = "Yair Sadan"` is fed to the resolver,
    which translates the display name to a Jira username."""
    cfg = _make_cfg()
    cfg.jira.module_to_assignee = {"_core": "Yair Sadan"}
    cfg.jira.default_assignee = "Guy Keinan"
    fm = build_field_map("BUG", "Bug", {}, {})
    resolver = _MapResolver({"Yair Sadan": "ysadan", "Guy Keinan": "gkeinan"})
    record = _make_record()
    # _make_record() uses repo_alias="_core" — the routed module entry
    # wins over the default.
    assert record.parsed.inherited_module.repo_alias == "_core"
    payload = build_issue_payload(record, cfg, fm, resolver, label="f2j-id:abc")
    assert payload["fields"]["assignee"] == {"name": "ysadan"}


def test_payload_assignee_falls_through_to_default() -> None:
    """A bug whose module isn't in `module_to_assignee` walks the chain
    down to `default_assignee`."""
    cfg = _make_cfg()
    cfg.jira.module_to_assignee = {"_core": "Yair Sadan"}
    cfg.jira.default_assignee = "Guy Keinan"
    fm = build_field_map("BUG", "Bug", {}, {})
    resolver = _MapResolver({"Guy Keinan": "gkeinan"})
    record = _make_record()
    # Override the module to one NOT in module_to_assignee.
    record.parsed.inherited_module = ModuleContext(
        repo_alias="vibe_coding/centarb", branch="main",
    )
    payload = build_issue_payload(record, cfg, fm, resolver, label="f2j-id:abc")
    assert payload["fields"]["assignee"] == {"name": "gkeinan"}


def test_payload_explicit_hint_wins_over_module() -> None:
    """`ParsedBug.hinted_assignee` from the markdown overrides module
    routing — the chain is hint > module > default."""
    cfg = _make_cfg()
    cfg.jira.module_to_assignee = {"_core": "Yair Sadan"}
    fm = build_field_map("BUG", "Bug", {}, {})
    resolver = _MapResolver({
        "Yair Sadan": "ysadan",
        "Lior Eli": "lior",
    })
    record = _make_record()
    # Set the parsed-md hinted assignee — should win over module routing.
    record.parsed.hinted_assignee = "Lior Eli"
    payload = build_issue_payload(record, cfg, fm, resolver, label="f2j-id:abc")
    assert payload["fields"]["assignee"] == {"name": "lior"}


def test_payload_assignee_resolved_via_resolver() -> None:
    """`_resolve_assignee` consults the resolver for every chain candidate;
    a candidate the resolver doesn't know is dropped, and the chain
    continues to the next entry."""
    cfg = _make_cfg()
    cfg.jira.module_to_assignee = {"_core": "Unknown Person"}
    cfg.jira.default_assignee = "Guy Keinan"
    fm = build_field_map("BUG", "Bug", {}, {})
    resolver = _MapResolver({"Guy Keinan": "gkeinan"})  # Unknown Person not mapped
    record = _make_record()
    payload = build_issue_payload(record, cfg, fm, resolver, label="f2j-id:abc")
    # Module assignee resolves to None → falls through to default.
    assert payload["fields"]["assignee"] == {"name": "gkeinan"}


def test_payload_assignee_omitted_when_no_chain_resolves() -> None:
    """If every chain candidate resolves to None, the `assignee` field
    is omitted entirely (Jira leaves the ticket unassigned)."""
    cfg = _make_cfg()
    cfg.jira.default_assignee = None
    fm = build_field_map("BUG", "Bug", {}, {})
    resolver = _MapResolver()  # empty mapping
    record = _make_record()
    payload = build_issue_payload(record, cfg, fm, resolver, label="f2j-id:abc")
    assert "assignee" not in payload["fields"]
