"""Live E2E test on a local file (no Jira writes).

Stages a planning doc in `data/local_files/` that:

  1. Describes the standing test epic (CENTPM-1253 "Jira Task Agent Test")
     so Stage 1 pairs the doc-defined epic to it.
  2. Restates the existing task (CENTPM-1255 "Jira Agent Test Task")
     with an updated body so Stage 2 pairs it to the existing task and
     the reconciler emits `update_task`.
  3. Adds a brand-new task that should surface as `create_task` under
     the matched epic.

Runs the pipeline with `--source local` in capture mode, then renders
the run-plan MD so the PM can review what would be sent to Jira before
approving an --apply run.

Outputs:
  data/run_plan_local_test.json
  data/run_plan_local_test.md

The local file persists at data/local_files/ so the user can flip to
--apply once the MD looks right.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dotenv import load_dotenv

from jira_task_agent.jira.client import JiraClient
from jira_task_agent.pipeline.run_plan_md import (
    build_run_plan_dict,
    render_run_plan_md,
)
from jira_task_agent.runner import run_once


pytestmark = [pytest.mark.live]

ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR = ROOT / "data" / "local_files"
LOCAL_FILE_NAME = "jira_task_agent_test.md"
LOCAL_FILE_PATH = LOCAL_DIR / LOCAL_FILE_NAME

OUT_MD = ROOT / "data" / "run_plan_local_test.md"
OUT_JSON = ROOT / "data" / "run_plan_local_test.json"


DOC_BODY = """# Jira Task Agent Test — May 2026 verification cycle

This standing test container has been refreshed for the May 2026 dev
cycle. The agent should detect the doc-side update on this epic and
issue an `update_epic` against the existing CENTPM-1253, posting a
changelog comment that mentions the current assignee and names this
source file. No new epic should be created.

Compared to the previous cycle, the scope is widened. In addition to
the original "create works" check, this epic now also covers:
update_task fidelity (a doc edit on an existing task reaches the same
Jira key with a comment, not a duplicate), update_epic fidelity (an
epic body refresh reaches the existing epic instead of forking),
the new `--apply --verify` gate that renders a human-readable run
plan and pauses the apply path for review, and the local-folder
source path (`--source local`) that bypasses Drive entirely.

Owner: Saar.

## Tasks

- T1 Validate create + update + comment fidelity end-to-end — This
  task previously only covered `create_issue`. The expanded scope for
  the May 2026 cycle adds end-to-end coverage of update behavior:
  re-running the agent after a doc edit triggers `update_task` on the
  same Jira key (no duplicate created), the changelog comment posted
  on update mentions the current Jira assignee with `[~name]`, the
  comment names the source doc and last editor, and the diff bullets
  describe the actual change rather than a generic "description
  updated" line. The Definition of Done now also explicitly verifies
  the comment body itself, not just the issue body.

- T2 Confirm new-task creation flow under standing test epic — Add a
  brand-new task definition to the source doc and re-run the agent.
  The agent should surface it as a `create_task` (never a duplicate
  `update_task`) with the correct `Epic Link` to the standing test
  epic, the `ai-generated` content marker, and a populated Definition
  of Done section. Use this task to spot-check that `source_anchor` is
  threaded through caching and the matcher splice correctly so future
  warm runs treat it as cached. Owner: Saar.
"""


def _ensure_env() -> None:
    load_dotenv()
    import os
    for var in ("NVIDIA_API_KEY", "JIRA_HOST", "JIRA_PROJECT_KEY", "JIRA_TOKEN"):
        if not os.environ.get(var):
            pytest.skip(f"{var} not set; live test skipped")


def _build_plan_dict(report, *, jira: JiraClient) -> dict:
    plan = build_run_plan_dict(
        report, jira=jira, mode="dry-run (capture)", source="local",
    )
    for f in plan["files"]:
        f["source_url"] = LOCAL_FILE_PATH.resolve().as_uri()
    return plan


def test_local_e2e_md_review(tmp_path):
    _ensure_env()

    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_FILE_PATH.write_text(DOC_BODY, encoding="utf-8")

    cache_path = tmp_path / "cache.json"
    state_path = tmp_path / "state.json"
    capture_path = tmp_path / "capture.json"

    report = run_once(
        apply=True,
        capture_path=str(capture_path),
        cache_path=cache_path,
        state_path=state_path,
        use_cache=False,
        source="local",
        local_dir=str(LOCAL_DIR),
        only_file_name=LOCAL_FILE_NAME,
        since_override=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert report.errors == [], f"errors: {report.errors}"
    assert report.plans, "expected at least one ReconcilePlan"

    jira = JiraClient.from_env()
    plan_dict = _build_plan_dict(report, jira=jira)
    md = render_run_plan_md(plan_dict)

    OUT_JSON.write_text(
        json.dumps(plan_dict, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    OUT_MD.write_text(md, encoding="utf-8")

    print(f"\n[local-e2e] wrote: {OUT_JSON}", flush=True)
    print(f"[local-e2e] wrote: {OUT_MD}", flush=True)
    print(f"[local-e2e] {len(md)} chars, {md.count(chr(10))} lines", flush=True)
    print(f"[local-e2e] totals: {plan_dict['totals']}", flush=True)
    print(
        "[local-e2e] re-render without LLM/Jira: "
        f"python -c \"import json; from jira_task_agent.pipeline.run_plan_md "
        f"import render_run_plan_md; "
        f"print(render_run_plan_md(json.load(open('{OUT_JSON}'))))\"",
        flush=True,
    )

    assert "# Run plan" in md
    assert LOCAL_FILE_NAME in md
    actions = plan_dict["files"][0]["actions"]
    kinds = [a["kind"] for a in actions]
    assert kinds, f"expected at least one action; got {kinds}"
    print(f"[local-e2e] actions: {kinds}", flush=True)
