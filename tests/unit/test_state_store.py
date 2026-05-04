"""Tests for the atomic state store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from file_to_jira.models import IntermediateFile
from file_to_jira.state import (
    StateFileCorruptError,
    StateStore,
    load_state,
    save_state,
)
from file_to_jira.state.store import _backup_path


def test_save_then_load_roundtrip(
    tmp_path: Path, intermediate_file: IntermediateFile
) -> None:
    p = tmp_path / "state.json"
    save_state(intermediate_file, p)
    assert p.exists()
    restored = load_state(p)
    assert restored.run_id == intermediate_file.run_id
    assert len(restored.bugs) == 1


def test_save_creates_no_backup_on_first_write(
    tmp_path: Path, intermediate_file: IntermediateFile
) -> None:
    p = tmp_path / "state.json"
    save_state(intermediate_file, p)
    assert not _backup_path(p, 0).exists()


def test_save_creates_backup_on_second_write(
    tmp_path: Path, intermediate_file: IntermediateFile
) -> None:
    p = tmp_path / "state.json"
    save_state(intermediate_file, p)
    save_state(intermediate_file, p)
    assert _backup_path(p, 0).exists()


def test_save_rolls_three_backups(
    tmp_path: Path, intermediate_file: IntermediateFile
) -> None:
    p = tmp_path / "state.json"
    # Write 5 times; with keep=3 we should end up with .bak, .bak1, .bak2 (no .bak3).
    for i in range(5):
        intermediate_file.source_file = f"v{i}.md"
        save_state(intermediate_file, p, backup_keep=3)
    assert _backup_path(p, 0).exists()
    assert _backup_path(p, 1).exists()
    assert _backup_path(p, 2).exists()
    assert not _backup_path(p, 3).exists()


def test_save_atomic_no_tmp_left_behind(
    tmp_path: Path, intermediate_file: IntermediateFile
) -> None:
    p = tmp_path / "state.json"
    save_state(intermediate_file, p)
    tmp = p.with_suffix(p.suffix + ".tmp")
    assert not tmp.exists()


def test_load_missing_file_raises_filenotfound(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_state(tmp_path / "nonexistent.json")


def test_load_invalid_json_raises_corrupt(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(StateFileCorruptError) as excinfo:
        load_state(p)
    assert "invalid JSON" in str(excinfo.value)


def test_load_invalid_schema_raises_corrupt(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    # Valid JSON, wrong shape.
    p.write_text(json.dumps({"unexpected": "fields"}), encoding="utf-8")
    with pytest.raises(StateFileCorruptError) as excinfo:
        load_state(p)
    assert "schema validation failed" in str(excinfo.value)


def test_state_store_lock_acquires_and_releases(
    tmp_path: Path, intermediate_file: IntermediateFile
) -> None:
    p = tmp_path / "state.json"
    save_state(intermediate_file, p)
    store = StateStore(p)
    # Successive lock acquisitions in the same process should both succeed.
    with store.lock(timeout=2):
        loaded = store.load()
        assert loaded.run_id == intermediate_file.run_id
    with store.lock(timeout=2):
        pass


def test_state_store_save_updates_timestamp(
    tmp_path: Path, intermediate_file: IntermediateFile
) -> None:
    p = tmp_path / "state.json"
    original_updated = intermediate_file.updated_at
    save_state(intermediate_file, p)
    # Reload from disk; updated_at should be at least as recent as when we created the model.
    restored = load_state(p)
    assert restored.updated_at >= original_updated
