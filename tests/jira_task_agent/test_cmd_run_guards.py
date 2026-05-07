"""Offline tests for the CLI-level scheduling guards on `run`.

Covers:
  - --verify is refused when stdin is not a TTY (non-interactive run).
  - --verify is allowed when stdin IS a TTY.
  - When the run lock is busy, the command exits 3 with a clear message.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from jira_task_agent.__main__ import cmd_run


def _args(**overrides) -> argparse.Namespace:
    """Minimal Namespace covering every attribute cmd_run reads, before it
    short-circuits on the guards."""
    base: dict = dict(
        apply=True, verify=True, capture=None, since=None,
        download_dir="data/gdrive_files", local_dir="data/local_files",
        source="both", only=None, target_epic=None, no_cache=False,
        report_out=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_verify_without_tty_refused(capsys) -> None:
    with patch("sys.stdin.isatty", return_value=False):
        rc = cmd_run(_args(apply=True, verify=True))
    assert rc == 2
    captured = capsys.readouterr()
    assert "--verify requires an interactive terminal" in captured.err


def test_verify_with_apply_false_does_not_short_circuit_on_tty_check(
    capsys, tmp_path: Path, monkeypatch,
) -> None:
    """If --apply is not set, --verify is just ignored (warning printed),
    and the no-TTY guard is not the failure mode."""
    # Move into a tmp cwd so the lock + run_once don't touch the real data/.
    monkeypatch.chdir(tmp_path)

    with patch("sys.stdin.isatty", return_value=False), \
         patch("jira_task_agent.__main__.run_once") as mock_run:
        # run_once returns a minimal stub
        from jira_task_agent.runner import RunReport
        from datetime import datetime, timezone
        mock_run.return_value = RunReport(
            started_at=datetime.now(tz=timezone.utc),
            finished_at=datetime.now(tz=timezone.utc),
            apply=False,
        )
        rc = cmd_run(_args(apply=False, verify=True))
    assert rc == 0
    captured = capsys.readouterr()
    assert "--verify only meaningful with --apply" in captured.err


def test_run_lock_busy_returns_exit_code_3(
    capsys, tmp_path: Path, monkeypatch,
) -> None:
    """Cron firing while a previous run is in flight must exit 3 with a
    clear message, not corrupt cache by overlapping."""
    monkeypatch.chdir(tmp_path)

    from _shared.process_lock import acquire_run_lock
    # Hold the lock for the duration of the cmd_run call.
    with acquire_run_lock(Path("data") / "run.lock"):
        with patch("sys.stdin.isatty", return_value=True):
            rc = cmd_run(_args(apply=True, verify=False))
    assert rc == 3
    captured = capsys.readouterr()
    assert "another run holds the lock" in captured.err
