"""CLI entrypoint: `python -m jira_task_agent run [--apply] [--since ...]`."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from .runner import run_once


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _print_report(report) -> None:
    print()
    print("=" * 72, file=sys.stderr)
    print(
        f"Run finished. apply={report.apply}",
        file=sys.stderr,
    )
    print(f"  files seen:        {report.files_total}", file=sys.stderr)
    print(f"  classified by role: {dict(report.files_classified)}", file=sys.stderr)
    print(
        f"  extractions:       ok={report.extractions_ok} failed={report.extractions_failed}",
        file=sys.stderr,
    )
    print(
        f"  cache hits:        classify={report.cache_hits_classify}  "
        f"extract={report.cache_hits_extract}  "
        f"match={report.cache_hits_match}",
        file=sys.stderr,
    )
    print(f"  actions by kind:   {dict(report.actions_by_kind)}", file=sys.stderr)
    if report.errors:
        print(f"  errors ({len(report.errors)}):", file=sys.stderr)
        for e in report.errors:
            print(f"    - {e}", file=sys.stderr)
    print("=" * 72, file=sys.stderr)


def _serialize_plans(report) -> list[dict]:
    out = []
    for plan in report.plans:
        out.append(
            {
                "file_id": plan.file_id,
                "file_name": plan.file_name,
                "role": plan.role,
                "groups": [
                    {
                        "epic": _serialize_action(g.epic_action),
                        "tasks": [_serialize_action(a) for a in g.task_actions],
                    }
                    for g in plan.groups
                ],
            }
        )
    return out


def _serialize_action(a) -> dict:
    return {
        "kind": a.kind,
        "target_key": a.target_key,
        "epic_key": a.epic_key,
        "epic_anchor": a.epic_anchor,
        "summary": a.summary,
        "assignee_username": a.assignee_username,
        "before_summary": a.before_summary,
        "source_anchor": a.source_anchor,
        "match_confidence": a.match_confidence,
        "match_reason": a.match_reason,
        "note": a.note,
    }


def cmd_run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    apply_writes = args.apply or bool(args.capture)
    report = run_once(
        apply=apply_writes,
        since_override=args.since,
        download_dir=args.download_dir,
        only_file_name=args.only,
        target_epic=args.target_epic,
        capture_path=args.capture,
        use_cache=not args.no_cache,
    )
    _print_report(report)

    if args.report_out:
        out_p = Path(args.report_out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(
            json.dumps(_serialize_plans(report), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  plan written to: {args.report_out}", file=sys.stderr)

    return 0 if not report.errors else 2


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="python -m jira_task_agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser(
        "run",
        help="One-shot run. Defaults to dry-run; pass --apply to write to Jira.",
    )
    r.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to Jira. Without this, the run is dry: classify + extract + reconcile only.",
    )
    r.add_argument(
        "--since",
        type=_parse_iso,
        default=None,
        help="Override last_run_at cursor with this ISO timestamp.",
    )
    r.add_argument(
        "--download-dir",
        default="data/gdrive_files",
        help="Local download dir. Default: data/gdrive_files/",
    )
    r.add_argument(
        "--report-out",
        default="data/run_plan.json",
        help="Path to write the per-file plan JSON. Default: data/run_plan.json",
    )
    r.add_argument(
        "--only",
        default=None,
        metavar="NAME",
        help=(
            "Process only the Drive file whose name matches NAME exactly. "
            "Other files are still classified (so root context stays "
            "available) but only the matching file is extracted/applied. "
            "Useful for narrow tests."
        ),
    )
    r.add_argument(
        "--target-epic",
        default=None,
        metavar="KEY",
        help=(
            "When set (e.g. CENTPM-1253), all created tasks are routed under "
            "this one epic regardless of fuzzy match. Existing-epic actions "
            "(create/update/noop on the doc's own epic) are skipped. Used to "
            "keep test runs contained in the standing test container."
        ),
    )
    r.add_argument(
        "--capture",
        default=None,
        metavar="PATH",
        help=(
            "Run the apply path but record Jira write payloads to PATH "
            "instead of sending them. Implies --apply (the apply branch "
            "must run for capture to record anything). No network calls "
            "for writes; reads still go through. Used to inspect the "
            "exact payload that would have been POST/PUT before any real "
            "write."
        ),
    )
    r.add_argument(
        "--no-cache",
        action="store_true",
        help=(
            "Do not consult or update cache.json. Forces re-classify and "
            "re-extract for every file. Use when prompts change or when "
            "you want to verify behaviour from scratch."
        ),
    )
    r.set_defaults(func=cmd_run)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
