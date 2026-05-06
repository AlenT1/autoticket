"""Write the full Jira project tree (epics + their direct children) to
JSON. Wraps `_shared.io.sinks.jira.project_tree.fetch_project_tree`.

    python scripts/list_project_tree.py            # uses $JIRA_PROJECT_KEY
    python scripts/list_project_tree.py CENTPM
    python scripts/list_project_tree.py --out tree.json
"""
from __future__ import annotations

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from _shared.io.sinks.jira.client import JiraClient
from _shared.io.sinks.jira.project_tree import fetch_project_tree


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description="One-shot project-wide snapshot.")
    p.add_argument(
        "project_key",
        nargs="?",
        default=os.getenv("JIRA_PROJECT_KEY"),
        help="Jira project key. Default: $JIRA_PROJECT_KEY",
    )
    p.add_argument(
        "--out",
        default="data/snapshots/project_tree.json",
        help="Path to write JSON. Default: data/snapshots/project_tree.json",
    )
    args = p.parse_args(argv)
    if not args.project_key:
        p.error("project_key required (positional or $JIRA_PROJECT_KEY)")

    client = JiraClient.from_env()
    print(
        f"Jira: https://{client.host}  project: {args.project_key}",
        file=sys.stderr,
    )
    tree = fetch_project_tree(client, args.project_key, log=sys.stderr)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(tree, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nwrote -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
