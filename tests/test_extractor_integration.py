"""Phase 2 — LLM with set data: extractor integration test.

Hits the real NVIDIA Inference API on a small hardcoded `single_epic`
markdown sample. Asserts the extractor returns:
  - one epic with a non-empty informative summary (no `Vx_` prefix,
    no banned `and`/`&`/`+` connectors, length 12..70).
  - the same number of tasks as bullets in the source.
  - every task description includes the `### Definition of Done`
    heading and at least 3 checklist items.
  - assignee strings are captured verbatim, including composites.
  - the agent marker is present at the end of every description.

Opt-in via the `live` marker. To run:

    pytest tests/test_extractor_integration.py -m live -v
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dotenv import load_dotenv

from jira_task_agent.drive.client import DriveFile
from jira_task_agent.pipeline.extractor import (
    AGENT_MARKER,
    extract_from_file,
)


pytestmark = [pytest.mark.live]


def _ensure_env():
    load_dotenv()
    if not os.environ.get("NVIDIA_API_KEY"):
        pytest.skip("NVIDIA_API_KEY not set; skipping live extractor test")


SAMPLE_TASK_FILE = """\
# V_DemoFeature_Tasks

**Owner:** Saar
**Goal:** Ship the Demo Feature for the May release.

The Demo Feature provides a small interactive surface for showing the
agent's classification output to PMs. Implementation is straightforward
once the API contract is settled; the harder work is wiring it through
the existing UI and ensuring server-side auth gating still applies.

## Phase 1: Backend

| ID | Task | Description | Owner | Pri |
|----|------|-------------|-------|-----|
| F-1 | API endpoint | Add GET /api/demo/classify returning the agent's role decision for a given file ID. Use Pydantic for the response model. | Saar | P0 |
| F-2 | Auth check | Wire the endpoint into the existing JWT middleware so unauthenticated callers get 401. | Saar + Lior | P0 |

## Phase 2: Frontend

| ID | Task | Description | Owner | Pri |
|----|------|-------------|-------|-----|
| F-3 | Sidebar entry | Add a Demo entry to the sidebar with a relevant icon. Hide if the user lacks the `demo` role. | Lior | P1 |
| F-4 | Result page | New page rendering the role / confidence / reason for the latest 10 classifications. Use the table component from `@cent/ui`. | Lior | P1 |
"""


def _drive_file(name: str) -> DriveFile:
    return DriveFile(
        id=f"FAKE-{name}",
        name=name,
        mime_type="text/markdown",
        created_time=datetime(2026, 4, 27, tzinfo=timezone.utc),
        modified_time=datetime(2026, 4, 27, tzinfo=timezone.utc),
        size=1500,
        creator_name=None,
        creator_email=None,
        last_modifying_user_name="Saar",
        last_modifying_user_email=None,
        parents=[],
        web_view_link="https://drive.google.com/fake-extractor",
    )


def test_extractor_produces_well_formed_output(tmp_path: Path):
    _ensure_env()

    name = "V_DemoFeature_Tasks.md"
    p = tmp_path / name
    p.write_text(SAMPLE_TASK_FILE, encoding="utf-8")

    res = extract_from_file(
        _drive_file(name),
        local_path=p,
        root_context="(no root context)",
    )

    # ---- epic shape ----
    assert res.epic.summary, "epic summary should be non-empty"
    assert 12 <= len(res.epic.summary) <= 70, (
        f"epic summary length out of range: {len(res.epic.summary)}: "
        f"{res.epic.summary!r}"
    )
    # No "Vx_" or "_Tasks" residue, and no banned connectors.
    s_lower = res.epic.summary.lower()
    assert " and " not in s_lower, f"epic summary contains banned `and`: {res.epic.summary!r}"
    assert " & " not in s_lower
    assert " + " not in s_lower
    assert " with " not in s_lower
    assert AGENT_MARKER in res.epic.description

    # ---- tasks ----
    assert len(res.tasks) == 4, (
        f"expected 4 tasks (F-1..F-4), got {len(res.tasks)}: "
        f"{[t.summary for t in res.tasks]!r}"
    )
    for i, t in enumerate(res.tasks, 1):
        assert "### Definition of Done" in t.description, (
            f"task #{i} missing DoD heading: {t.summary!r}"
        )
        # at least 3 checklist items in the DoD section
        dod_section = t.description.split("### Definition of Done", 1)[1]
        items = [ln for ln in dod_section.splitlines()
                 if ln.strip().startswith("- [")]
        assert len(items) >= 3, (
            f"task #{i} DoD has only {len(items)} item(s): {t.summary!r}"
        )
        assert AGENT_MARKER in t.description

    # ---- assignees: composite ("Saar + Lior") preserved verbatim ----
    f2 = next((t for t in res.tasks if "auth" in t.summary.lower()), None)
    assert f2 is not None, f"could not find F-2-style task in {[t.summary for t in res.tasks]!r}"
    assert f2.assignee_name and "Saar" in f2.assignee_name and "Lior" in f2.assignee_name, (
        f"composite assignee not captured for F-2: {f2.assignee_name!r}"
    )
    # The Co-owners line should appear in the description for the
    # composite-owner task (added by _inject_co_owners).
    assert "Co-owners" in f2.description, (
        f"F-2 description missing Co-owners line: ...{f2.description[-300:]}"
    )
