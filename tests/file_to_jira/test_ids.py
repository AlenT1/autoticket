"""Tests for stable ID generation."""

from __future__ import annotations

import pytest

from file_to_jira.util.ids import compute_bug_id, file_sha256_bytes, file_sha256_path


def test_compute_bug_id_is_deterministic() -> None:
    a = compute_bug_id("abc", "Title", 42)
    b = compute_bug_id("abc", "Title", 42)
    assert a == b


def test_compute_bug_id_changes_with_title() -> None:
    a = compute_bug_id("abc", "Title A", 42)
    b = compute_bug_id("abc", "Title B", 42)
    assert a != b


def test_compute_bug_id_changes_with_line() -> None:
    a = compute_bug_id("abc", "Title", 1)
    b = compute_bug_id("abc", "Title", 2)
    assert a != b


def test_compute_bug_id_changes_with_source_hash() -> None:
    a = compute_bug_id("abc", "Title", 1)
    b = compute_bug_id("xyz", "Title", 1)
    assert a != b


def test_compute_bug_id_length_16() -> None:
    bug_id = compute_bug_id("abc", "Title", 42)
    assert len(bug_id) == 16
    assert all(c in "0123456789abcdef" for c in bug_id)


def test_file_sha256_bytes_known_value() -> None:
    # Known hash of the empty string.
    assert file_sha256_bytes(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_file_sha256_path_matches_bytes(tmp_path) -> None:
    payload = b"hello, file-to-jira-tickets!"
    p = tmp_path / "data.bin"
    p.write_bytes(payload)
    assert file_sha256_path(p) == file_sha256_bytes(payload)
