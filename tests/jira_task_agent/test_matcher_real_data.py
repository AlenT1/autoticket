"""Phase 2 — LLM matcher exercised on REAL local data.

Loads three artifacts captured from prior real runs (no live Drive,
no live Jira reads at test time):

  - epics.json       — all 90 CENTPM project epics (real list_epics output)
  - epic_tree.json   — CENTPM-1162 'Security Hardening' + 9 real children
  - extraction.json  — May1 multi-epic extraction (real LLM extractor output)

The matcher gets fed the same items + candidates that the production
agent would see when reconciling May1 against CENTPM. We assert the
matcher returns the right pairings, with known-correct misses.

Marked `live` (opt-in). Real LLM calls but small set:
  - 1 call for epic-level matching (9 items × 90 candidates)
  - 1 call for task-level matching of Section A's 7 tasks against the 9
    children of CENTPM-1162.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from jira_task_agent.pipeline.matcher import MatchInput, match


pytestmark = [pytest.mark.live]


ROOT = Path(__file__).resolve().parents[2]


# ----------------------------------------------------------------------
# Known-correct pairings, derived by inspection of the real artifacts.
#
# Each entry is "extracted summary" -> expected CENTPM key.
# These are the cases where the agent MUST pair correctly to avoid
# duplicate Jira issues. Misses below would silently double-create.
# ----------------------------------------------------------------------


EXPECTED_EPIC_PAIRS: dict[str, str] = {
    "Production Security Hardening":      "CENTPM-1162",  # "Security Hardening"
    "Production Environment Setup":       "CENTPM-1172",  # exact wording
    "Production Monitoring Readiness":    "CENTPM-1179",  # "Monitoring & Observability"
    "Production Testing Readiness":       "CENTPM-1184",  # "Testing & Quality"
    "Production Data Readiness":          "CENTPM-1192",  # "Data & Content Readiness"
    "Production Readiness for Cent Apps": "CENTPM-1197",  # "CentARB & CentProjects Production Readiness"
}

# May1 Section A (Security Hardening) tasks vs CENTPM-1162's children.
# 6 of 7 extracted tasks have a direct existing-child counterpart; the
# 7th (SEC-3 Azure AD redirect URI) has no equivalent in CENTPM-1162.
EXPECTED_SEC_TASK_PAIRS: dict[str, str] = {
    "Generate JWT secret and store in Vault for prod":           "CENTPM-1163",  # "JWT secret"
    "Enable JWKS signature verification for ID tokens":          "CENTPM-1164",  # "ID token verification"
    "Disable SSO bypass in prod and add startup validation":     "CENTPM-1165",  # "Disable SSO bypass"
    "Sanitize production error responses to generic messages":   "CENTPM-1167",  # "Sanitize error responses"
    "Add rate limiting for auth and chat endpoints":             "CENTPM-1168",  # "Rate limiting"
    "Restrict CORS origins to production domain":                "CENTPM-1170",  # "CORS policy review"
}

EXPECTED_TASK_NONE: set[str] = {
    "Register Azure AD production app redirect URI",  # SEC-3 — no equivalent in CENTPM-1162
}


# ----------------------------------------------------------------------
# loaders
# ----------------------------------------------------------------------


def _ensure_env() -> None:
    load_dotenv()
    if not os.environ.get("NVIDIA_API_KEY"):
        pytest.skip("NVIDIA_API_KEY not set")
    for name in ("data/snapshots/extraction.json", "data/snapshots/epic_tree.json", "data/snapshots/epics.json"):
        if not (ROOT / name).exists():
            pytest.skip(f"required artifact {name!r} not present in repo root")


def _load_json(name: str) -> object:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# tests
# ----------------------------------------------------------------------


def test_matcher_pairs_extracted_epics_to_real_project_epics():
    """May1's 9 extracted sub-epics → real CENTPM epics. 6 of 9 have
    obvious matches (Section A → CENTPM-1162, etc.). The matcher must
    pair those correctly so we adopt instead of duplicating."""
    _ensure_env()
    extraction: dict = _load_json("data/snapshots/extraction.json")  # type: ignore[assignment]
    project_epics: list[dict] = _load_json("data/snapshots/epics.json")  # type: ignore[assignment]

    items = [
        MatchInput(
            summary=e["summary"],
            description=(e.get("description") or "")[:600],
        )
        for e in extraction["epics"]
    ]
    candidates = [
        MatchInput(
            key=e["key"],
            summary=(e.get("summary") or "").strip(),
            description="",
        )
        for e in project_epics
        if e.get("key") and e.get("summary")
    ]

    decisions = match(items=items, candidates=candidates, kind="epic")
    assert len(decisions) == len(items), "one decision per item"

    by_summary = {e["summary"]: d for e, d in zip(extraction["epics"], decisions)}

    failures: list[str] = []
    for ext_summary, expected_key in EXPECTED_EPIC_PAIRS.items():
        d = by_summary.get(ext_summary)
        if d is None:
            failures.append(f"  missing decision for {ext_summary!r}")
            continue
        if d.candidate_key != expected_key:
            failures.append(
                f"  {ext_summary!r}\n"
                f"      expected: {expected_key}\n"
                f"      got:      {d.candidate_key!r}  conf={d.confidence:.2f}\n"
                f"      reason:   {d.reason!r}"
            )

    if failures:
        pytest.fail(
            "Real-data epic matcher disagreed with known-correct pairings:\n"
            + "\n".join(failures)
        )


def test_matcher_pairs_section_a_tasks_to_real_centpm_1162_children():
    """May1 Section A's 7 extracted tasks → real CENTPM-1162 children
    (loaded from epic_tree.json). 6 should match; SEC-3 (Azure AD redirect
    URI) should return None — no equivalent exists in the existing
    children."""
    _ensure_env()
    extraction: dict = _load_json("data/snapshots/extraction.json")  # type: ignore[assignment]
    epic_tree: dict = _load_json("data/snapshots/epic_tree.json")  # type: ignore[assignment]

    section_a = extraction["epics"][0]
    assert section_a["summary"] == "Production Security Hardening", (
        f"unexpected first epic in extraction.json: {section_a['summary']!r}"
    )

    items = [
        MatchInput(
            summary=t["summary"],
            description=(t.get("description") or "")[:600],
        )
        for t in section_a["tasks"]
    ]
    candidates = [
        MatchInput(
            key=c["key"],
            summary=(c.get("summary") or "").strip(),
            description=(c.get("description") or "")[:600],
        )
        for c in epic_tree["children"]
        if c.get("key")
    ]

    decisions = match(items=items, candidates=candidates, kind="task")
    assert len(decisions) == len(items)

    by_summary = {t["summary"]: d for t, d in zip(section_a["tasks"], decisions)}

    failures: list[str] = []
    for ext_summary, expected_key in EXPECTED_SEC_TASK_PAIRS.items():
        d = by_summary.get(ext_summary)
        if d is None:
            failures.append(f"  missing decision for {ext_summary!r}")
            continue
        if d.candidate_key != expected_key:
            failures.append(
                f"  {ext_summary!r}\n"
                f"      expected: {expected_key}\n"
                f"      got:      {d.candidate_key!r}  conf={d.confidence:.2f}\n"
                f"      reason:   {d.reason!r}"
            )

    for ext_summary in EXPECTED_TASK_NONE:
        d = by_summary.get(ext_summary)
        if d is not None and d.candidate_key is not None:
            failures.append(
                f"  {ext_summary!r} should have NO match, got "
                f"{d.candidate_key!r} conf={d.confidence:.2f} reason={d.reason!r}"
            )

    if failures:
        pytest.fail(
            "Real-data task matcher disagreed with known-correct pairings:\n"
            + "\n".join(failures)
        )
