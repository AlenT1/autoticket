"""Local-folder source — scan a directory for textual planning docs.

Moved here from ``jira_task_agent/drive/client.py`` (the
``list_local_folder`` function) as part of the merge (stage 2). Exposes
both the legacy function (consumed by drive's existing pipeline through
the back-compat re-export) and a :class:`Source`-conforming class.
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from .base import RawDocument
from .gdrive import DriveFile

_LOCAL_TEXTUAL_SUFFIXES = (".md", ".html", ".txt")


def list_local_folder(
    local_dir: Path,
) -> tuple[list[DriveFile], dict[str, Path]]:
    """Scan ``local_dir`` for textual planning docs and return them as
    :class:`DriveFile`s plus a ``{file_id: Path}`` map.

    The returned shape mirrors :func:`gdrive.list_folder` +
    :func:`gdrive.download_file` so drive's runner can treat local files
    identically to Drive files. File ids are ``local::<filename>`` so they
    can't collide with Drive's opaque ids.
    """
    if not local_dir.exists():
        return [], {}
    files: list[DriveFile] = []
    paths: dict[str, Path] = {}
    user = (
        os.environ.get("LOCAL_AUTHOR_NAME")
        or os.environ.get("USER")
        or "local"
    )
    for p in sorted(local_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() not in _LOCAL_TEXTUAL_SUFFIXES:
            continue
        st = p.stat()
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        ctime = datetime.fromtimestamp(st.st_ctime, tz=timezone.utc)
        mime = "text/markdown" if p.suffix.lower() == ".md" else (
            "text/html" if p.suffix.lower() == ".html" else "text/plain"
        )
        f = DriveFile(
            id=f"local::{p.name}",
            name=p.name,
            mime_type=mime,
            created_time=ctime,
            modified_time=mtime,
            size=st.st_size,
            creator_name=None,
            creator_email=None,
            last_modifying_user_name=user,
            last_modifying_user_email=None,
            parents=[],
            web_view_link=p.resolve().as_uri(),
        )
        files.append(f)
        paths[f.id] = p
    return files, paths


class LocalFolderSource:
    """A :class:`Source` impl backed by a local directory.

    Yields one :class:`RawDocument` per textual file (.md, .html, .txt).
    """

    def __init__(
        self,
        local_dir: Path | str,
        *,
        author_name: str | None = None,
    ) -> None:
        self.local_dir = Path(local_dir)
        self.author_name = author_name or _default_author_name()

    def iter_documents(
        self,
        *,
        since: datetime | None = None,
        only: str | None = None,
    ) -> Iterable[RawDocument]:
        files, paths = list_local_folder(self.local_dir)
        for f in files:
            if only and f.name != only:
                continue
            if since is not None and f.modified_time <= since:
                continue
            local_path = paths[f.id]
            try:
                content = local_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            yield RawDocument(
                id=f"local::{f.name}",
                name=f.name,
                content=content,
                mtime=f.modified_time,
                metadata={
                    "source_kind": "local_folder",
                    "absolute_path": str(local_path.resolve()),
                    "size": f.size,
                    "mime_type": f.mime_type,
                    "last_modifying_user_name": self.author_name,
                    "web_view_link": f.web_view_link,
                },
            )


def _default_author_name() -> str:
    return os.environ.get("LOCAL_AUTHOR_NAME") or os.environ.get("USER") or "local"
