"""Atomic state file I/O with rolling backups and OS-level locking."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout
from pydantic import ValidationError

from ..models import IntermediateFile

DEFAULT_BACKUP_KEEP = 3
DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0


class StateFileError(Exception):
    """Base for state-file errors."""


class StateFileLockedError(StateFileError):
    """Raised when another process holds the state-file lock."""


class StateFileCorruptError(StateFileError):
    """Raised when the state file fails to parse or validate."""


def _lock_path(state_path: Path) -> Path:
    return state_path.with_suffix(state_path.suffix + ".lock")


def _backup_path(state_path: Path, n: int) -> Path:
    suffix = ".bak" if n == 0 else f".bak{n}"
    return state_path.with_suffix(state_path.suffix + suffix)


def _rotate_backups(state_path: Path, keep: int) -> None:
    """Rotate backups: drop the oldest, shift the rest up by one, copy current to .bak."""
    if not state_path.exists() or keep <= 0:
        return
    # Drop the oldest.
    _backup_path(state_path, keep - 1).unlink(missing_ok=True)
    # Shift bak{i} -> bak{i+1} for i from keep-2 down to 0.
    for i in range(keep - 2, -1, -1):
        src = _backup_path(state_path, i)
        if src.exists():
            shutil.move(str(src), str(_backup_path(state_path, i + 1)))
    # Snapshot current to .bak (n=0). Copy (not move) so the original remains until atomic replace.
    shutil.copy2(str(state_path), str(_backup_path(state_path, 0)))


@contextmanager
def acquire_state_lock(
    state_path: Path,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """OS-level lock on the state file. Raises StateFileLockedError on timeout."""
    lock = FileLock(str(_lock_path(state_path)), timeout=timeout)
    try:
        lock.acquire()
    except Timeout as e:
        raise StateFileLockedError(
            f"State file {state_path} is locked by another process. "
            f"If no other process is running, delete {_lock_path(state_path)}."
        ) from e
    try:
        yield
    finally:
        lock.release()


def load_state(state_path: Path) -> IntermediateFile:
    """Read and validate state.json. Raises StateFileCorruptError with line-pointed detail."""
    state_path = Path(state_path)
    if not state_path.exists():
        raise FileNotFoundError(f"State file does not exist: {state_path}")
    try:
        with state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise StateFileCorruptError(
            f"{state_path}: invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}"
        ) from e
    try:
        return IntermediateFile.model_validate(data)
    except ValidationError as e:
        raise StateFileCorruptError(f"{state_path}: schema validation failed:\n{e}") from e


def save_state(
    file: IntermediateFile,
    state_path: Path,
    *,
    backup_keep: int = DEFAULT_BACKUP_KEEP,
) -> None:
    """Atomic write with rolling backups. Caller is responsible for holding the file lock."""
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    file.updated_at = datetime.now(timezone.utc).isoformat()

    _rotate_backups(state_path, keep=backup_keep)

    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    payload = file.model_dump_json(indent=2, exclude_none=False)
    with tmp_path.open("w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp_path), str(state_path))


class StateStore:
    """Path + lock + load/save helper. Sync API; async coordination layered in Step 6."""

    def __init__(self, state_path: Path, *, backup_keep: int = DEFAULT_BACKUP_KEEP) -> None:
        self.path = Path(state_path)
        self.backup_keep = backup_keep

    def load(self) -> IntermediateFile:
        return load_state(self.path)

    def save(self, file: IntermediateFile) -> None:
        save_state(file, self.path, backup_keep=self.backup_keep)

    @contextmanager
    def lock(self, timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
        with acquire_state_lock(self.path, timeout=timeout):
            yield
