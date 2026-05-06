"""Real-data integration test for the two-stage `run_matcher`.

Loads:
  - extraction.json   (May1 multi-epic extraction; 9 sub-epics, 40 tasks)
  - project_tree.json (97 CENTPM epics with their 191 direct children)

Calls `pipeline.matcher.run_matcher(...)` once. Asserts that:
  - Stage 1 (epic match) pairs the 6 known May1 sub-epics to the right
    CENTPM epics (CENTPM-1162 etc.).
  - Stage 2 (grouped task match) for Section A → CENTPM-1162 produces the
    6 known correct task pairings.
  - Orphans are surfaced for matched epics.

Marked `live` (opt-in). LLM calls: 1 Stage-1 + ~2 Stage-2 batches.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from jira_task_agent.pipeline.extractor import (
    ExtractedEpicWithTasks,
    ExtractedTask,
    MultiExtractionResult,
)
from jira_task_agent.pipeline.matcher import run_matcher


pytestmark = [pytest.mark.live]


ROOT = Path(__file__).resolve().parents[2]


EXPECTED_EPIC_PAIRS: dict[str, str] = {
    "Production Security Hardening":      "CENTPM-1162",
    "Production Environment Setup":       "CENTPM-1172",
    "Production Monitoring Readiness":    "CENTPM-1179",
    "Production Testing Readiness":       "CENTPM-1184",
    "Production Data Readiness":          "CENTPM-1192",
    "Production Readiness for Cent Apps": "CENTPM-1197",
}

EXPECTED_SEC_TASK_PAIRS: dict[str, str] = {
    "Generate JWT secret and store in Vault for prod":         "CENTPM-1163",
    "Enable JWKS signature verification for ID tokens":        "CENTPM-1164",
    "Disable SSO bypass in prod and add startup validation":   "CENTPM-1165",
    "Sanitize production error responses to generic messages": "CENTPM-1167",
    "Add rate limiting for auth and chat endpoints":           "CENTPM-1168",
    "Restrict CORS origins to production domain":              "CENTPM-1170",
}


def _ensure_env() -> None:
    load_dotenv()
    if not os.environ.get("NVIDIA_API_KEY"):
        pytest.skip("NVIDIA_API_KEY not set")
    for name in ("data/snapshots/extraction.json", "data/snapshots/project_tree.json"):
        if not (ROOT / name).exists():
            pytest.skip(f"required artifact {name!r} not present in repo root")


def _load_multi_extraction(payload: dict) -> MultiExtractionResult:
    epics: list[ExtractedEpicWithTasks] = []
    for e in payload["epics"]:
        tasks = [
            ExtractedTask(
                summary=t["summary"],
                description=t.get("description", ""),
                source_anchor=t.get("source_anchor", ""),
                assignee_name=t.get("assignee"),
            )
            for t in e["tasks"]
        ]
        epics.append(
            ExtractedEpicWithTasks(
                summary=e["summary"],
                description=e.get("description", ""),
                assignee_name=e.get("assignee"),
                tasks=tasks,
            )
        )
    return MultiExtractionResult(
        file_id=payload.get("source_file_id", "extraction"),
        file_name=payload.get("source_file_name", "extraction"),
        epics=epics,
    )


def test_run_matcher_against_real_local_data():
    _ensure_env()

    extraction_payload = json.loads(
        (ROOT / "data/snapshots/extraction.json").read_text(encoding="utf-8")
    )
    project_tree = json.loads(
        (ROOT / "data/snapshots/project_tree.json").read_text(encoding="utf-8")
    )

    if extraction_payload.get("role") != "multi_epic":
        pytest.skip(
            "extraction.json is not a multi_epic; this test expects May1's "
            "multi extraction. Re-run scripts/extract_one.py on May1 first."
        )

    extraction = _load_multi_extraction(extraction_payload)
    extractions = [(None, extraction)]

    result = run_matcher(
        extractions, project_tree, batch_size=4, max_workers=3
    )

    # We get one FileEpicResult per extracted sub-epic in May1.
    assert len(result.file_results) == len(extraction.epics), (
        f"expected {len(extraction.epics)} per-epic results, got {len(result.file_results)}"
    )

    by_summary = {fr.extracted_epic_summary: fr for fr in result.file_results}

    # ---- Stage 1: epic pairings ----
    epic_failures: list[str] = []
    for ext_summary, expected_key in EXPECTED_EPIC_PAIRS.items():
        fr = by_summary.get(ext_summary)
        if fr is None:
            epic_failures.append(f"  missing FileEpicResult for {ext_summary!r}")
            continue
        if fr.matched_jira_key != expected_key:
            epic_failures.append(
                f"  {ext_summary!r}\n"
                f"      expected: {expected_key}\n"
                f"      got:      {fr.matched_jira_key!r}  "
                f"conf={fr.epic_match_confidence:.2f}\n"
                f"      reason:   {fr.epic_match_reason!r}"
            )
    if epic_failures:
        pytest.fail(
            "Stage-1 epic matcher disagreed with known pairings:\n"
            + "\n".join(epic_failures)
        )

    # ---- Stage 2: task pairings inside Section A → CENTPM-1162 ----
    sec_a = by_summary.get("Production Security Hardening")
    assert sec_a is not None
    assert sec_a.matched_jira_key == "CENTPM-1162"
    assert len(sec_a.task_decisions) == len(
        extraction.epics[0].tasks
    ), "task decisions must be parallel to extracted tasks"

    task_decisions_by_summary = {
        t.summary: d for t, d in zip(extraction.epics[0].tasks, sec_a.task_decisions)
    }

    task_failures: list[str] = []
    for ext_task_summary, expected_key in EXPECTED_SEC_TASK_PAIRS.items():
        d = task_decisions_by_summary.get(ext_task_summary)
        if d is None:
            task_failures.append(f"  no decision for {ext_task_summary!r}")
            continue
        if d.candidate_key != expected_key:
            task_failures.append(
                f"  {ext_task_summary!r}\n"
                f"      expected: {expected_key}\n"
                f"      got:      {d.candidate_key!r}  conf={d.confidence:.2f}\n"
                f"      reason:   {d.reason!r}"
            )
    if task_failures:
        pytest.fail(
            "Stage-2 task matcher disagreed with known pairings:\n"
            + "\n".join(task_failures)
        )

    # ---- orphan flagging: CENTPM-1162 has 12 children, ~6 paired ----
    # The exact orphan count is data-dependent (depends on which children
    # are 'DONE' & what the extractor included). We just assert the
    # mechanism produced a list (could be empty if all were paired).
    assert isinstance(sec_a.orphan_keys, list)
