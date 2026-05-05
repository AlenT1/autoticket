"""Tests for the Jira upload pipeline (client wrapper, field map, user resolver, payload builder)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from file_to_jira.config import (
    AppConfig,
    JiraConfig,
)
from file_to_jira.jira.client import JiraClient
from file_to_jira.jira.field_map import (
    FieldInfo,
    build_field_map,
    discover_create_meta,
)
from file_to_jira.jira.uploader import build_issue_payload
from file_to_jira.jira.user_resolver import UserResolver
from file_to_jira.models import (
    BugRecord,
    BugStage,
    EnrichedBug,
    EnrichmentMeta,
    ModuleContext,
    ParsedBug,
)


# ---------------------------------------------------------------------------
# Fake atlassian Jira client
# ---------------------------------------------------------------------------


class FakeAtlassianJira:
    """Stub of ``atlassian.Jira`` for tests."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.searches: list[str] = []
        self._next_key = 100
        self.users_by_name: dict[str, list[dict[str, Any]]] = {}
        self.createmeta_response: dict[str, Any] = {}
        self.myself_response: dict[str, Any] = {}
        self.search_response: dict[str, Any] = {"issues": []}

    def myself(self) -> dict[str, Any]:
        return self.myself_response

    def get_server_info(self) -> dict[str, Any]:
        return {"version": "9.0.0"}

    def issue_createmeta(self, project: str, expand: str) -> dict[str, Any]:
        return self.createmeta_response

    def jql(self, jql: str, fields: str = "", limit: int = 50) -> dict[str, Any]:
        self.searches.append(jql)
        return self.search_response

    def user_find_by_user_string(self, query: str, start: int = 0, limit: int = 5):
        return self.users_by_name.get(query, [])

    def issue_create(self, fields: dict[str, Any]) -> dict[str, Any]:
        self.created.append(fields)
        key = f"BUG-{self._next_key}"
        self._next_key += 1
        return {"key": key}


@pytest.fixture
def fake_atlassian() -> FakeAtlassianJira:
    return FakeAtlassianJira()


@pytest.fixture
def client(fake_atlassian: FakeAtlassianJira) -> JiraClient:
    return JiraClient("https://jirasw.example.com", client=fake_atlassian)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def test_whoami_extracts_username(client: JiraClient, fake_atlassian) -> None:
    fake_atlassian.myself_response = {
        "name": "jdoe",
        "displayName": "Jane Doe",
        "emailAddress": "jane@example.com",
    }
    me = client.whoami()
    assert me.username == "jdoe"
    assert me.display_name == "Jane Doe"
    assert me.email == "jane@example.com"


def test_create_issue_routes_through_atlassian(client: JiraClient, fake_atlassian) -> None:
    result = client.create_issue({"summary": "test"})
    assert result["key"] == "BUG-100"
    assert fake_atlassian.created == [{"summary": "test"}]


def test_browse_url_format(client: JiraClient) -> None:
    assert client.issue_browse_url("BUG-42") == "https://jirasw.example.com/browse/BUG-42"


# ---------------------------------------------------------------------------
# Auth-mode dispatch (Server/DC bearer vs Cloud basic)
# ---------------------------------------------------------------------------


def test_basic_auth_requires_user_email() -> None:
    """auth_mode='basic' (Cloud) without user_email must raise."""
    from file_to_jira.jira.client import JiraError

    with pytest.raises(JiraError) as ei:
        JiraClient("https://example.atlassian.net", pat="t", auth_mode="basic")
    assert "user_email" in str(ei.value)


def test_unknown_auth_mode_raises() -> None:
    from file_to_jira.jira.client import JiraError

    with pytest.raises(JiraError) as ei:
        JiraClient("https://example.com", pat="t", auth_mode="weird")
    assert "auth_mode" in str(ei.value)


class _FakeStreamingResponse:
    """Stand-in for ``requests.Response`` returned by session.get(stream=True)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def __enter__(self) -> "_FakeStreamingResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 65536):
        yield from self._chunks


class _FakeSession:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.headers: dict[str, str] = {}

    def get(self, url: str, **kwargs) -> _FakeStreamingResponse:
        return _FakeStreamingResponse(self._chunks)


def test_attachment_download_caps_at_max_bytes(
    tmp_path, client: JiraClient, fake_atlassian
) -> None:
    """download_attachment must stop writing once max_bytes is hit."""
    fake_atlassian._session = _FakeSession([b"x" * 4_000_000, b"x" * 4_000_000])
    dest = tmp_path / "big.bin"
    written = client.download_attachment(
        "https://example.com/file", dest, max_bytes=5_000_000
    )
    assert written == 5_000_000
    assert dest.stat().st_size == 5_000_000


def test_attachment_download_writes_to_dest(
    tmp_path, client: JiraClient, fake_atlassian
) -> None:
    fake_atlassian._session = _FakeSession([b"hello ", b"world"])
    dest = tmp_path / "small.txt"
    written = client.download_attachment(
        "https://example.com/file", dest, max_bytes=1000
    )
    assert written == 11
    assert dest.read_bytes() == b"hello world"


# ---------------------------------------------------------------------------
# Field map
# ---------------------------------------------------------------------------


def _createmeta_for(project: str, issue_type: str) -> dict[str, Any]:
    return {
        "projects": [
            {
                "key": project,
                "issuetypes": [
                    {
                        "name": issue_type,
                        "fields": {
                            "summary": {
                                "name": "Summary",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                            "description": {
                                "name": "Description",
                                "required": False,
                                "schema": {"type": "string"},
                            },
                            "priority": {
                                "name": "Priority",
                                "required": False,
                                "schema": {"type": "priority"},
                                "allowedValues": [
                                    {"name": "Highest"},
                                    {"name": "High"},
                                    {"name": "Medium"},
                                    {"name": "Low"},
                                ],
                            },
                            "customfield_12345": {
                                "name": "Severity",
                                "required": False,
                                "schema": {"type": "option"},
                            },
                        },
                    },
                ],
            },
        ],
    }


def test_discover_create_meta(client: JiraClient, fake_atlassian) -> None:
    fake_atlassian.createmeta_response = _createmeta_for("BUG", "Bug")
    fields = discover_create_meta(client, "BUG", "Bug")
    assert "summary" in fields
    assert "severity" in fields
    assert fields["severity"].field_id == "customfield_12345"
    assert fields["priority"].allowed_values == ["Highest", "High", "Medium", "Low"]


def test_build_field_map_marks_unknown_fields(client: JiraClient, fake_atlassian) -> None:
    fake_atlassian.createmeta_response = _createmeta_for("BUG", "Bug")
    fields = discover_create_meta(client, "BUG", "Bug")
    fm = build_field_map(
        "BUG",
        "Bug",
        {
            "summary": "summary",
            "severity": "customfield_12345",
            "ghost_field": "customfield_99999",
        },
        fields,
    )
    assert fm.by_logical_name == {
        "summary": "summary",
        "severity": "customfield_12345",
    }
    assert fm.missing == ["ghost_field=customfield_99999"]


# ---------------------------------------------------------------------------
# User resolver
# ---------------------------------------------------------------------------


def test_user_resolver_loads_existing_yaml(tmp_path: Path) -> None:
    p = tmp_path / "users.yaml"
    p.write_text('"Jane Doe": jdoe\n', encoding="utf-8")
    resolver = UserResolver(client=None, user_map_path=p)
    res = resolver.resolve("Jane Doe")
    assert res.username == "jdoe"
    assert res.confidence == "exact"


def test_user_resolver_searches_jira_and_caches(
    tmp_path: Path, client: JiraClient, fake_atlassian
) -> None:
    fake_atlassian.users_by_name["Jane Doe"] = [
        {"name": "jdoe", "displayName": "Jane Doe"}
    ]
    p = tmp_path / "users.yaml"
    resolver = UserResolver(client, p)
    res = resolver.resolve("Jane Doe")
    assert res.username == "jdoe"
    assert res.confidence == "search"
    resolver.save()
    text = p.read_text(encoding="utf-8")
    assert "Jane Doe" in text
    assert "jdoe" in text


def test_user_resolver_unknown_default_policy(tmp_path: Path) -> None:
    resolver = UserResolver(
        client=None,
        user_map_path=tmp_path / "u.yaml",
        unknown_policy="default",
        default_assignee="triage",
    )
    res = resolver.resolve("Mystery Person")
    assert res.username == "triage"
    assert res.confidence == "default"


def test_user_resolver_unknown_skip_policy(tmp_path: Path) -> None:
    resolver = UserResolver(
        client=None,
        user_map_path=tmp_path / "u.yaml",
        unknown_policy="skip",
    )
    res = resolver.resolve("Mystery Person")
    assert res.username is None
    assert res.confidence == "skip"


def test_user_resolver_unknown_fail_policy(tmp_path: Path) -> None:
    resolver = UserResolver(
        client=None,
        user_map_path=tmp_path / "u.yaml",
        unknown_policy="fail",
    )
    with pytest.raises(KeyError):
        resolver.resolve("Mystery Person")


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------


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


def test_payload_includes_summary_priority_labels(tmp_path: Path) -> None:
    cfg = _make_cfg()
    fm = build_field_map(
        "BUG", "Bug",
        {"summary": "summary", "description": "description", "priority": "priority"},
        {
            "summary": FieldInfo("summary", "Summary", True, "string", None),
            "description": FieldInfo("description", "Description", False, "string", None),
            "priority": FieldInfo("priority", "Priority", False, "priority", None),
        },
    )
    resolver = UserResolver(client=None, user_map_path=tmp_path / "u.yaml")
    record = _make_record()
    payload = build_issue_payload(record, cfg, fm, resolver, label="f2j-id:abc")
    fields = payload["fields"]
    assert fields["project"] == {"key": "BUG"}
    assert fields["issuetype"] == {"name": "Bug"}
    assert fields["summary"] == "Multi-domain composer fails to emit per-skill events"
    assert fields["priority"] == {"name": "Highest"}
    assert "f2j-id:abc" in fields["labels"]
    assert "from-md" in fields["labels"]
    assert "upstream:CORE-CHAT-026" in fields["labels"]
    assert {"name": "Core"} in fields["components"]


def test_payload_truncates_oversize_description(tmp_path: Path) -> None:
    cfg = _make_cfg()
    fm = build_field_map("BUG", "Bug", {}, {})
    resolver = UserResolver(client=None, user_map_path=tmp_path / "u.yaml")
    record = _make_record()
    record.enriched.description_md = "x" * 50_000
    payload = build_issue_payload(record, cfg, fm, resolver, label="f2j-id:abc")
    desc = payload["fields"]["description"]
    assert len(desc) < 50_000
    assert "truncated" in desc


def test_payload_includes_external_id_field_when_configured(tmp_path: Path) -> None:
    cfg = _make_cfg(external_id_field="customfield_99999")
    fm = build_field_map("BUG", "Bug", {}, {})
    resolver = UserResolver(client=None, user_map_path=tmp_path / "u.yaml")
    record = _make_record(external_id="CORE-CHAT-026")
    payload = build_issue_payload(record, cfg, fm, resolver, label="f2j-id:abc")
    assert payload["fields"]["customfield_99999"] == "CORE-CHAT-026"


def test_payload_assignee_routed_by_module(tmp_path: Path) -> None:
    """module_to_assignee[_core] -> 'Yair Sadan' -> resolved via user_map.yaml."""
    cfg = _make_cfg()
    cfg.jira.module_to_assignee = {"_core": "Yair Sadan"}
    cfg.jira.default_assignee = "Guy Keinan"
    fm = build_field_map("BUG", "Bug", {}, {})
    user_map = tmp_path / "u.yaml"
    user_map.write_text(
        '"Yair Sadan": ysadan\n"Guy Keinan": gkeinan\n', encoding="utf-8"
    )
    resolver = UserResolver(client=None, user_map_path=user_map)
    # _make_record produces a bug whose inherited_module.repo_alias is "_core".
    record = _make_record()
    assert record.parsed.inherited_module.repo_alias == "_core"
    payload = build_issue_payload(record, cfg, fm, resolver, label="f2j-id:abc")
    assert payload["fields"]["assignee"] == {"name": "ysadan"}


def test_payload_assignee_falls_through_to_default(tmp_path: Path) -> None:
    """A bug whose module isn't in module_to_assignee picks up default_assignee."""
    cfg = _make_cfg()
    cfg.jira.module_to_assignee = {"_core": "Yair Sadan"}
    cfg.jira.default_assignee = "Guy Keinan"
    fm = build_field_map("BUG", "Bug", {}, {})
    user_map = tmp_path / "u.yaml"
    user_map.write_text('"Guy Keinan": gkeinan\n', encoding="utf-8")
    resolver = UserResolver(client=None, user_map_path=user_map)
    record = _make_record()
    # Override the module to one NOT in module_to_assignee.
    record.parsed.inherited_module = ModuleContext(
        repo_alias="vibe_coding/centarb", branch="main"
    )
    payload = build_issue_payload(record, cfg, fm, resolver, label="f2j-id:abc")
    assert payload["fields"]["assignee"] == {"name": "gkeinan"}


def test_payload_explicit_hint_wins_over_module(tmp_path: Path) -> None:
    """ParsedBug.hinted_assignee from the markdown overrides module routing."""
    cfg = _make_cfg()
    cfg.jira.module_to_assignee = {"_core": "Yair Sadan"}
    fm = build_field_map("BUG", "Bug", {}, {})
    user_map = tmp_path / "u.yaml"
    user_map.write_text('"Eli Cohen": ecohen\n', encoding="utf-8")
    resolver = UserResolver(client=None, user_map_path=user_map)
    record = _make_record()
    record.parsed.hinted_assignee = "Eli Cohen"  # explicitly named in markdown
    payload = build_issue_payload(record, cfg, fm, resolver, label="f2j-id:abc")
    assert payload["fields"]["assignee"] == {"name": "ecohen"}


def test_payload_assignee_resolved_from_user_map(tmp_path: Path) -> None:
    cfg = _make_cfg()
    fm = build_field_map("BUG", "Bug", {}, {})
    user_map = tmp_path / "u.yaml"
    user_map.write_text('"Jane Doe": jdoe\n', encoding="utf-8")
    resolver = UserResolver(client=None, user_map_path=user_map)
    record = _make_record()
    record.enriched.assignee_hint = "Jane Doe"
    payload = build_issue_payload(record, cfg, fm, resolver, label="f2j-id:abc")
    assert payload["fields"]["assignee"] == {"name": "jdoe"}
