"""List + download Drive files, then classify each via the LLM. Stops there.

Examples:
    python scripts/classify_files.py --today
    python scripts/classify_files.py --days 7
    python scripts/classify_files.py --since 2026-04-20T00:00
    python scripts/classify_files.py                  # all files in the folder
"""
from __future__ import annotations

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from jira_task_agent.drive.client import build_service, download_file, list_folder
from jira_task_agent.pipeline.classifier import classify_file
from jira_task_agent.pipeline.dedupe import find_duplicate_copies


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
    p = argparse.ArgumentParser(description="List + download + classify Drive files.")
    p.add_argument("folder_id", nargs="?", default=os.getenv("FOLDER_ID"))

    when = p.add_mutually_exclusive_group()
    when.add_argument("--since", type=_parse_since, default=None)
    when.add_argument("--today", action="store_true")
    when.add_argument("--days", type=int, default=None, metavar="N")

    p.add_argument("--out", default="data/snapshots/classifications.json")
    p.add_argument("--download-dir", default="data/gdrive_files")
    args = p.parse_args(argv)

    if not args.folder_id:
        p.error("folder_id required (positional or FOLDER_ID env var)")
    if not os.getenv("NVIDIA_API_KEY"):
        p.error("NVIDIA_API_KEY is required in .env to run the classifier")

    modified_after = _resolve_filter(args)
    if modified_after is not None:
        print(f"filter: modifiedTime > {modified_after.isoformat()}", file=sys.stderr)

    service = build_service()
    files = list_folder(args.folder_id, modified_after=modified_after, service=service)
    print(f"matched {len(files)} file(s) in folder", file=sys.stderr)

    if not files:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text("[]", encoding="utf-8")
        print(f"wrote 0 classifications -> {args.out}", file=sys.stderr)
        return 0

    download_root = Path(args.download_dir)
    neighbor_names = [f.name for f in files]
    rows: list[dict] = []
    role_counts: dict[str, int] = {}

    # Download all first so we can dedupe across the set before classifying.
    local_paths: dict[str, Path] = {}
    for f in files:
        try:
            local = download_file(f, download_root, service=service)
        except Exception as e:  # noqa: BLE001
            print(f"  [download error] {f.name}: {e}", file=sys.stderr)
            continue
        if local is None:
            print(f"  [unsupported  ] {f.name}", file=sys.stderr)
            continue
        local_paths[f.id] = local

    duplicate_of = find_duplicate_copies(files, local_paths)
    if duplicate_of:
        for dup_id, canon_id in duplicate_of.items():
            dup = next((x for x in files if x.id == dup_id), None)
            canon = next((x for x in files if x.id == canon_id), None)
            if dup and canon:
                print(
                    f"  [dup of canonical] {dup.name} ({dup.mime_type}) "
                    f"==> canonical: {canon.name} ({canon.mime_type})",
                    file=sys.stderr,
                )

    for f in files:
        local = local_paths.get(f.id)
        if local is None:
            continue

        if f.id in duplicate_of:
            role, confidence, reason = (
                "skip",
                1.0,
                f"content-duplicate of canonical file {duplicate_of[f.id]}",
            )
        else:
            try:
                res = classify_file(
                    f, local_path=local, neighbor_names=neighbor_names
                )
            except Exception as e:  # noqa: BLE001
                print(f"  [classify error] {f.name}: {e}", file=sys.stderr)
                continue
            role, confidence, reason = res.role, res.confidence, res.reason

        role_counts[role] = role_counts.get(role, 0) + 1
        print(
            f"  [{role:5s} conf={confidence:.2f}] {f.name}  --  {reason}",
            file=sys.stderr,
        )
        rows.append(
            {
                "id": f.id,
                "name": f.name,
                "mime_type": f.mime_type,
                "size": f.size,
                "modified_time": f.modified_time.isoformat(),
                "last_modifying_user_name": f.last_modifying_user_name,
                "web_view_link": f.web_view_link,
                "local_path": str(local),
                "classification": {
                    "role": role,
                    "confidence": confidence,
                    "reason": reason,
                },
            }
        )

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"\nclassified {len(rows)}/{len(files)} file(s)  by role: {dict(role_counts)}\n"
        f"wrote -> {args.out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
