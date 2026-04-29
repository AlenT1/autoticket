"""List + download files in a Google Drive folder.

Time filters (mutually exclusive):
    --today              files modified today (local time)
    --days N             files modified in the last N days
    --since 2026-04-01   files modified after a specific ISO date/datetime

Examples:
    python list_files.py                              # uses FOLDER_ID, no filter
    python list_files.py --today                      # only today's edits
    python list_files.py --days 7                     # last week
    python list_files.py <folder_id> --since 2026-04-20T12:00
    python list_files.py --today --clean              # wipe dir before download
"""

from __future__ import annotations

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from jira_task_agent.drive.client import build_service, download_file, list_folder


def _parse_since(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _start_of_today_local() -> datetime:
    return datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _resolve_filter(args: argparse.Namespace) -> datetime | None:
    if args.today:
        return _start_of_today_local()
    if args.days is not None:
        return datetime.now().astimezone() - timedelta(days=args.days)
    return args.since


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description="List + download Drive folder.")
    p.add_argument("folder_id", nargs="?", default=os.getenv("FOLDER_ID"))

    when = p.add_mutually_exclusive_group()
    when.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        help="Only files modified after this ISO date/datetime (UTC if no tz).",
    )
    when.add_argument(
        "--today",
        action="store_true",
        help="Only files modified today (local time).",
    )
    when.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help="Only files modified in the last N days.",
    )

    p.add_argument(
        "--out",
        default="data/snapshots/files.json",
        help="Path to write metadata JSON. Default: data/snapshots/files.json",
    )
    p.add_argument(
        "--download-dir",
        default="data/gdrive_files",
        help="Directory to download files into. Default: data/gdrive_files/",
    )
    p.add_argument(
        "--no-download",
        action="store_true",
        help="Skip downloads; only write metadata.",
    )
    p.add_argument(
        "--clean",
        action="store_true",
        help="Remove existing files in --download-dir before downloading.",
    )
    args = p.parse_args(argv)

    if not args.folder_id:
        p.error("folder_id required (positional arg or FOLDER_ID env var)")

    modified_after = _resolve_filter(args)
    if modified_after is not None:
        print(
            f"filter: modifiedTime > {modified_after.isoformat()}",
            file=sys.stderr,
        )

    service = build_service()
    files = list_folder(args.folder_id, modified_after=modified_after, service=service)

    download_root = Path(args.download_dir)
    if args.clean and download_root.exists():
        removed = 0
        for child in download_root.iterdir():
            if child.is_file():
                child.unlink()
                removed += 1
        print(f"cleaned {removed} file(s) from {download_root}/", file=sys.stderr)

    payload = []
    for f in files:
        local_path: str | None = None
        download_status = "skipped"
        if not args.no_download:
            try:
                p_out = download_file(f, download_root, service=service)
                if p_out is None:
                    download_status = "unsupported_type"
                else:
                    local_path = str(p_out)
                    download_status = "ok"
                    print(
                        f"  [{download_status:18s}] {f.name}  ->  {p_out}",
                        file=sys.stderr,
                    )
            except Exception as e:  # noqa: BLE001
                download_status = f"error: {e.__class__.__name__}: {e}"
                print(f"  [error             ] {f.name}: {e}", file=sys.stderr)
        payload.append(
            {
                "id": f.id,
                "name": f.name,
                "mime_type": f.mime_type,
                "creator_name": f.creator_name,
                "creator_email": f.creator_email,
                "last_modifying_user_name": f.last_modifying_user_name,
                "last_modifying_user_email": f.last_modifying_user_email,
                "size": f.size,
                "created_time": f.created_time.isoformat(),
                "modified_time": f.modified_time.isoformat(),
                "web_view_link": f.web_view_link,
                "local_path": local_path,
                "download_status": download_status,
            }
        )

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    ok = sum(1 for x in payload if x["download_status"] == "ok")
    print(
        f"\nwrote metadata for {len(payload)} file(s) -> {args.out}\n"
        f"downloaded {ok}/{len(payload)} into {download_root}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
