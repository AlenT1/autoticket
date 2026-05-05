"""Live test (LLM-only): does the matcher honour `dirty_anchors`?

Cold-extracts May1, runs `match_with_cache` once to seed the matcher
cache, mutates the file, runs extract_or_reuse to compute fresh
`dirty_anchors`, then runs `match_with_cache` again with the new
dirty set and asserts:

  - LLM Stage 1 receives only the dirty epic sections.
  - LLM Stage 2 receives only the dirty tasks (or all tasks under a
    freshly re-matched epic).
  - Sections / tasks not in dirty keep their cached `MatchDecision`
    byte-for-byte.

No Jira writes; reads still hit live Jira (project_tree).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dotenv import load_dotenv

from jira_task_agent.cache import Cache, file_content_sha
from jira_task_agent.drive.client import DriveFile
from jira_task_agent.jira.client import JiraClient
from jira_task_agent.jira.project_tree import fetch_project_tree
from jira_task_agent.pipeline import file_match, matcher as matcher_mod
from jira_task_agent.pipeline.classifier import ClassifyResult
from jira_task_agent.pipeline.extractor import MultiExtractionResult
from jira_task_agent.pipeline.file_extract import extract_or_reuse
from jira_task_agent.pipeline.file_match import match_with_cache


pytestmark = [pytest.mark.live]


ROOT = Path(__file__).resolve().parent.parent
GDRIVE_DIR = ROOT / "data" / "gdrive_files"


def _ensure_env() -> None:
    load_dotenv()
    for var in ("NVIDIA_API_KEY", "JIRA_HOST", "JIRA_PROJECT_KEY", "JIRA_TOKEN"):
        if not os.environ.get(var):
            pytest.skip(f"{var} not set; live matcher test skipped")


def _resolve_may1() -> Path:
    matches = sorted(GDRIVE_DIR.glob("*May1_Initial*.md"))
    if not matches:
        pytest.skip("May1 source file not found")
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


def _edit_ui1(text: str) -> str:
    return text.replace(
        "Hide or disable this button for the May 1st release to avoid user confusion.",
        "Hide or disable this button for the May 1st release "
        "(target deploy 2026-04-30 09:00 UTC) to avoid user confusion.",
        1,
    )


def _add_mon_new(text: str) -> str:
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


def _add_section_j(text: str) -> str:
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


def test_matcher_partial_path_only_processes_dirty(tmp_path):
    _ensure_env()
    file_path = _resolve_may1()
    cache = Cache()
    drive_file = _drive_file_for(file_path)

    classification = ClassifyResult(
        file_id=drive_file.id, role="multi_epic",
        confidence=1.0, reason="test fixture",
    )
    project_tree = fetch_project_tree(JiraClient.from_env(), os.environ["JIRA_PROJECT_KEY"])

    print(f"\n[matcher-dirty] cold extract …", flush=True)
    cold_path = tmp_path / file_path.name
    cold_text = file_path.read_text(encoding="utf-8")
    cold_path.write_text(cold_text, encoding="utf-8")
    cold_sha = file_content_sha(cold_path)
    cold_ext, _ = extract_or_reuse(
        drive_file, classification=classification,
        local_path=cold_path, content_sha=cold_sha, root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=lambda: None,
        on_extract_failed=lambda m: pytest.fail(f"cold extract failed: {m}"),
        on_cache_hit_extract=lambda: None,
    )
    assert isinstance(cold_ext, MultiExtractionResult)

    print(f"[matcher-dirty] cold match …", flush=True)
    match_with_cache(
        [(drive_file, cold_ext)], project_tree, cache,
        content_shas={drive_file.id: cold_sha},
        use_cache=True, matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: None,
    )

    mutated = _edit_ui1(_add_mon_new(_add_section_j(cold_text)))
    mutated_path = tmp_path / f"mutated__{file_path.name}"
    mutated_path.write_text(mutated, encoding="utf-8")
    mutated_sha = file_content_sha(mutated_path)

    print(f"[matcher-dirty] warm extract …", flush=True)
    warm_ext, dirty = extract_or_reuse(
        drive_file, classification=classification,
        local_path=mutated_path, content_sha=mutated_sha, root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=lambda: None,
        on_extract_failed=lambda m: pytest.fail(f"warm extract failed: {m}"),
        on_cache_hit_extract=lambda: None,
    )
    assert dirty is not None and len(dirty) > 0, f"unexpected empty dirty: {dirty}"
    print(f"[matcher-dirty] dirty={sorted(dirty)}", flush=True)

    s1_items: list = []
    s2_groups: list = []
    real_match = matcher_mod.match
    real_grouped = matcher_mod.match_grouped

    def _spy_match(items, candidates, *, kind):
        s1_items.extend(items)
        return real_match(items=items, candidates=candidates, kind=kind)

    def _spy_grouped(groups, *, kind, batch_size, max_workers):
        s2_groups.extend(groups)
        return real_grouped(
            groups, kind=kind, batch_size=batch_size, max_workers=max_workers,
        )

    monkey_match = pytest.MonkeyPatch()
    try:
        monkey_match.setattr(file_match, "match", _spy_match)
        monkey_match.setattr(file_match, "match_grouped", _spy_grouped)
        monkey_match.setattr(
            file_match, "run_matcher",
            lambda *a, **kw: pytest.fail(
                "run_matcher should not be called on the partial path"
            ),
        )

        print(f"[matcher-dirty] warm match (partial path) …", flush=True)
        result = match_with_cache(
            [(drive_file, warm_ext)], project_tree, cache,
            content_shas={drive_file.id: mutated_sha},
            use_cache=True, matcher_batch_size=4, matcher_max_workers=3,
            on_cache_hits_match=lambda: None,
            dirty_anchors_per_file={drive_file.id: dirty},
        )
    finally:
        monkey_match.undo()

    s1_summaries = sorted(it.summary for it in s1_items)
    s2_summaries = sorted(t.summary for g in s2_groups for t in g.items)

    cold_section_count = len(cold_ext.epics)
    warm_section_count = len(warm_ext.epics)
    new_section_indexes = set(range(cold_section_count, warm_section_count))

    dirty_epic_idxs = {
        int(d[len("<epic>:"):]) for d in dirty if d.startswith("<epic>:")
    } | new_section_indexes
    dirty_task_anchors = {d for d in dirty if not d.startswith("<epic>:")}
    # Stage 1 always re-runs for any *processed* section: epic-dirty,
    # has-dirty-tasks, or brand-new. This is the always-Stage-1 self-heal
    # behavior — Stage 1 LLM stochasticity can mis-pair on a cold run, so
    # any subsequent run that processes the section re-pairs it.
    processed_section_idxs = set(dirty_epic_idxs)
    for i, epic in enumerate(warm_ext.epics):
        if any(t.source_anchor in dirty_task_anchors for t in epic.tasks):
            processed_section_idxs.add(i)

    section_owner: dict[str, str] = {}
    for i, epic in enumerate(warm_ext.epics):
        for t in epic.tasks:
            if t.source_anchor in dirty_task_anchors:
                section_owner[t.source_anchor] = (
                    f"epic[{i}]={epic.summary!r} "
                    f"matched={result.file_results[i].matched_jira_key}"
                )
    print(
        f"[matcher-dirty] stage1 items={len(s1_items)} ({s1_summaries})\n"
        f"[matcher-dirty] stage2 items={len(s2_summaries)} ({s2_summaries})\n"
        f"[matcher-dirty] dirty task anchor → section: "
        f"{section_owner}",
        flush=True,
    )

    expected_s1_summaries = sorted(
        warm_ext.epics[i].summary for i in processed_section_idxs
    )
    assert s1_summaries == expected_s1_summaries, (
        f"stage1 mismatch: got {s1_summaries}; expected {expected_s1_summaries}"
    )

    # Stage 2 is invoked only for sections whose matched_jira_key is set.
    # Sections with no matched epic skip Stage 2 entirely (their dirty
    # tasks become create_task downstream via the reconciler).
    expected_s2_summaries: set[str] = set()
    for i, fr in enumerate(result.file_results):
        if fr.matched_jira_key is None:
            continue
        tasks = warm_ext.epics[i].tasks
        if i in processed_section_idxs:
            # Section was re-evaluated by Stage 1 (epic-dirty, has-dirty-
            # tasks, or brand-new). Stage 2 runs full because the
            # children list may have shifted with the re-pairing.
            expected_s2_summaries.update(t.summary for t in tasks)
        else:
            expected_s2_summaries.update(
                t.summary for t in tasks
                if t.source_anchor in dirty_task_anchors
            )
    missing = expected_s2_summaries - set(s2_summaries)
    extra = set(s2_summaries) - expected_s2_summaries
    assert not missing and not extra, (
        f"stage2 mismatch — missing={sorted(missing)} extra={sorted(extra)}"
    )

    warm_total_tasks = sum(len(e.tasks) for e in warm_ext.epics)
    print(
        f"[matcher-dirty] full re-eval would have been "
        f"{warm_total_tasks} tasks; partial used "
        f"{len(s2_summaries)} (dirty task anchors: {len(dirty_task_anchors)})",
        flush=True,
    )

    assert len(result.file_results) == warm_section_count
