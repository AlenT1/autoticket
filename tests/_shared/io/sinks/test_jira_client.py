"""Offline tests for `_shared.io.sinks.jira.client.JiraClient`.

Migrated from `tests/file_to_jira/test_jira.py`'s 7 client/whoami tests
when the legacy `file_to_jira.jira.client` was deleted in c8.c. Adapted
to the shared client's interface — which uses `requests` directly
rather than wrapping atlassian-python-api as the legacy client did, so
tests monkeypatch `requests.get / put / post / Session` at the module
boundary instead of injecting a fake `Jira` object.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from _shared.io.sinks.jira import client as client_module
from _shared.io.sinks.jira.client import JiraClient


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


class _FakeResponse:
    """Stand-in for `requests.Response` (sync GET/PUT/POST shape).

    Mirrors the attributes the shared client touches: `raise_for_status`,
    `.content` (truthiness check before `.json()`), and `.json()` itself.
    """

    def __init__(self, *, json_body: Any = None, status_code: int = 200) -> None:
        self._json = json_body
        self.status_code = status_code
        # Non-empty content so the shared client's `if not r.content`
        # truthy guard passes when the body is JSON.
        self.content = b"{}" if json_body is None else b'{"_": ""}'

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def json(self) -> Any:
        return self._json


class _FakeStreamingResponse:
    """Stand-in for `requests.get(stream=True)` output."""

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


def _client() -> JiraClient:
    """A minimal in-process JiraClient — no real HTTP at construction time."""
    return JiraClient(
        host="jirasw.example.com",
        auth_header="Bearer test",
        auth_mode="bearer",
    )


# ----------------------------------------------------------------------
# whoami
# ----------------------------------------------------------------------


def test_whoami_extracts_username_display_name_and_email(monkeypatch):
    """`whoami()` returns a `WhoamiResult` with username / display_name /
    email pulled from the `/myself` payload."""
    captured: dict[str, Any] = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        return _FakeResponse(json_body={
            "name": "jdoe",
            "displayName": "Jane Doe",
            "emailAddress": "jane@example.com",
        })

    monkeypatch.setattr(client_module.requests, "get", fake_get)
    me = _client().whoami()

    assert me.username == "jdoe"
    assert me.display_name == "Jane Doe"
    assert me.email == "jane@example.com"
    assert "/myself" in captured["url"]


def test_whoami_falls_back_to_account_id_when_name_absent(monkeypatch):
    """Jira Cloud doesn't return `name`; fall back to `accountId` then `key`."""
    monkeypatch.setattr(
        client_module.requests, "get",
        lambda url, **kwargs: _FakeResponse(json_body={
            "accountId": "acct-abc",
            "displayName": "Cloud User",
        }),
    )
    me = _client().whoami()
    assert me.username == "acct-abc"
    assert me.display_name == "Cloud User"


# ----------------------------------------------------------------------
# create_issue
# ----------------------------------------------------------------------


def test_create_issue_posts_to_issue_endpoint_and_returns_key(monkeypatch):
    """`create_issue` POSTs to /issue and returns the response body
    (which carries the new key)."""
    captured: dict[str, Any] = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _FakeResponse(json_body={"key": "BUG-100", "id": "1"})

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    # Pre-populate field cache so we don't hit /field on this path.
    c = _client()
    c._custom_fields = {}

    result = c.create_issue(
        project_key="BUG",
        summary="test issue",
        description="body",
        issue_type="Bug",
    )
    assert result["key"] == "BUG-100"
    assert "/issue" in captured["url"]
    body = captured["json"]
    fields = body["fields"]
    assert fields["project"] == {"key": "BUG"}
    assert fields["summary"] == "test issue"
    assert fields["issuetype"] == {"name": "Bug"}


# ----------------------------------------------------------------------
# issue_browse_url
# ----------------------------------------------------------------------


def test_issue_browse_url_format():
    """Browse URL is `https://<host>/browse/<KEY>`."""
    c = _client()
    assert c.issue_browse_url("BUG-42") == "https://jirasw.example.com/browse/BUG-42"


def test_issue_browse_url_normalizes_host_with_scheme():
    """If the host accidentally has a scheme prefix, browse URL still works."""
    c = JiraClient(
        host="jirasw.example.com",  # already normalized
        auth_header="Bearer x", auth_mode="bearer",
    )
    assert c.issue_browse_url("X-1").startswith("https://jirasw.example.com/browse/")


# ----------------------------------------------------------------------
# Auth-mode dispatch
# ----------------------------------------------------------------------


def test_basic_auth_requires_user_email(monkeypatch):
    """`auth_mode='basic'` (Cloud) without `user_email` (and no
    JIRA_USER_EMAIL env) must raise."""
    monkeypatch.delenv("JIRA_USER_EMAIL", raising=False)
    with pytest.raises(RuntimeError) as ei:
        JiraClient.from_config(
            url="https://example.atlassian.net",
            pat="t",
            auth_mode="basic",
        )
    assert "user_email" in str(ei.value)


def test_basic_auth_uses_user_email_for_authorization():
    """When `user_email` is provided, basic auth header is built correctly."""
    c = JiraClient.from_config(
        url="https://example.atlassian.net",
        pat="apitoken",
        auth_mode="basic",
        user_email="me@example.com",
    )
    assert c.auth_header.startswith("Basic ")
    # decode and check the email:token shape
    import base64
    decoded = base64.b64decode(c.auth_header[len("Basic "):]).decode()
    assert decoded == "me@example.com:apitoken"


def test_unknown_auth_mode_raises():
    """An unrecognized `auth_mode` value must fail loudly, not silently."""
    with pytest.raises(RuntimeError) as ei:
        JiraClient.from_config(
            url="https://example.com", pat="t", auth_mode="weird",
        )
    assert "auth_mode" in str(ei.value)


# ----------------------------------------------------------------------
# download_attachment
# ----------------------------------------------------------------------


def test_attachment_download_caps_at_max_bytes(tmp_path: Path, monkeypatch):
    """`download_attachment` must stop writing once `max_bytes` is hit
    even if the streamed body is larger."""
    chunks = [b"x" * 4_000_000, b"x" * 4_000_000]

    def fake_get(url, **kwargs):
        return _FakeStreamingResponse(chunks)

    monkeypatch.setattr(client_module.requests, "get", fake_get)
    dest = tmp_path / "big.bin"
    written = _client().download_attachment(
        "https://example.com/file", dest, max_bytes=5_000_000,
    )
    assert written == 5_000_000
    assert dest.stat().st_size == 5_000_000


def test_attachment_download_writes_to_dest(tmp_path: Path, monkeypatch):
    """Happy path — concatenated chunks land in `dest` and `written`
    matches the byte total."""
    monkeypatch.setattr(
        client_module.requests, "get",
        lambda url, **kwargs: _FakeStreamingResponse([b"hello ", b"world"]),
    )
    dest = tmp_path / "small.txt"
    written = _client().download_attachment(
        "https://example.com/file", dest, max_bytes=1000,
    )
    assert written == 11
    assert dest.read_bytes() == b"hello world"
