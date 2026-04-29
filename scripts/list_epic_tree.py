"""Build a tree: an epic + all its child issues, with descriptions.

Examples:
    python list_epic_tree.py CENTPM-1162
    python list_epic_tree.py CENTPM-1162 --out tree.json
"""

from __future__ import annotations

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import json
import sys

from dotenv import load_dotenv

from jira_task_agent.jira.client import JiraClient, get_issue, list_epic_children


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description="Fetch epic + its children as a tree.")
    p.add_argument("epic_key", help="e.g. CENTPM-1162")
    p.add_argument(
        "--out",
        default="data/snapshots/epic_tree.json",
        help="Path to write JSON. Default: data/snapshots/epic_tree.json",
    )
    args = p.parse_args(argv)

    client = JiraClient.from_env()
    print(
        f"Jira: https://{client.host}  (auth: {client.auth_mode})", file=sys.stderr
    )

    print(f"fetching epic {args.epic_key} ...", file=sys.stderr)
    epic = get_issue(args.epic_key, client=client)

    print(f"fetching children of {args.epic_key} ...", file=sys.stderr)
    children = list_epic_children(args.epic_key, client=client)

    tree = {**epic, "children": children}

    _Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(tree, fh, indent=2, ensure_ascii=False)

    print(
        f"\nepic   {epic['key']}  '{epic['summary']}'\n"
        f"       status={epic['status']}  assignee={epic.get('assignee_name')}\n"
        f"children {len(children)} issue(s):",
        file=sys.stderr,
    )
    for c in children:
        desc = (c.get("description") or "").strip().splitlines()
        first_line = desc[0][:80] if desc else ""
        print(
            f"  {c['key']:14s} [{(c['issue_type'] or '-'):8s}] "
            f"[{(c['status'] or '-'):14s}] {(c['summary'] or '')[:60]}",
            file=sys.stderr,
        )
        if first_line:
            print(f"                  {first_line}", file=sys.stderr)

    print(f"\nwrote tree -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
