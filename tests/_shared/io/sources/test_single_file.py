"""Contract tests for SingleFileSource."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from _shared.io.sources import RawDocument, SingleFileSource


@pytest.fixture
def bug_list_md(tmp_path: Path) -> Path:
    p = tmp_path / "Bugs_For_Dev_Review_2026-05-03.md"
    # write_bytes to avoid platform newline translation (write_text on
    # Windows expands \n → \r\n which throws off content equality + sha)
    p.write_bytes(b"# bug list\n\n- one\n- two\n")
    return p


def test_yields_one_document(bug_list_md: Path) -> None:
    src = SingleFileSource(bug_list_md, author_name="Sharon")
    docs = list(src.iter_documents())
    assert len(docs) == 1
    doc = docs[0]
    assert isinstance(doc, RawDocument)
    assert doc.name == "Bugs_For_Dev_Review_2026-05-03.md"
    assert doc.content == "# bug list\n\n- one\n- two\n"
    assert doc.mtime.tzinfo is not None
    assert doc.metadata["source_kind"] == "single_file"
    assert doc.metadata["last_modifying_user_name"] == "Sharon"


def test_id_is_content_sha(bug_list_md: Path) -> None:
    src = SingleFileSource(bug_list_md)
    [doc] = list(src.iter_documents())
    expected_sha = hashlib.sha256(bug_list_md.read_bytes()).hexdigest()
    assert doc.id == f"file::{expected_sha}"
    assert doc.metadata["content_sha256"] == expected_sha


def test_only_filter_matches_filename(bug_list_md: Path) -> None:
    src = SingleFileSource(bug_list_md)
    matched = list(src.iter_documents(only="Bugs_For_Dev_Review_2026-05-03.md"))
    assert len(matched) == 1
    skipped = list(src.iter_documents(only="some_other_file.md"))
    assert skipped == []


def test_since_filter_skips_older(bug_list_md: Path) -> None:
    src = SingleFileSource(bug_list_md)
    # file's mtime is "now-ish"; a future cursor should suppress it
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert list(src.iter_documents(since=future)) == []
    # a past cursor should still yield it
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert len(list(src.iter_documents(since=past))) == 1


def test_missing_path_yields_nothing(tmp_path: Path) -> None:
    src = SingleFileSource(tmp_path / "does-not-exist.md")
    assert list(src.iter_documents()) == []


def test_default_author_uses_env(monkeypatch, bug_list_md: Path) -> None:
    monkeypatch.setenv("LOCAL_AUTHOR_NAME", "FromEnv")
    src = SingleFileSource(bug_list_md)
    [doc] = list(src.iter_documents())
    assert doc.metadata["last_modifying_user_name"] == "FromEnv"


def test_explicit_author_beats_env(monkeypatch, bug_list_md: Path) -> None:
    monkeypatch.setenv("LOCAL_AUTHOR_NAME", "FromEnv")
    src = SingleFileSource(bug_list_md, author_name="Explicit")
    [doc] = list(src.iter_documents())
    assert doc.metadata["last_modifying_user_name"] == "Explicit"
