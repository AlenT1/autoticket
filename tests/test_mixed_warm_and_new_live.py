"""Live test (capture mode): one pipeline run mixing a warm-with-diffs
file and a fresh (cold-path) file.

  - May1: same 3 mutations as test_may1_full_pipeline_live.py. Expected
    to produce exactly the 5-mutation op set: 1 create_epic (section J)
    + 4 create_task + 1 update_task + 1 comment = 7 ops.

  - V11: starts the warm phase with NO extraction or matcher cache for
    its file_id, so the runner treats it as new. Expected: full cold
    path — 1 epic action (`create_epic` if no Jira match, `update_epic`
    if matched + body differs) + N task actions for every V11 task.

`download_file` is patched so the runner reads local copies of both
files. `list_folder` is patched so only those two files reach the
pipeline (no LLM/Jira spend on the other 17 files in the Drive folder).
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
            (out["issue_create_epic"] if kind == "Epic" else out["issue_create_task"]).append(op)
        elif method == "PUT" and path.startswith("/issue/") and path.count("/") == 2:
            out["issue_update"].append(op)
        elif method == "POST" and path.endswith("/comment"):
            out["comment"].append(op)
        elif method == "POST" and path.endswith("/remotelink"):
            out["remotelink"].append(op)
        else:
            out["other"].append(op)
    return out


def _ops_for_file(ops: list[dict], file_marker: str) -> list[dict]:
    """Filter ops whose body / path mentions a file-specific marker."""
    return [op for op in ops if file_marker in json.dumps(op, ensure_ascii=False)]


def _clear_extract_and_match(cache_path: Path, file_id: str) -> None:
    """Reset the extraction + matcher cache for one file so the runner
    treats it as fresh (cold path), while preserving its classification."""
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    entry = (data.get("files") or {}).get(file_id)
    if entry is None:
        return
    entry["extraction_payload"] = None
    entry["matcher_payload"] = None
    entry["diff_payload"] = None
    cache_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_warm_may1_plus_fresh_v11_in_one_run(tmp_path):
    _ensure_env()

    may1_path = _resolve_one_file("*May1_Initial*.md")
    v11_path = _resolve_one_file("*V11_Dashboard*.md")
    may1_id = may1_path.name.split("__", 1)[0]
    v11_id = v11_path.name.split("__", 1)[0]

    cache_baseline, state_baseline, _, _ = _maybe_run_cold(
        scenario_name="MAY1_FULL_PIPELINE",
        file_path=may1_path,
        only_name=may1_path.name.split("__", 1)[-1],
    )

    cache_path = tmp_path / "cache.json"
    state_path = tmp_path / "state.json"
    warm_capture = tmp_path / "warm_capture.json"
    shutil.copy(cache_baseline, cache_path)
    if state_baseline.exists():
        shutil.copy(state_baseline, state_path)

    _clear_extract_and_match(cache_path, v11_id)

    may1_original = may1_path.read_text(encoding="utf-8")
    mutated = may1_original
    mutated = _may1_edit_ui1_task(mutated)
    mutated = _may1_add_task_to_section_c(mutated)
    mutated = _may1_add_new_epic_section(mutated)
    assert "2026-Q3-DEADLINE-MAY1J" in mutated

    original_list_folder = runner_module.list_folder

    def _list_only_two(folder_id: str, *, service=None):
        all_files = original_list_folder(folder_id, service=service)
        return [f for f in all_files if f.id in (may1_id, v11_id)]

    try:
        may1_path.write_text(mutated, encoding="utf-8")
        original_dl = runner_module.download_file
        runner_module.download_file = _no_redownload
        runner_module.list_folder = _list_only_two
        try:
            warm_report = run_once(
                apply=True,
                capture_path=str(warm_capture),
                cache_path=cache_path,
                state_path=state_path,
                use_cache=True,
                since_override=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        finally:
            runner_module.download_file = original_dl
            runner_module.list_folder = original_list_folder
    finally:
        may1_path.write_text(may1_original, encoding="utf-8")

    assert warm_report.errors == [], f"warm errors: {warm_report.errors}"

    ops = _load_capture(warm_capture)
    actions = dict(warm_report.actions_by_kind)
    buckets = _classify_ops(ops)

    print(
        f"\n[mixed] action counts: {actions}\n"
        f"[mixed] capture total: {len(ops)} ops "
        f"(epic_create={len(buckets['issue_create_epic'])} "
        f"task_create={len(buckets['issue_create_task'])} "
        f"update={len(buckets['issue_update'])} "
        f"comment={len(buckets['comment'])} "
        f"remotelink={len(buckets['remotelink'])} "
        f"other={len(buckets['other'])})",
        flush=True,
    )

    may1_v11_path = "/issue/CENTPM-1237"
    assert any(op.get("path") == may1_v11_path for op in buckets["issue_update"]), (
        f"expected May1's UI-1 update on CENTPM-1237; got "
        f"{[op.get('path') for op in buckets['issue_update']]}"
    )
    assert len(buckets["issue_update"]) == 1, (
        f"expected exactly 1 update (May1 UI-1); got {len(buckets['issue_update'])}"
    )

    assert actions.get("update_task", 0) == 1, (
        f"expected exactly 1 update_task (May1 UI-1); got {actions}"
    )
    assert actions.get("create_epic", 0) == 2, (
        f"expected 2 create_epic (May1 J + V11 cold); got {actions}"
    )
    assert actions.get("create_task", 0) >= 4 + 1, (
        f"expected >=5 create_task (4 May1 + at least 1 V11); got {actions}"
    )
    assert buckets["other"] == [], f"unexpected ops: {buckets['other']}"

    expected_ops = (
        len(buckets["issue_create_epic"])
        + len(buckets["issue_create_task"])
        + len(buckets["issue_update"])
        + len(buckets["comment"])
        + len(buckets["remotelink"])
    )
    assert expected_ops == len(ops), (
        f"capture total mismatch: {len(ops)} vs sum-of-buckets {expected_ops}"
    )

    update_count = len(buckets["issue_update"])
    assert buckets["remotelink"] == [], (
        f"unexpected remotelink ops: {buckets['remotelink']}"
    )
    assert len(buckets["comment"]) == update_count, (
        f"expected {update_count} comment(s) (1 per update); "
        f"got {len(buckets['comment'])}"
    )
