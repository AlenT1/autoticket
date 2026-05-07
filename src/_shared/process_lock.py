"""Cooperative inter-process lock for scheduled runs.

A single agent run mutates ``data/cache.json`` and ``data/state.json``.
If a cron / systemd timer fires the next run while the previous one is
still in flight (slow LLM, large folder), the two processes collide and
either truncate the cache or persist an incoherent cursor.

:func:`acquire_run_lock` wraps a critical section with a non-blocking
exclusive lock. The second invocation exits cleanly with
:class:`RunLockBusy` rather than waiting or crashing.

POSIX uses ``fcntl.flock`` (whole-file, auto-released on process exit).
Windows uses ``msvcrt.locking`` over a 1-byte range at offset 0 (also
auto-released by the OS on process exit). Both stdlib; no extra deps.
On Windows the lock file stays at a single sentinel byte so the
byte-range lock remains valid for the run's lifetime — the diagnostic
PID written into the lock file on POSIX is omitted on Windows (the
docstring on the original POSIX path already noted the PID is
diagnostic-only and not safe for cleanup decisions).
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import msvcrt  # type: ignore[import-not-found]
else:
    import fcntl  # type: ignore[import-not-found]


class RunLockBusy(RuntimeError):
    """Raised when another run already holds the lock."""


def _try_lock_exclusive_nb(f: Any) -> bool:
    """Try to acquire an exclusive non-blocking lock. Returns True on success."""
    if _IS_WINDOWS:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(f: Any) -> None:
    if _IS_WINDOWS:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def acquire_run_lock(lock_path: Path | str) -> Iterator[Path]:
    """Acquire an exclusive non-blocking lock on ``lock_path``.

    Creates parent dirs as needed. On POSIX, stores the locking PID in
    the lock file for diagnostic logging — do NOT trust it for cleanup
    decisions; the OS releases the lock when the holder exits.

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
    # Windows msvcrt.locking needs a real byte at offset 0 to bind the
    # lock to. Plant a sentinel byte if the file is empty.
    f.seek(0, os.SEEK_END)
    if f.tell() == 0:
        f.write("\0")
        f.flush()
    if not _try_lock_exclusive_nb(f):
        try:
            f.seek(0)
            holder = f.read().strip().lstrip("\x00") or "unknown"
        except Exception:
            holder = "unknown"
        f.close()
        raise RunLockBusy(
            f"another run holds the lock at {p} (pid={holder}). "
            f"Wait for it to finish, or remove the lock file if "
            f"you're sure the previous run died."
        )
    # Lock acquired — guarantee unlock + close on exit, even if the
    # body raises.
    try:
        if not _IS_WINDOWS:
            # POSIX flock is whole-file; truncating + rewriting is safe.
            f.seek(0)
            f.truncate()
            f.write(str(os.getpid()))
            f.flush()
        # On Windows, leave the sentinel byte at offset 0 in place so
        # the byte-range lock stays valid for the run's lifetime. The
        # PID-in-lockfile diagnostic is omitted on this platform.
        yield p
    finally:
        _unlock(f)
        f.close()
