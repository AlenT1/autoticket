"""Re-export shim — the canonical home for these symbols is now
``_shared/io/sources/{gdrive,local_folder}.py``.

Drive's existing pipeline still imports from here (``DriveFile``,
``list_folder``, ``download_file``, ``list_local_folder``, etc.) so this
shim keeps that working without touching every call site. Step 12 / stage 3
will migrate consumers to the canonical location.
"""
from __future__ import annotations

from _shared.io.sources.gdrive import (  # noqa: F401  (re-export)
    GOOGLE_EXPORT_MIME,
    SCOPES,
    DriveFile,
    GDriveSource,
    build_service,
    download_file,
    list_folder,
)
from _shared.io.sources.local_folder import (  # noqa: F401  (re-export)
    LocalFolderSource,
    list_local_folder,
)

__all__ = [
    "DriveFile",
    "GDriveSource",
    "LocalFolderSource",
    "SCOPES",
    "GOOGLE_EXPORT_MIME",
    "build_service",
    "list_folder",
    "download_file",
    "list_local_folder",
]
