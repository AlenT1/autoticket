"""Run the LLM extractor against one downloaded task file.

Reads `classifications.json` to locate the file (and to identify root files
for context). Writes the resulting {epic, [tasks]} payload to JSON.

Examples:
    python scripts/extract_one.py V2_CentARB_Tasks.md
    python scripts/extract_one.py 1kB7TZZoldeR1UhGdj-792Wm1WerqmuGK
    python scripts/extract_one.py V2_CentARB_Tasks.md --out my_extraction.json
"""
from __future__ import annotations

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import json
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from jira_task_agent.drive.client import DriveFile
from jira_task_agent.pipeline.context_bundler import bundle_root_context
from jira_task_agent.pipeline.extractor import (
    extract_from_file,
    extract_multi_from_file,
)


def _file_from_record(rec: dict) -> DriveFile:
    return DriveFile(
        id=rec["id"],
        name=rec["name"],
        mime_type=rec["mime_type"],
        created_time=datetime.fromisoformat(rec.get("created_time") or rec["modified_time"]),
        modified_time=datetime.fromisoformat(rec["modified_time"]),
        size=rec.get("size"),
        creator_name=None,
        creator_email=None,
        last_modifying_user_name=rec.get("last_modifying_user_name"),
        last_modifying_user_email=None,
        parents=[],
        web_view_link=rec.get("web_view_link"),
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description="Extract one task file via LLM.")
    p.add_argument("target", help="filename or Drive file id of the task file")
    p.add_argument(
        "--classifications",
        default="data/snapshots/classifications.json",
        help="Path to classifications.json from the previous classify run.",
    )
    p.add_argument(
        "--out",
        default="data/snapshots/extraction.json",
        help="Path to write extracted JSON. Default: data/snapshots/extraction.json",
    )
    args = p.parse_args(argv)

    classifications = json.loads(Path(args.classifications).read_text(encoding="utf-8"))

    target_rec = next(
        (
            r
            for r in classifications
            if r["id"] == args.target or r["name"] == args.target
        ),
        None,
    )
    if not target_rec:
        p.error(f"target {args.target!r} not found in {args.classifications}")

    role = target_rec["classification"]["role"]
    # Backward-compat with older classifications.json that used "task".
    if role == "task":
        role = "single_epic"
    if role not in {"single_epic", "multi_epic"}:
        print(
            f"warning: target's classified role is {role!r}; "
            f"the extractor expects 'single_epic' or 'multi_epic'. "
            f"Proceeding with single-epic path.",
            file=sys.stderr,
        )
    target_file = _file_from_record(target_rec)
    target_local = Path(target_rec["local_path"])
    if not target_local.exists():
        p.error(f"local file missing: {target_local}")

    root_pairs: list[tuple[str, Path]] = [
        (r["name"], Path(r["local_path"]))
        for r in classifications
        if r["classification"]["role"] == "root"
        and r.get("local_path")
        and Path(r["local_path"]).exists()
    ]
    print(
        f"target: {target_file.name}\n"
        f"        ({target_file.id})\n"
        f"root context files ({len(root_pairs)}):",
        file=sys.stderr,
    )
    for name, _ in root_pairs:
        print(f"  - {name}", file=sys.stderr)

    root_context = bundle_root_context(root_pairs)
    print(
        f"\nrunning extractor for role={role!r} (root context: {len(root_context):,} chars) ...\n",
        file=sys.stderr,
    )

    if role == "multi_epic":
        multi = extract_multi_from_file(
            target_file, local_path=target_local, root_context=root_context
        )
        payload = {
            "source_file_id": multi.file_id,
            "source_file_name": multi.file_name,
            "role": role,
            "epics": [
                {
                    "summary": e.summary,
                    "description": e.description,
                    "assignee": e.assignee_name,
                    "tasks": [
                        {
                            "summary": t.summary,
                            "description": t.description,
                            "source_anchor": t.source_anchor,
                            "assignee": t.assignee_name,
                        }
                        for t in e.tasks
                    ],
                }
                for e in multi.epics
            ],
        }
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        total_tasks = sum(len(e.tasks) for e in multi.epics)
        print(
            f"\nMULTI-EPIC: {len(multi.epics)} epic(s), "
            f"{total_tasks} task(s) total\n",
            file=sys.stderr,
        )
        for ei, e in enumerate(multi.epics, 1):
            print(
                f"  EPIC #{ei}: {e.summary}\n"
                f"          assignee={e.assignee_name or '(none)'}  "
                f"tasks={len(e.tasks)}",
                file=sys.stderr,
            )
            for ti, t in enumerate(e.tasks, 1):
                print(
                    f"    {ti:>2}. [{(t.assignee_name or '-'):14s}] {t.summary}",
                    file=sys.stderr,
                )
            print("", file=sys.stderr)
        print(f"wrote -> {args.out}", file=sys.stderr)
        return 0

    # role == "task" (or anything else: fall through to single-epic extractor)
    result = extract_from_file(
        target_file, local_path=target_local, root_context=root_context
    )
    payload = {
        "source_file_id": result.file_id,
        "source_file_name": result.file_name,
        "role": role,
        "epic": {
            "summary": result.epic.summary,
            "description": result.epic.description,
            "assignee": result.epic.assignee_name,
        },
        "tasks": [
            {
                "summary": t.summary,
                "description": t.description,
                "source_anchor": t.source_anchor,
                "assignee": t.assignee_name,
            }
            for t in result.tasks
        ],
    }
    Path(args.out).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(
        f"\nEPIC: {result.epic.summary}\n"
        f"      assignee={result.epic.assignee_name or '(none)'}  "
        f"({len(result.epic.description)} chars of description)",
        file=sys.stderr,
    )
    print(f"\nTASKS ({len(result.tasks)}):", file=sys.stderr)
    for i, t in enumerate(result.tasks, 1):
        print(
            f"  {i:>2}. [{(t.assignee_name or '-'):14s}] {t.summary}    "
            f"[anchor: {t.source_anchor}]",
            file=sys.stderr,
        )
    print(f"\nwrote -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
