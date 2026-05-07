"""Offline tests for the inter-process run lock."""
from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path

import pytest

from _shared.process_lock import RunLockBusy, acquire_run_lock


def test_acquire_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "subdir" / "run.lock"
    with acquire_run_lock(target):
        assert target.exists()
        assert target.parent.is_dir()


def test_acquire_writes_pid(tmp_path: Path) -> None:
    target = tmp_path / "run.lock"
    with acquire_run_lock(target):
        assert target.read_text().strip() == str(os.getpid())


def test_release_after_context_exit(tmp_path: Path) -> None:
    target = tmp_path / "run.lock"
    # First acquire-release pair must succeed; second must succeed too,
    # because the first released its flock at __exit__.
    with acquire_run_lock(target):
        pass
    with acquire_run_lock(target):
        pass


def test_second_acquirer_raises_run_lock_busy(tmp_path: Path) -> None:
    target = tmp_path / "run.lock"
    with acquire_run_lock(target):
        with pytest.raises(RunLockBusy) as exc:
            with acquire_run_lock(target):
                pytest.fail("second acquire should not have succeeded")
        assert "another run holds the lock" in str(exc.value)
        assert str(target) in str(exc.value)


def _hold_lock_in_subproc(lock_path: str, hold_seconds: float) -> None:
    """Helper run in a subprocess: acquire and hold the lock briefly."""
    with acquire_run_lock(Path(lock_path)):
        time.sleep(hold_seconds)


def test_second_acquirer_in_separate_process_raises(tmp_path: Path) -> None:
    """Cross-process verification: the lock blocks across processes,
    not just within one process."""
    target = tmp_path / "run.lock"
    p = multiprocessing.Process(
        target=_hold_lock_in_subproc, args=(str(target), 1.0),
    )
    p.start()
    # Give the subprocess time to acquire
    time.sleep(0.2)
    try:
        with pytest.raises(RunLockBusy):
            with acquire_run_lock(target):
                pytest.fail("acquire should have failed while subproc holds lock")
    finally:
        p.join(timeout=3)
        assert p.exitcode == 0


def test_lock_message_includes_holder_pid(tmp_path: Path) -> None:
    target = tmp_path / "run.lock"
    with acquire_run_lock(target):
        with pytest.raises(RunLockBusy) as exc:
            with acquire_run_lock(target):
                pass
        assert str(os.getpid()) in str(exc.value)
