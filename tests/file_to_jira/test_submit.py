"""Tests for submit_enrichment (schema validation + file-existence checks)."""

from __future__ import annotations

from pathlib import Path

import pytest

from file_to_jira.enrich.tools import build_submit_tool


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("# real file\n", encoding="utf-8")
    return root


@pytest.fixture
def submit(fixture_repo: Path):
    return build_submit_tool({"sample": fixture_repo})


def _valid_payload(**overrides):
    base = {
        "bug_id": "abc123",
        "summary": "Test summary",
        "description_md": "Test description.",
        "priority": "P1",
        "code_references": [],
        "enrichment_meta": {
            "model": "claude-sonnet-4-6",
            "started_at": "2026-05-03T12:00:00+00:00",
            "finished_at": "2026-05-03T12:01:00+00:00",
        },
    }
    base.update(overrides)
    return base


def test_submit_accepts_valid_payload(submit) -> None:
    out = submit(_valid_payload())
    assert out["ok"] is True
    assert out["enriched"]["summary"] == "Test summary"


def test_submit_rejects_missing_required_field(submit) -> None:
    payload = _valid_payload()
    del payload["summary"]
    out = submit(payload)
    assert out["ok"] is False
    assert any("summary" in e for e in out["errors"])


def test_submit_rejects_oversize_summary(submit) -> None:
    payload = _valid_payload(summary="x" * 256)
    out = submit(payload)
    assert out["ok"] is False
    assert any("summary" in e for e in out["errors"])


def test_submit_validates_existing_code_reference(submit) -> None:
    payload = _valid_payload(
        code_references=[
            {"repo_alias": "sample", "file_path": "src/main.py", "line_start": 1}
        ]
    )
    out = submit(payload)
    assert out["ok"] is True


def test_submit_rejects_missing_file(submit) -> None:
    payload = _valid_payload(
        code_references=[
            {"repo_alias": "sample", "file_path": "src/does_not_exist.py"}
        ]
    )
    out = submit(payload)
    assert out["ok"] is False
    assert any("does_not_exist" in e for e in out["errors"])


def test_submit_rejects_unknown_repo_alias(submit) -> None:
    payload = _valid_payload(
        code_references=[
            {"repo_alias": "ghost-repo", "file_path": "src/main.py"}
        ]
    )
    out = submit(payload)
    assert out["ok"] is False
    assert any("ghost-repo" in e for e in out["errors"])


def test_submit_rejects_path_traversal(submit) -> None:
    payload = _valid_payload(
        code_references=[
            {"repo_alias": "sample", "file_path": "../../../etc/passwd"}
        ]
    )
    out = submit(payload)
    assert out["ok"] is False
    assert any("escapes repo root" in e or "not found" in e for e in out["errors"])
