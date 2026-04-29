"""Phase 2 — LLM with set data: classifier integration test.

Hits the real NVIDIA Inference API on three small hardcoded markdown
samples — one per role — and asserts the classifier picks the correct
label with confidence ≥ 0.7.

Opt-in via the `live` marker (see pytest.ini), so it doesn't run on
every `pytest`. To run:

    pytest tests/test_classifier_integration.py -m live -v
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dotenv import load_dotenv

from jira_task_agent.drive.client import DriveFile
from jira_task_agent.pipeline.classifier import classify_file


pytestmark = [pytest.mark.live]


def _ensure_env():
    load_dotenv()
    if not os.environ.get("NVIDIA_API_KEY"):
        pytest.skip("NVIDIA_API_KEY not set; skipping live classifier test")


def _drive_file(name: str) -> DriveFile:
    return DriveFile(
        id=f"FAKE-{name}",
        name=name,
        mime_type="text/markdown",
        created_time=datetime(2026, 4, 27, tzinfo=timezone.utc),
        modified_time=datetime(2026, 4, 27, tzinfo=timezone.utc),
        size=1000,
        creator_name=None,
        creator_email=None,
        last_modifying_user_name="Saar",
        last_modifying_user_email=None,
        parents=[],
        web_view_link="https://drive.google.com/fake",
    )


# Hand-crafted samples. Small enough to be cheap; structured enough that
# the role is unambiguous given the classifier's prompt.

SAMPLE_SINGLE_EPIC = """\
# V_Demo_Component_Tasks

**Owner:** Saar
**Goal:** Build the demo widget across all phases.

This document covers a single component (the Demo widget), with the
work split into phases. Each phase delivers a step toward the same
shipping goal.

## Phase 1: scaffolding

| ID | Task | Description | Owner | h | Pri |
|----|------|-------------|-------|---|-----|
| D-1 | Set up component shell | Create the component skeleton + tests | Saar | 2 | P0 |
| D-2 | Wire up routing | Add routes for the component | Saar | 2 | P0 |

## Phase 2: features

| ID | Task | Description | Owner | h | Pri |
|----|------|-------------|-------|---|-----|
| D-3 | Implement core feature | The main interaction loop | Saar | 4 | P0 |
| D-4 | Add error states | Handle network errors gracefully | Saar | 2 | P1 |

## Phase 3: polish

| ID | Task | Description | Owner | h | Pri |
|----|------|-------------|-------|---|-----|
| D-5 | Add loading skeleton | UX polish | Saar | 1 | P1 |
| D-6 | Telemetry hooks | Track widget usage | Saar | 2 | P1 |
"""

SAMPLE_MULTI_EPIC = """\
# Sample Release V99 — production rollout

**Target:** May 30, 2026

This release bundles work across multiple independent components, each
owned by a different lead.

## A. Security Hardening

**Owner:** Sharon

| ID | Task | Owner | Pri |
|----|------|-------|-----|
| SEC-1 | Rotate JWT secret | Sharon | P0 |
| SEC-2 | Restrict CORS origins | Sharon | P0 |
| SEC-3 | Disable bypass flag | Sharon | P0 |

## B. Production Environment

**Owner:** Yuval

| ID | Task | Owner | Pri |
|----|------|-------|-----|
| ENV-1 | Provision PostgreSQL | Yuval | P0 |
| ENV-2 | Configure Vault | Yuval | P0 |

## C. Monitoring

**Owner:** Nick/Joe

| ID | Task | Owner | Pri |
|----|------|-------|-----|
| MON-1 | Health endpoint alerts | Nick/Joe | P0 |
| MON-2 | K8s probes | Nick/Joe | P0 |

## D. Testing

**Owner:** Noam

| ID | Task | Owner | Pri |
|----|------|-------|-----|
| TEST-1 | E2E auth test | Noam | P0 |
| TEST-2 | Smoke test for the chat path | Noam | P0 |
"""

SAMPLE_ROOT = """\
# Quarterly engineering rollup — Q2 highlights

This document summarizes the engineering organisation's deliveries for
the quarter. It does NOT contain a task list of its own. It exists for
context — to give incoming PMs an at-a-glance view of what's
landing in the Cent platform.

## What shipped

- Helios LDAP integration (auto-provisioning of user profiles).
- DB pool metrics on /health.
- Pytest + CI test stage in core.
- FK constraints on FlowRun.
- OAuthState expiry + cleanup.

## Current investments

- CentARB integration into Central V3.
- Frontend migration to Next.js 15.
- Observability dashboards.

## Notes for PMs

The teams are running multiple verticals concurrently; see each
vertical's own `V*_Tasks.md` for the actionable task list.
"""


@pytest.mark.parametrize(
    "name, content, expected_role",
    [
        ("V_Demo_Component_Tasks.md", SAMPLE_SINGLE_EPIC, "single_epic"),
        ("Sample_Release_V99.md", SAMPLE_MULTI_EPIC, "multi_epic"),
        ("Q2_Highlights.md", SAMPLE_ROOT, "root"),
    ],
    ids=["single_epic_sample", "multi_epic_sample", "root_sample"],
)
def test_classifier_assigns_correct_role(
    tmp_path: Path, name: str, content: str, expected_role: str
):
    _ensure_env()

    p = tmp_path / name
    p.write_text(content, encoding="utf-8")

    res = classify_file(
        _drive_file(name),
        local_path=p,
        neighbor_names=[name],
    )

    assert res.role == expected_role, (
        f"expected {expected_role!r} for {name!r}, got {res.role!r} "
        f"(confidence {res.confidence:.2f}, reason: {res.reason!r})"
    )
    assert res.confidence >= 0.70, (
        f"low confidence ({res.confidence:.2f}) on {name!r}: {res.reason!r}"
    )
