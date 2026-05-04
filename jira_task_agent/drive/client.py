from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

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
    """List non-trashed files inside `folder_id`.

    If `modified_after` is given (must be timezone-aware), only files whose
    `modifiedTime` is strictly greater are returned.
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
    """Download `file` into `dest_dir`. Native Google types are exported.

    Files are saved as `<id>__<sanitized-name>` to avoid collisions on
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


_LOCAL_TEXTUAL_SUFFIXES = (".md", ".html", ".txt")


def list_local_folder(
    local_dir: Path,
) -> tuple[list[DriveFile], dict[str, Path]]:
    """Scan `local_dir` for textual planning docs and return them as
    `DriveFile`s plus a `{file_id: Path}` map. The returned shape mirrors
    `list_folder` + `download_file` so the runner can treat local files
    identically to Drive files.

    File ids are `local::<filename>` so they cannot collide with Drive's
    opaque ids and have a stable cache key across runs.
    """
    if not local_dir.exists():
        return [], {}
    files: list[DriveFile] = []
    paths: dict[str, Path] = {}
    user = os.environ.get("USER") or "local"
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
