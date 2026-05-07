"""Unit tests for `drive.client.list_local_folder` and the runner's
three-source mode (both / gdrive / local)."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jira_task_agent.drive.client import DriveFile, list_local_folder
from jira_task_agent.runner import run_once


def _write(p: Path, content: str = "# title\n\nbody.\n") -> None:
    p.write_text(content, encoding="utf-8")


def test_empty_or_missing_dir_returns_empty(tmp_path):
    files, paths = list_local_folder(tmp_path / "does-not-exist")
    assert files == [] and paths == {}

    empty = tmp_path / "empty"
    empty.mkdir()
    files, paths = list_local_folder(empty)
    assert files == [] and paths == {}


def test_lists_textual_files_only(tmp_path):
    _write(tmp_path / "doc.md")
    _write(tmp_path / "page.html", "<html>x</html>")
    _write(tmp_path / "notes.txt", "raw notes")
    _write(tmp_path / "image.png", "binary")
    _write(tmp_path / ".hidden", "")

    files, paths = list_local_folder(tmp_path)
    names = sorted(f.name for f in files)
    assert names == ["doc.md", "notes.txt", "page.html"]
    assert all(fid.startswith("local::") for fid in paths)
    assert all(p.exists() for p in paths.values())


def test_drive_file_shape_for_local(tmp_path):
    p = tmp_path / "spec.md"
    _write(p, "# spec\n")

    files, paths = list_local_folder(tmp_path)
    f = files[0]
    assert isinstance(f, DriveFile)
    assert f.id == "local::spec.md"
    assert f.name == "spec.md"
    assert f.mime_type == "text/markdown"
    assert f.size == p.stat().st_size
    assert f.parents == []
    assert f.web_view_link == p.resolve().as_uri()
    assert isinstance(f.modified_time, datetime)
    assert f.modified_time.tzinfo is timezone.utc
    assert paths[f.id] == p


def test_runner_local_only_skips_drive_auth(tmp_path, monkeypatch):
    """`--source local` works without FOLDER_ID or Drive auth."""
    monkeypatch.delenv("FOLDER_ID", raising=False)
    monkeypatch.setenv("JIRA_PROJECT_KEY", "TEST")

    local_dir = tmp_path / "local_files"
    local_dir.mkdir()

    def _bomb_drive(*a, **kw):
        raise AssertionError("Drive should not be called when source=local")

    monkeypatch.setattr("jira_task_agent.runner.build_service", _bomb_drive)
    monkeypatch.setattr("jira_task_agent.runner.list_folder", _bomb_drive)
    monkeypatch.setattr("jira_task_agent.runner.download_file", _bomb_drive)

    report = run_once(
        apply=False,
        source="local",
        local_dir=str(local_dir),
        cache_path=tmp_path / "cache.json",
        state_path=tmp_path / "state.json",
        use_cache=False,
    )
    assert report.errors == []
    assert report.files_total == 0


def test_runner_invalid_source_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_PROJECT_KEY", "TEST")
    with pytest.raises(ValueError, match="invalid source"):
        run_once(
            apply=False,
            source="invalid",
            local_dir=str(tmp_path),
            cache_path=tmp_path / "cache.json",
            state_path=tmp_path / "state.json",
            use_cache=False,
        )


def test_runner_gdrive_requires_folder_id(tmp_path, monkeypatch):
    monkeypatch.delenv("FOLDER_ID", raising=False)
    monkeypatch.delenv("DRIVE_FOLDER_ID", raising=False)
    monkeypatch.setenv("JIRA_PROJECT_KEY", "TEST")
    report = run_once(
        apply=False,
        source="gdrive",
        cache_path=tmp_path / "cache.json",
        state_path=tmp_path / "state.json",
        use_cache=False,
    )
    assert any("DRIVE_FOLDER_ID" in e for e in report.errors)


def test_runner_local_source_classifies_local_files(tmp_path, monkeypatch):
    monkeypatch.delenv("FOLDER_ID", raising=False)
    monkeypatch.setenv("JIRA_PROJECT_KEY", "TEST")

    local_dir = tmp_path / "local_files"
    local_dir.mkdir()
    _write(local_dir / "task_doc.md", "# Task doc\n\n- T1 do thing\n")

    monkeypatch.setattr(
        "jira_task_agent.runner.build_service",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("no Drive in local mode"),
        ),
    )

    def _classify_stub(f, *, local_path, neighbor_names):
        from jira_task_agent.pipeline.classifier import ClassifyResult
        return ClassifyResult(
            file_id=f.id, role="root", confidence=1.0, reason="stub",
        )

    monkeypatch.setattr("jira_task_agent.runner.classify_file", _classify_stub)

    report = run_once(
        apply=False,
        source="local",
        local_dir=str(local_dir),
        cache_path=tmp_path / "cache.json",
        state_path=tmp_path / "state.json",
        use_cache=False,
        since_override=datetime(2000, 1, 1, tzinfo=timezone.utc),
    )
    assert report.errors == []
    assert report.files_total == 1
    assert report.files_classified.get("root") == 1
