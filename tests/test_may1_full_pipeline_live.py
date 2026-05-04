"""Live test (capture mode, no Jira writes): full pipeline on May1.

Cold-extracts and cold-matches May1 to seed the caches, applies the 3
mutations (UI-1 edit + MON-NEW added + section J added with DR-1/2/3),
runs the warm pipeline end-to-end with `--capture`, and asserts that
the capture contains EXACTLY the writes the 5 mutations imply — no
incidental ops on unchanged tasks/epics.

Mutations → expected captured ops:

  - UI-1 edited (matches CENTPM-1237):
      PUT  /issue/CENTPM-1237                     (update_task body)
      POST /issue/CENTPM-1237/comment             (changelog)

  - MON-NEW added under cached "Monitoring" sub-epic (no candidate):
      POST /issue                                  (create_task)
      POST /issue/<new key>/remotelink             (back-pointer)

  - Section J brand-new sub-epic + DR-1 / DR-2 / DR-3:
      POST /issue                                  (create_epic for J)
      POST /issue/<new key>/remotelink
      POST /issue × 3                              (create_task DR-1/2/3)
      POST /issue/<new key>/remotelink × 3

Total: 1 PUT + 9 POST(/issue or /comment or /remotelink) = 11 ops.

Also asserts the run report's action counts: 1 update_task, 4 create_task,
1 create_epic, 0 noop.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dotenv import load_dotenv

from jira_task_agent import runner as runner_module
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


def _load_capture(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _classify_ops(ops: list[dict]) -> dict[str, list[dict]]:
    """Bucket captured ops by purpose."""
    out = {
        "issue_create_epic": [],
        "issue_create_task": [],
        "issue_update": [],
        "comment": [],
        "remotelink": [],
        "other": [],
    }
    for op in ops:
        method = op.get("method")
        path = op.get("path", "")
        body = op.get("body") or {}
        if method == "POST" and path == "/issue":
            itype = (body.get("fields") or {}).get("issuetype") or {}
            kind = itype.get("name") if isinstance(itype, dict) else None
            if kind == "Epic":
                out["issue_create_epic"].append(op)
            else:
                out["issue_create_task"].append(op)
        elif method == "PUT" and path.startswith("/issue/") and path.count("/") == 2:
            out["issue_update"].append(op)
        elif method == "POST" and path.endswith("/comment"):
            out["comment"].append(op)
        elif method == "POST" and path.endswith("/remotelink"):
            out["remotelink"].append(op)
        else:
            out["other"].append(op)
    return out


def test_may1_full_pipeline_only_processes_dirty_changes(tmp_path):
    _ensure_env()
    file_path = _resolve_one_file("*May1_Initial*.md")
    only_name = file_path.name.split("__", 1)[-1]

    cache_baseline, state_baseline, _, _ = _maybe_run_cold(
        scenario_name="MAY1_FULL_PIPELINE", file_path=file_path, only_name=only_name,
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
    assert "2026-Q3-DEADLINE-MAY1C" in mutated
    assert "2026-Q3-DEADLINE-MAY1J" in mutated

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

    ops = _load_capture(warm_capture)
    buckets = _classify_ops(ops)

    print(
        f"\n[may1-full] action counts: {dict(warm_report.actions_by_kind)}\n"
        f"[may1-full] capture buckets: "
        f"epic_create={len(buckets['issue_create_epic'])} "
        f"task_create={len(buckets['issue_create_task'])} "
        f"update={len(buckets['issue_update'])} "
        f"comment={len(buckets['comment'])} "
        f"remotelink={len(buckets['remotelink'])} "
        f"other={len(buckets['other'])}",
        flush=True,
    )

    assert len(buckets["issue_create_epic"]) == 1, (
        f"expected 1 create_epic (section J); got "
        f"{len(buckets['issue_create_epic'])}"
    )
    assert len(buckets["issue_create_task"]) == 4, (
        f"expected 4 create_task (MON-NEW + DR-1/2/3); got "
        f"{len(buckets['issue_create_task'])}: "
        f"{[(op['body'].get('fields') or {}).get('summary') for op in buckets['issue_create_task']]}"
    )
    assert len(buckets["issue_update"]) == 1, (
        f"expected 1 update (UI-1 → CENTPM-1237); got "
        f"{len(buckets['issue_update'])}: "
        f"{[op.get('path') for op in buckets['issue_update']]}"
    )
    assert buckets["issue_update"][0]["path"] == "/issue/CENTPM-1237"

    assert len(buckets["comment"]) == 1, (
        f"expected 1 comment (changelog on CENTPM-1237); got "
        f"{len(buckets['comment'])}"
    )
    assert buckets["comment"][0]["path"] == "/issue/CENTPM-1237/comment"

    assert len(buckets["remotelink"]) == 5, (
        f"expected 5 remotelinks (1 epic + 4 tasks); got "
        f"{len(buckets['remotelink'])}"
    )
    assert buckets["other"] == [], (
        f"unexpected ops: {buckets['other']}"
    )

    expected_total = 1 + 4 + 1 + 1 + 5
    assert len(ops) == expected_total, (
        f"expected exactly {expected_total} captured ops; got {len(ops)}"
    )

    actions = dict(warm_report.actions_by_kind)
    assert actions.get("update_task", 0) == 1, actions
    assert actions.get("create_task", 0) == 4, actions
    assert actions.get("create_epic", 0) == 1, actions
    assert actions.get("update_epic", 0) == 0, (
        f"expected zero update_epic (no dirty epic body); got actions={actions}"
    )
