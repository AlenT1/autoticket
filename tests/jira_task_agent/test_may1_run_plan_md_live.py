"""Live test (capture mode, no Jira writes): generate the run-plan MD
for the May1 warm run and write it to data/run_plan_may1.md.

Mirrors test_may1_full_pipeline_live.py: cold-extract + cold-match seed
the cache, then apply the 3 mutations (UI-1 edit + MON-NEW + section J
+ DR-1/2/3) and run the warm pipeline in capture mode. Then renders the
report into the simplified MD format and writes it to disk.
"""
from __future__ import annotations

import json
import os
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jira_task_agent import runner as runner_module
from _shared.io.sinks.jira.client import JiraClient, get_issue
from jira_task_agent.pipeline.run_plan_md import render_run_plan_md
from jira_task_agent.runner import run_once

from .test_warm_scenarios_live import (
    _ensure_env,
    _maybe_run_cold,
    _may1_add_new_epic_section,
    _may1_add_task_to_section_c,
    _may1_edit_ui1_task,
    _no_redownload,
    _resolve_one_file,
)


pytestmark = [pytest.mark.live]

ROOT = Path(__file__).resolve().parents[2]
OUT_MD = ROOT / "data" / "run_plan_may1.md"
OUT_JSON = ROOT / "data" / "run_plan_may1.json"


def _jira_url(host: str | None, key: str | None) -> str | None:
    if not host or not key:
        return None
    base = host if host.startswith("http") else f"https://{host}"
    return f"{base.rstrip('/')}/browse/{key}"


def _build_plan_dict(report, *, jira: JiraClient) -> dict:
    host = getattr(jira, "host", None)
    totals: Counter = Counter()
    files_out = []

    for plan in report.plans:
        actions_out: list[dict] = []
        for g in plan.groups:
            ea = g.epic_action
            totals[ea.kind] += 1
            epic_key_for_children = ea.target_key

            if ea.kind == "update_epic" and ea.target_key:
                live = get_issue(ea.target_key, client=jira) or {}
                actions_out.append({
                    "kind": "update_epic",
                    "target_key": ea.target_key,
                    "jira_url": _jira_url(host, ea.target_key),
                    "summary": ea.summary,
                    "description": ea.description,
                    "source_anchor": ea.source_anchor,
                    "assignee_username": ea.assignee_username,
                    "live_summary": live.get("summary"),
                    "live_description": live.get("description"),
                })
            elif ea.kind == "create_epic":
                actions_out.append({
                    "kind": "create_epic",
                    "summary": ea.summary,
                    "description": ea.description,
                    "source_anchor": ea.source_anchor,
                    "assignee_username": ea.assignee_username,
                })
            elif ea.kind == "skip_completed_epic" and ea.target_key:
                actions_out.append({
                    "kind": "skip_completed_epic",
                    "target_key": ea.target_key,
                    "jira_url": _jira_url(host, ea.target_key),
                })

            parent_summary = None
            if epic_key_for_children:
                live_epic = get_issue(epic_key_for_children, client=jira) or {}
                parent_summary = live_epic.get("summary") or ea.summary

            for ta in g.task_actions:
                totals[ta.kind] += 1
                if ta.kind == "update_task" and ta.target_key:
                    live = get_issue(ta.target_key, client=jira) or {}
                    actions_out.append({
                        "kind": "update_task",
                        "target_key": ta.target_key,
                        "jira_url": _jira_url(host, ta.target_key),
                        "summary": ta.summary,
                        "description": ta.description,
                        "source_anchor": ta.source_anchor,
                        "assignee_username": ta.assignee_username,
                        "live_summary": live.get("summary"),
                        "live_description": live.get("description"),
                    })
                elif ta.kind == "create_task":
                    actions_out.append({
                        "kind": "create_task",
                        "summary": ta.summary,
                        "description": ta.description,
                        "source_anchor": ta.source_anchor,
                        "assignee_username": ta.assignee_username,
                        "parent_epic_key": epic_key_for_children,
                        "parent_epic_url": _jira_url(host, epic_key_for_children),
                        "parent_epic_summary": parent_summary,
                    })
                elif ta.kind == "covered_by_rollup":
                    actions_out.append({
                        "kind": "covered_by_rollup",
                        "target_key": ta.target_key,
                        "jira_url": _jira_url(host, ta.target_key),
                        "source_anchor": ta.source_anchor,
                    })

        files_out.append({
            "file_name": plan.file_name,
            "source_url": None,
            "last_edited_by": None,
            "actions": actions_out,
        })

    writes = sum(totals.get(k, 0) for k in (
        "create_epic", "update_epic", "create_task", "update_task",
    ))
    comments = totals.get("update_epic", 0) + totals.get("update_task", 0)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "dry-run (capture)",
        "source": "Drive (live test)",
        "files_scanned": len(report.plans),
        "files_with_changes": sum(1 for f in files_out if f["actions"]),
        "totals": {
            "create_epic": totals.get("create_epic", 0),
            "update_epic": totals.get("update_epic", 0),
            "create_task": totals.get("create_task", 0),
            "update_task": totals.get("update_task", 0),
            "skip_completed_epic": totals.get("skip_completed_epic", 0),
            "covered_by_rollup": totals.get("covered_by_rollup", 0),
            "comments": comments,
            "jira_ops": writes + comments,
        },
        "files": files_out,
    }


def test_render_may1_run_plan_md(tmp_path):
    _ensure_env()
    file_path = _resolve_one_file("*May1_Initial*.md")
    only_name = file_path.name.split("__", 1)[-1]

    cache_baseline, state_baseline, _, _ = _maybe_run_cold(
        scenario_name="MAY1_FULL_PIPELINE",
        file_path=file_path,
        only_name=only_name,
    )

    cache_path = tmp_path / "cache.json"
    state_path = tmp_path / "state.json"
    warm_capture = tmp_path / "warm_capture.json"
    shutil.copy(cache_baseline, cache_path)
    if state_baseline.exists():
        shutil.copy(state_baseline, state_path)

    original_text = file_path.read_text(encoding="utf-8")
    mutated = original_text
    mutated = _may1_edit_ui1_task(mutated)
    mutated = _may1_add_task_to_section_c(mutated)
    mutated = _may1_add_new_epic_section(mutated)

    try:
        file_path.write_text(mutated, encoding="utf-8")
        original_dl = runner_module.download_file
        runner_module.download_file = _no_redownload
        try:
            warm_report = run_once(
                apply=True,
                capture_path=str(warm_capture),
                cache_path=cache_path,
                state_path=state_path,
                use_cache=True,
                only_file_name=only_name,
                since_override=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        finally:
            runner_module.download_file = original_dl
    finally:
        file_path.write_text(original_text, encoding="utf-8")

    assert warm_report.errors == [], f"warm errors: {warm_report.errors}"

    jira = JiraClient.from_env()
    plan_dict = _build_plan_dict(warm_report, jira=jira)
    md = render_run_plan_md(plan_dict)

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(plan_dict, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    OUT_MD.write_text(md, encoding="utf-8")

    print(f"\n[run-plan-md] wrote: {OUT_JSON}", flush=True)
    print(f"[run-plan-md] wrote: {OUT_MD}", flush=True)
    print(f"[run-plan-md] {len(md)} chars, "
          f"{md.count(chr(10))} lines", flush=True)
    print(f"[run-plan-md] totals: {plan_dict['totals']}", flush=True)
    print(
        "[run-plan-md] re-render without LLM/Jira: "
        f"python -c \"import json; from jira_task_agent.pipeline.run_plan_md "
        f"import render_run_plan_md; "
        f"print(render_run_plan_md(json.load(open('{OUT_JSON}'))))\"",
        flush=True,
    )

    assert "# Run plan" in md
    assert "May1_Initial_Version_Tasks.md" in md
    assert "### Updates" in md
    assert "### Creates" in md
    assert "CENTPM-1237" in md
    assert "_new epic created in this run_" in md or "Under epic:" in md
    assert "(unlabeled)" not in md
