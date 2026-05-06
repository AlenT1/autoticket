"""End-to-end Jira write smoke test.

Exercises the four write primitives the agent will use in production:
    1. create_issue (Epic) under JIRA_PROJECT_KEY
    2. create_issue (Task) under that Epic, with assignee resolved from
       team_mapping.json
    3. update_issue (description change)
    4. post_comment with [~assignee] mention
    5. transition_issue → Done   (cleanup; opt-out with --no-cleanup)

Each run creates fresh issues with timestamps in the title; safe to run
multiple times. Issues are tagged "[smoke-test]" in the summary for easy
filtering in the Jira UI.
"""
from __future__ import annotations

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from _shared.io.sinks.jira.client import JiraClient, get_issue


def _ts() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(
        description="Smoke-test Jira write primitives end-to-end."
    )
    p.add_argument(
        "--project", default=os.getenv("JIRA_PROJECT_KEY"),
        help="Jira project key. Default: $JIRA_PROJECT_KEY",
    )
    p.add_argument(
        "--assignee", default="Saar",
        help="Owner string to resolve via team_mapping.json. Default: Saar",
    )
    p.add_argument(
        "--no-cleanup", action="store_true",
        help="Skip closing the smoke issues (leave them open for inspection).",
    )
    args = p.parse_args(argv)

    if not args.project:
        p.error("project required (--project or JIRA_PROJECT_KEY env var)")

    client = JiraClient.from_env()
    print(
        f"[1] Jira: https://{client.host}  project={args.project}",
        file=sys.stderr,
    )

    # 2) Resolve assignee
    print(f"[2] Resolving assignee {args.assignee!r} ...", file=sys.stderr)
    username = client.resolve_assignee_username(args.assignee)
    if not username:
        print(
            f"    WARN: could not resolve {args.assignee!r}; "
            f"will create unassigned",
            file=sys.stderr,
        )
    else:
        print(f"    resolved -> {username}", file=sys.stderr)

    extra = {"assignee": {"name": username}} if username else None
    ts = _ts()

    # 3) Create epic
    epic_summary = f"[smoke-test] agent write check {ts}"
    epic_desc = (
        "Disposable epic created by `jira-task-agent` smoke test.\n\n"
        f"Run timestamp: {ts}\n\n"
        "Safe to delete or close.\n\n"
        "<!-- managed-by:jira-task-agent v1 -->"
    )
    print(f"[3] Creating epic ...", file=sys.stderr)
    epic = client.create_issue(
        project_key=args.project,
        summary=epic_summary,
        description=epic_desc,
        issue_type="Epic",
        extra_fields=extra,
    )
    epic_key = epic["key"]
    print(f"    -> {epic_key}: {epic_summary!r}", file=sys.stderr)

    # 5) Create task linked to epic
    task_summary = f"[smoke-test] child task {ts}"
    task_desc = (
        "Disposable task created by smoke test.\n\n"
        "### Implementation hints\n"
        "```python\n"
        "# This is a fenced code block to verify rendering.\n"
        "print('hello from the smoke test')\n"
        "```\n\n"
        "### Acceptance criteria\n"
        "- Smoke test reports success.\n"
        "- Issue is visible in Jira with the correct fields.\n\n"
        "### Definition of Done\n"
        "- [ ] Smoke test passes end-to-end\n"
        "- [ ] All write primitives exercised\n"
        "- [ ] Issues cleaned up\n\n"
        "### Source\n"
        "- Doc: smoke-test fake doc\n"
        "- Last edited by: smoke-test\n\n"
        "<!-- managed-by:jira-task-agent v1 -->"
    )
    print(f"[5] Creating task linked to {epic_key} ...", file=sys.stderr)
    task = client.create_issue(
        project_key=args.project,
        summary=task_summary,
        description=task_desc,
        issue_type="Task",
        epic_link=epic_key,
        extra_fields=extra,
    )
    task_key = task["key"]
    print(f"    -> {task_key}: {task_summary!r}", file=sys.stderr)

    # 6) Update task description
    print(f"[6] Updating description of {task_key} ...", file=sys.stderr)
    new_desc = task_desc.replace(
        "Disposable task created by smoke test.",
        "Disposable task created by smoke test. (updated)",
    )
    client.update_issue(task_key, {"description": new_desc})

    # 7) Post comment with [~mention]
    mention = f"[~{username}]" if username else "(unassigned)"
    comment_body = (
        f"{mention}\n\n"
        "Smoke-test comment from `jira-task-agent`. This issue is "
        "disposable.\n\n"
        f"- Run timestamp: {ts}\n"
        f"- Resolved assignee: {username or 'NONE'}"
    )
    print(f"[7] Posting comment on {task_key} (mention: {mention}) ...", file=sys.stderr)
    client.post_comment(task_key, comment_body)

    # 8) Read back to verify
    print(f"[8] Reading {task_key} back to verify ...", file=sys.stderr)
    final = get_issue(task_key, client=client)
    print(f"    summary:   {final['summary']}", file=sys.stderr)
    print(
        f"    assignee:  {final.get('assignee_name')} "
        f"({final.get('assignee_username')})",
        file=sys.stderr,
    )
    desc_tail = (final.get("description") or "")[-80:]
    print(f"    desc tail: {desc_tail!r}", file=sys.stderr)

    # 9) Cleanup
    if not args.no_cleanup:
        print(f"\n[cleanup] Transitioning issues to Done ...", file=sys.stderr)
        for k in (task_key, epic_key):
            transitioned = False
            for status in ("Done", "Closed", "Resolved"):
                if client.transition_issue(k, status):
                    print(f"    {k} -> {status}", file=sys.stderr)
                    transitioned = True
                    break
            if not transitioned:
                print(
                    f"    {k}: no clean closing transition; leaving open",
                    file=sys.stderr,
                )

    print(f"\nDone. Epic: {epic_key}    Task: {task_key}", file=sys.stderr)
    print(
        f"View task: https://{client.host}/browse/{task_key}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
