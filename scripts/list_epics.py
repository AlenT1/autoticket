"""List all Epics in a Jira project.

Examples:
    python list_epics.py                    # uses JIRA_PROJECT_KEY from .env
    python list_epics.py CENT
    python list_epics.py CENT --out epics.json
"""

from __future__ import annotations

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import json
import os
import sys

from dotenv import load_dotenv

from _shared.io.sinks.jira.client import JiraClient, list_epics


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description="List epics from a Jira project.")
    p.add_argument(
        "project_key",
        nargs="?",
        default=os.getenv("JIRA_PROJECT_KEY"),
        help="Jira project key (e.g. CENT). Defaults to $JIRA_PROJECT_KEY.",
    )
    p.add_argument(
        "--out",
        default="data/snapshots/epics.json",
        help="Path to write metadata JSON. Default: data/snapshots/epics.json",
    )
    args = p.parse_args(argv)

    if not args.project_key:
        p.error("project_key required (positional or JIRA_PROJECT_KEY env var)")

    client = JiraClient.from_env()
    print(
        f"Jira: https://{client.host}  (auth: {client.auth_mode})  "
        f"project: {args.project_key}",
        file=sys.stderr,
    )

    epics = list_epics(args.project_key, client=client)

    text = json.dumps(epics, indent=2, ensure_ascii=False)
    _Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"\nwrote {len(epics)} epic(s) -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
