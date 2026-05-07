"""Cooperative inter-process lock for scheduled runs.

A single agent run mutates ``data/cache.json`` and ``data/state.json``.
If a cron / systemd timer fires the next run while the previous one is
still in flight (slow LLM, large folder), the two processes collide and
either truncate the cache or persist an incoherent cursor.

:func:`acquire_run_lock` wraps a critical section with a non-blocking
``fcntl.flock``. The second invocation exits cleanly with
:class:`RunLockBusy` rather than waiting or crashing.

POSIX-only (uses ``fcntl``). Windows is not a target deployment for
this agent today.
"""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class RunLockBusy(RuntimeError):
    """Raised when another run already holds the lock."""


@contextmanager
def acquire_run_lock(lock_path: Path | str) -> Iterator[Path]:
    """Acquire an exclusive non-blocking flock on ``lock_path``.

    Creates parent dirs as needed. Stores the locking PID in the lock
    file for diagnostic logging — do NOT trust it for cleanup
    decisions; the kernel releases the flock when the holder exits.

    Args:
        lock_path: Filesystem path for the lock sentinel.

    Yields:
        The :class:`Path` of the lock file (resolved).

    Raises:
        RunLockBusy: another process holds the lock.
    """
    p = Path(lock_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    f = p.open("a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        try:
            f.seek(0)
            holder = f.read().strip() or "unknown"
        except Exception:
            holder = "unknown"
        f.close()
        raise RunLockBusy(
            f"another run holds the lock at {p} (pid={holder}). "
            f"Wait for it to finish, or remove the lock file if "
            f"you're sure the previous run died."
        ) from e
    # Lock acquired — guarantee unlock + close on exit, even if the
    # body raises.
    try:
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        yield p
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()
