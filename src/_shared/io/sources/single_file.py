"""SingleFileSource — yield one ``RawDocument`` for a known local file path.

Used by ``file_to_jira`` whose CLI input is a single markdown bug list.
Wraps a path so f2j can consume the unified ``Source`` protocol without
caring how the file got there.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from .base import RawDocument

_UNSAFE_FS_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_id(name: str) -> str:
    cleaned = _UNSAFE_FS_CHARS.sub("_", name).strip().strip(".")
    return cleaned or "untitled"


class SingleFileSource:
    """A :class:`Source` that yields exactly one :class:`RawDocument`.

    Args:
        path: The file to read. Read as UTF-8 text.
        author_name: Optional display name stamped into ``metadata`` as
            ``last_modifying_user_name``. Falls back to ``LOCAL_AUTHOR_NAME``
            env, then ``USER``, then ``"local"``.
    """

    def __init__(self, path: Path | str, *, author_name: str | None = None) -> None:
        self.path = Path(path)
        self.author_name = author_name or _default_author_name()

    def iter_documents(
        self,
        *,
        since: datetime | None = None,
        only: str | None = None,
    ) -> Iterable[RawDocument]:
        if not self.path.exists() or not self.path.is_file():
            return
        if only and self.path.name != only:
            return
        st = self.path.stat()
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        if since is not None and mtime <= since:
            return
        # Hash the raw bytes on disk so the id is independent of platform
        # newline translation (write_text on Windows expands \n → \r\n).
        raw = self.path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        content = raw.decode("utf-8")
        yield RawDocument(
            id=f"file::{sha}",
            name=self.path.name,
            content=content,
            mtime=mtime,
            metadata={
                "source_kind": "single_file",
                "absolute_path": str(self.path.resolve()),
                "size": st.st_size,
                "content_sha256": sha,
                "last_modifying_user_name": self.author_name,
                # for parity with GDrive / local_folder doc shapes
                "web_view_link": self.path.resolve().as_uri(),
                "safe_id": _safe_id(self.path.name),
            },
        )


def _default_author_name() -> str:
    return os.environ.get("LOCAL_AUTHOR_NAME") or os.environ.get("USER") or "local"
