"""Google Drive source — listing, OAuth, file download/export.

Moved here from ``jira_task_agent/drive/client.py`` as part of the merge
(stage 2). The module exposes both:
- The low-level functions (``list_folder``, ``download_file``,
  ``build_service``, ...) that drive's existing pipeline consumes.
- A high-level :class:`GDriveSource` class conforming to the
  :class:`Source` protocol, for new consumers (autodev, future tools).

The legacy ``DriveFile`` dataclass stays here so drive's body keeps
working through the back-compat re-export at
``jira_task_agent/drive/client.py``.
"""
from __future__ import annotations

import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from .base import RawDocument

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Native Google types must be exported, not directly downloaded.
# Maps source mimeType -> (export mimeType, file extension to append).
GOOGLE_EXPORT_MIME = {
    "application/vnd.google-apps.document": ("text/markdown", ".md"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}

_UNSAFE_FS_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass
class DriveFile:
    """Drive's rich metadata shape — used by drive's existing pipeline.

    For consumers using the unified ``Source`` protocol, see
    :class:`GDriveSource` which yields :class:`RawDocument` instead.
    """

    id: str
    name: str
    mime_type: str
    created_time: datetime
    modified_time: datetime
    size: int | None  # None for native Google Docs/Sheets/Slides/Forms
    creator_name: str | None  # populated only on My Drive items, not shared drives
    creator_email: str | None
    last_modifying_user_name: str | None
    last_modifying_user_email: str | None
    parents: list[str]
    web_view_link: str | None


def _load_credentials(
    credentials_path: Path = Path("credentials.json"),
    token_path: Path = Path("token.json"),
) -> Credentials:
    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"OAuth client secrets not found at {credentials_path}. "
                    "Download from Google Cloud Console: APIs & Services -> "
                    "Credentials -> Create Credentials -> OAuth client ID -> "
                    "Desktop app, then save the JSON as 'credentials.json'."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return creds


def _load_credentials_from_env_values(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> Credentials:
    """Build :class:`Credentials` from three discrete OAuth values.

    No JSON file on disk. The ``refresh_token`` is the durable secret;
    access tokens regenerate from it on first use.
    """
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


def build_service(
    credentials_path: Path = Path("credentials.json"),
    token_path: Path = Path("token.json"),
):
    creds = _load_credentials(credentials_path, token_path)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_folder(
    folder_id: str,
    *,
    modified_after: datetime | None = None,
    page_size: int = 100,
    service=None,
) -> list[DriveFile]:
    """List non-trashed files inside ``folder_id``.

    If ``modified_after`` is given (must be timezone-aware), only files
    whose ``modifiedTime`` is strictly greater are returned.
    """
    if service is None:
        service = build_service()

    clauses = [f"'{folder_id}' in parents", "trashed = false"]
    if modified_after is not None:
        if modified_after.tzinfo is None:
            raise ValueError("modified_after must be timezone-aware")
        clauses.append(f"modifiedTime > '{modified_after.isoformat()}'")
    query = " and ".join(clauses)

    fields = (
        "nextPageToken, "
        "files(id, name, mimeType, createdTime, modifiedTime, size, "
        "owners(displayName, emailAddress), "
        "lastModifyingUser(displayName, emailAddress), "
        "parents, webViewLink)"
    )

    out: list[DriveFile] = []
    page_token: str | None = None
    while True:
        resp = (
            service.files()
            .list(
                q=query,
                pageSize=page_size,
                fields=fields,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                orderBy="modifiedTime desc",
            )
            .execute()
        )
        for f in resp.get("files", []):
            owners = f.get("owners") or []
            creator = owners[0] if owners else {}
            last_user = f.get("lastModifyingUser") or {}
            size_str = f.get("size")
            out.append(
                DriveFile(
                    id=f["id"],
                    name=f["name"],
                    mime_type=f["mimeType"],
                    created_time=datetime.fromisoformat(
                        f["createdTime"].replace("Z", "+00:00")
                    ),
                    modified_time=datetime.fromisoformat(
                        f["modifiedTime"].replace("Z", "+00:00")
                    ),
                    size=int(size_str) if size_str is not None else None,
                    creator_name=creator.get("displayName"),
                    creator_email=creator.get("emailAddress"),
                    last_modifying_user_name=last_user.get("displayName"),
                    last_modifying_user_email=last_user.get("emailAddress"),
                    parents=f.get("parents", []),
                    web_view_link=f.get("webViewLink"),
                )
            )
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def _safe_filename(name: str) -> str:
    cleaned = _UNSAFE_FS_CHARS.sub("_", name).strip().strip(".")
    return cleaned or "untitled"


def download_file(
    file: DriveFile,
    dest_dir: Path,
    *,
    service=None,
) -> Path | None:
    """Download ``file`` into ``dest_dir``. Native Google types are exported.

    Files are saved as ``<id>__<sanitized-name>`` to avoid collisions on
    duplicate names. Returns the local path, or None if the file cannot be
    downloaded (e.g. unsupported native type like Forms).
    """
    if service is None:
        service = build_service()

    dest_dir.mkdir(parents=True, exist_ok=True)

    if file.mime_type.startswith("application/vnd.google-apps."):
        mapping = GOOGLE_EXPORT_MIME.get(file.mime_type)
        if mapping is None:
            return None  # e.g. forms, shortcuts — nothing useful to export
        export_mime, ext = mapping
        request = service.files().export_media(
            fileId=file.id, mimeType=export_mime
        )
        name = file.name if file.name.endswith(ext) else f"{file.name}{ext}"
    else:
        request = service.files().get_media(fileId=file.id)
        name = file.name

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    path = dest_dir / f"{file.id}__{_safe_filename(name)}"
    path.write_bytes(buf.getvalue())
    return path


# ---------------------------------------------------------------------------
# Source-protocol-conforming wrapper
# ---------------------------------------------------------------------------

class GDriveSource:
    """A :class:`Source` impl backed by Google Drive.

    Yields :class:`RawDocument`s with text content where exportable. Native
    types that don't export to text (PDF, PNG, Forms, Shortcuts) are skipped.

    Two auth modes:
    - **Env-based** (preferred): pass ``oauth_client_id`` +
      ``oauth_client_secret`` + ``oauth_refresh_token``. No JSON files
      written or read.
    - **File-based** (legacy): pass ``credentials_path`` + ``token_path``.
      The library writes refreshed tokens back to ``token_path``.

    Args:
        folder_id: Drive folder UUID to list.
        download_dir: Local cache dir for downloaded files. Files are
            saved as ``<id>__<sanitized-name>`` so subsequent runs can
            reuse them.
        credentials_path / token_path: file-mode paths. Defaults to CWD.
        oauth_client_id / oauth_client_secret / oauth_refresh_token:
            env-mode OAuth values. When all three are set, file paths
            are ignored.
    """

    def __init__(
        self,
        *,
        folder_id: str,
        download_dir: Path | str = "data/gdrive_files",
        credentials_path: Path | str = "credentials.json",
        token_path: Path | str = "token.json",
        oauth_client_id: str | None = None,
        oauth_client_secret: str | None = None,
        oauth_refresh_token: str | None = None,
    ) -> None:
        self.folder_id = folder_id
        self.download_dir = Path(download_dir)
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)
        self.oauth_client_id = oauth_client_id
        self.oauth_client_secret = oauth_client_secret
        self.oauth_refresh_token = oauth_refresh_token
        self._service = None  # lazy

    @classmethod
    def from_settings(cls, settings: "Any") -> "GDriveSource":
        """Build a source from a unified ``Settings`` object.

        Picks env-mode auth when all three OAuth values are set; otherwise
        falls back to file-mode paths.
        """
        if not settings.drive_folder_id:
            raise RuntimeError(
                "settings.drive_folder_id is empty. Set DRIVE_FOLDER_ID in .env "
                "or YAML."
            )
        return cls(
            folder_id=settings.drive_folder_id,
            download_dir=settings.drive_download_dir,
            credentials_path=settings.drive_credentials_path,
            token_path=settings.drive_token_path,
            oauth_client_id=settings.google_oauth_client_id,
            oauth_client_secret=settings.google_oauth_client_secret,
            oauth_refresh_token=settings.google_oauth_refresh_token,
        )

    def _build_service(self):
        if all((self.oauth_client_id, self.oauth_client_secret, self.oauth_refresh_token)):
            creds = _load_credentials_from_env_values(
                client_id=self.oauth_client_id,
                client_secret=self.oauth_client_secret,
                refresh_token=self.oauth_refresh_token,
            )
            return build("drive", "v3", credentials=creds, cache_discovery=False)
        return build_service(self.credentials_path, self.token_path)

    def iter_documents(
        self,
        *,
        since: datetime | None = None,
        only: str | None = None,
    ) -> Iterable[RawDocument]:
        if self._service is None:
            self._service = self._build_service()
        files = list_folder(
            self.folder_id, modified_after=since, service=self._service
        )
        for f in files:
            if only and f.name != only:
                continue
            local_path = download_file(f, self.download_dir, service=self._service)
            if local_path is None:
                continue  # unsupported native type
            try:
                content = local_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # Skip binary content — out of scope for this layer.
                continue
            yield RawDocument(
                id=f"gdrive::{f.id}",
                name=f.name,
                content=content,
                mtime=f.modified_time,
                metadata={
                    "source_kind": "gdrive",
                    "drive_id": f.id,
                    "mime_type": f.mime_type,
                    "created_time": f.created_time,
                    "size": f.size,
                    "creator_name": f.creator_name,
                    "creator_email": f.creator_email,
                    "last_modifying_user_name": f.last_modifying_user_name,
                    "last_modifying_user_email": f.last_modifying_user_email,
                    "parents": f.parents,
                    "web_view_link": f.web_view_link,
                    "local_path": str(local_path),
                },
            )
