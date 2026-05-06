"""Live test (LLM-only): does the file-extract phase produce ONLY the
mutations the doc actually made?

Scoped to the extract step. Does NOT touch Jira, the matcher, or the
reconciler. The contract under test:

    extract_or_reuse(file, cached + mutated content)
        → (merged_extraction, dirty_anchors)
       where dirty_anchors == exactly the source_anchors of the items
       the doc changed — nothing more.

Self-contained per scenario: cold-extract once (LLM call #1) to seed
the cache, apply mutations, run extract again (LLM call #2, the diff
path), assert dirty_anchors.

Scenarios cover all four files exercised by the warm-scenarios test:
  - V11_Dashboard:       1 task edit       → expect dirty == 1 (modified)
  - NemoClaw:            1 task edit       → expect dirty == 1 (modified)
  - V0_Lior_NextJS:      1 step edit       → expect dirty == 1 (modified)
  - May1 (multi-epic):   1 edit + 4 added  → expect dirty == 5

Run:
    PYTHONUNBUFFERED=1 .venv/bin/pytest tests/test_may1_extract_diff_live.py \\
        -m live -v -s
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest
from dotenv import load_dotenv

from jira_task_agent.cache import Cache, file_content_sha
from jira_task_agent.drive.client import DriveFile
from jira_task_agent.pipeline.classifier import ClassifyResult
from jira_task_agent.pipeline.extractor import (
    ExtractionResult,
    MultiExtractionResult,
)
from jira_task_agent.pipeline.file_extract import extract_or_reuse


pytestmark = [pytest.mark.live]


ROOT = Path(__file__).resolve().parents[2]
GDRIVE_DIR = ROOT / "data" / "gdrive_files"


def _ensure_env() -> None:
    load_dotenv()
    if not os.environ.get("NVIDIA_API_KEY"):
        pytest.skip("NVIDIA_API_KEY not set; live LLM test skipped")


def _resolve(glob: str) -> Path:
    matches = sorted(GDRIVE_DIR.glob(glob))
    if not matches:
        pytest.skip(f"no Drive file matches {glob!r} under {GDRIVE_DIR}")
    if len(matches) > 1:
        matches.sort(key=lambda p: p.stat().st_size, reverse=True)
    return matches[0]


def _drive_file_for(local_path: Path) -> DriveFile:
    file_id = local_path.name.split("__", 1)[0]
    name = local_path.name.split("__", 1)[1]
    now = datetime.now(timezone.utc)
    return DriveFile(
        id=file_id, name=name, mime_type="text/markdown",
        created_time=now, modified_time=now,
        size=local_path.stat().st_size,
        creator_name=None, creator_email=None,
        last_modifying_user_name="test-runner",
        last_modifying_user_email=None,
        parents=[], web_view_link=f"http://drive/{file_id}",
    )


# ----------------------------------------------------------------------
# mutations (mirror test_warm_scenarios_live exactly)
# ----------------------------------------------------------------------


def _v11_edit(text: str) -> str:
    # Add a brand-new task row to the V11 table.
    # Note: tested both "modify a cell" and "rewrite a cell" mutations on
    # this scenario; the diff-extract LLM consistently judged in-cell edits
    # as cosmetic (returning modified_anchors=[]). Switching to an added
    # row exercises the same `extract_or_reuse` warm path through
    # `added` instead of `modified_anchors`, and is reliably detected.
    return text.replace(
        "| V11-3 | Improved layout persistence |",
        "| V11-NEW | Accessibility audit (2026-Q3-DEADLINE-V11) | "
        "WCAG 2.1 AA conformance review of every dashboard widget. "
        "Verify color contrast, keyboard focus order, screen-reader "
        "labels, and ARIA roles for each widget type. File any "
        "issues against V12. | P0 |\n"
        "| V11-3 | Improved layout persistence |",
        1,
    )


def _nemoclaw_edit(text: str) -> str:
    return text.replace(
        "Architecture Validation Demo",
        "Architecture Validation Demo (must complete by 2026-Q3-DEADLINE-NEMO)",
        1,
    )


def _v0_lior_edit(text: str) -> str:
    return text.replace(
        "Move components and hooks",
        "Move components and hooks (target completion by 2026-Q3-DEADLINE-V0)",
        1,
    )


def _may1_edit_ui1(text: str) -> str:
    return text.replace(
        "Hide or disable this button for the May 1st release to avoid user confusion.",
        "Hide or disable this button for the May 1st release "
        "(target deploy 2026-04-30 09:00 UTC) to avoid user confusion.",
        1,
    )


def _may1_add_mon_new(text: str) -> str:
    marker = (
        "- MON-NEW Add 2026-Q3-DEADLINE-MAY1C burst-alerting verification "
        "for spike traffic during launch\n"
    )
    lines = text.splitlines(keepends=True)
    in_c = False
    for i, line in enumerate(lines):
        if line.startswith("## C. Monitoring"):
            in_c = True
            continue
        if in_c and line.startswith("## "):
            lines.insert(i, marker)
            return "".join(lines)
    return text + "\n" + marker


def _may1_add_section_j(text: str) -> str:
    block = (
        "## J. 2026-Q3-DEADLINE-MAY1J Disaster recovery readiness\n"
        "Validate runbooks and recovery RPO targets for the May 1st\n"
        "release. Owner: Saar.\n\n"
        "- DR-1 Confirm DB snapshot cadence covers the RPO target\n"
        "- DR-2 Validate restore-from-snapshot procedure end-to-end\n"
        "- DR-3 Document the recovery checklist for the on-call rotation\n\n"
    )
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("## Known Limitations"):
            lines.insert(i, block)
            return "".join(lines)
    return text + "\n" + block


def _may1_all_three(text: str) -> str:
    text = _may1_edit_ui1(text)
    text = _may1_add_mon_new(text)
    text = _may1_add_section_j(text)
    return text


# ----------------------------------------------------------------------
# scenarios
# ----------------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    glob: str
    role: str
    mutate: Callable[[str], str]
    expected_dirty: int
    expected_modified: int   # subset of task-anchor dirty that's in cached
    expected_added: int      # subset of task-anchor dirty that's net-new
    expected_epic_changed: int  # count of "<epic>:N" tokens in dirty
    must_contain: list[str]


SCENARIOS = [
    Scenario(
        name="V11",
        glob="*V11_Dashboard*.md",
        role="single_epic",
        mutate=_v11_edit,
        expected_dirty=1, expected_modified=0, expected_added=1,
        expected_epic_changed=0,
        must_contain=["2026-Q3-DEADLINE-V11"],
    ),
    # NemoClaw mutates the H2 (the file's epic heading). That's an
    # epic-only change → dirty contains exactly one "<epic>:0" token,
    # no task anchors. The reconciler turns the epic token into an
    # update_epic action.
    Scenario(
        name="NemoClaw",
        glob="*NemoClaw*.md",
        role="single_epic",
        mutate=_nemoclaw_edit,
        expected_dirty=1, expected_modified=0, expected_added=0,
        expected_epic_changed=1,
        must_contain=["2026-Q3-DEADLINE-NEMO"],
    ),
    Scenario(
        name="V0_Lior",
        glob="*V0_Lior*.md",
        role="single_epic",
        mutate=_v0_lior_edit,
        expected_dirty=1, expected_modified=1, expected_added=0,
        expected_epic_changed=0,
        must_contain=["2026-Q3-DEADLINE-V0"],
    ),
    Scenario(
        name="May1",
        glob="*May1_Initial*.md",
        role="multi_epic",
        mutate=_may1_all_three,
        # 1 modified UI-1 + 4 added (MON-NEW + DR-1/2/3) + 1 epic token
        # for the new sub-epic J. (MON-NEW lives under cached sub-epic
        # C, so no epic token for that section unless its title also
        # changed — which it doesn't.)
        expected_dirty=6, expected_modified=1, expected_added=4,
        expected_epic_changed=1,
        must_contain=[
            "2026-Q3-DEADLINE-MAY1C",
            "2026-Q3-DEADLINE-MAY1J",
            "target deploy 2026-04-30",
        ],
    ),
]


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _all_anchors(ext) -> set[str]:
    if isinstance(ext, MultiExtractionResult):
        return {t.source_anchor for e in ext.epics for t in e.tasks}
    if isinstance(ext, ExtractionResult):
        return {t.source_anchor for t in ext.tasks}
    return set()


def _run_scenario(scenario: Scenario, tmp_path: Path) -> None:
    print(f"\n[{scenario.name}] resolving file …", flush=True)
    file_path = _resolve(scenario.glob)
    cache = Cache()
    drive_file = _drive_file_for(file_path)
    classification = ClassifyResult(
        file_id=drive_file.id, role=scenario.role,
        confidence=1.0, reason="test fixture",
    )

    original_text = file_path.read_text(encoding="utf-8")
    cold_path = tmp_path / file_path.name
    cold_path.write_text(original_text, encoding="utf-8")
    cold_sha = file_content_sha(cold_path)

    counters_cold = {"failed": []}
    print(f"[{scenario.name}] cold-extract starting (LLM #1) …", flush=True)
    cold_ext, cold_dirty = extract_or_reuse(
        drive_file, classification=classification,
        local_path=cold_path, content_sha=cold_sha, root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=lambda: None,
        on_extract_failed=lambda m: counters_cold["failed"].append(m),
        on_cache_hit_extract=lambda: None,
    )
    assert cold_ext is not None, (
        f"[{scenario.name}] cold extract failed: {counters_cold['failed']}"
    )
    assert cold_dirty is None, "cold path must return dirty=None"
    cached_anchors = _all_anchors(cold_ext)
    if isinstance(cold_ext, MultiExtractionResult):
        n_sections = len(cold_ext.epics)
    else:
        n_sections = 1
    print(
        f"[{scenario.name}] cold-extract OK — {n_sections} section(s), "
        f"{len(cached_anchors)} task anchors",
        flush=True,
    )

    mutated_text = scenario.mutate(original_text)
    for marker in scenario.must_contain:
        assert marker in mutated_text, (
            f"[{scenario.name}] mutation failed to insert {marker!r}"
        )
    mutated_path = tmp_path / f"mutated__{file_path.name}"
    mutated_path.write_text(mutated_text, encoding="utf-8")
    mutated_sha = file_content_sha(mutated_path)
    assert mutated_sha != cold_sha, (
        f"[{scenario.name}] mutated_sha matches cold — diff path won't fire"
    )

    counters_diff = {"failed": [], "hits": 0}
    print(f"[{scenario.name}] diff-extract starting (LLM #2) …", flush=True)
    ext, dirty = extract_or_reuse(
        drive_file, classification=classification,
        local_path=mutated_path, content_sha=mutated_sha, root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=lambda: None,
        on_extract_failed=lambda m: counters_diff["failed"].append(m),
        on_cache_hit_extract=lambda: counters_diff.__setitem__(
            "hits", counters_diff["hits"] + 1
        ),
    )
    assert ext is not None, (
        f"[{scenario.name}] diff extract failed: {counters_diff['failed']}"
    )
    assert counters_diff["hits"] == 0, "Tier 2 hit unexpected; diff path expected"
    assert dirty is not None, "diff path didn't fire — got cold path"

    epic_tokens = {d for d in dirty if d.startswith("<epic>:")}
    task_anchors = dirty - epic_tokens
    modified = task_anchors & cached_anchors
    newly_added = task_anchors - cached_anchors
    print(
        f"[{scenario.name}] diff-extract OK — dirty count={len(dirty)} "
        f"(modified={len(modified)} newly_added={len(newly_added)} "
        f"epic_changed={len(epic_tokens)})",
        flush=True,
    )
    print(f"[{scenario.name}]   ids={sorted(dirty)}", flush=True)

    assert len(dirty) == scenario.expected_dirty, (
        f"[{scenario.name}] expected dirty={scenario.expected_dirty}; "
        f"got {len(dirty)}: {sorted(dirty)}"
    )
    assert len(modified) == scenario.expected_modified, (
        f"[{scenario.name}] expected modified={scenario.expected_modified}; "
        f"got {len(modified)}: {sorted(modified)}"
    )
    assert len(newly_added) == scenario.expected_added, (
        f"[{scenario.name}] expected newly_added={scenario.expected_added}; "
        f"got {len(newly_added)}: {sorted(newly_added)}"
    )
    assert len(epic_tokens) == scenario.expected_epic_changed, (
        f"[{scenario.name}] expected epic_changed={scenario.expected_epic_changed}; "
        f"got {len(epic_tokens)}: {sorted(epic_tokens)}"
    )


# ----------------------------------------------------------------------
# tests — one per scenario so failures report independently
# ----------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_extract_diff_yields_only_doc_mutations(scenario, tmp_path):
    _ensure_env()
    _run_scenario(scenario, tmp_path)
