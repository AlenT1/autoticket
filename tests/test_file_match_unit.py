"""Unit tests for pipeline.file_match.

Covers:
  - Tier 3 (file-level) cache hit + soft fallback for stale matched_jira_key
  - Per-task cache: only dirty tasks fed to Stage 2; clean tasks reuse
    cached MatchDecisions
  - Schema-mismatch fallback (epic count differs from cached results)
  - No-cache path: full matcher invoked

The matcher's `chat` and `match_grouped` are stubbed; no network.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from jira_task_agent.cache import Cache, serialize_extraction
from jira_task_agent.drive.client import DriveFile
from jira_task_agent.pipeline import file_match, matcher
from jira_task_agent.pipeline.extractor import (
    AGENT_MARKER,
    ExtractedEpic,
    ExtractedEpicWithTasks,
    ExtractedTask,
    ExtractionResult,
    MultiExtractionResult,
)
from jira_task_agent.pipeline.file_extract import _compute_diff_payload
from jira_task_agent.pipeline.file_match import match_with_cache
from jira_task_agent.pipeline.matcher import (
    FileEpicResult,
    GroupResult,
    MatchDecision,
    MatcherResult,
    compute_matcher_prompt_sha,
    file_epic_result_to_json,
)


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------


def _df(file_id: str = "F1", name: str = "V11.md") -> DriveFile:
    return DriveFile(
        id=file_id,
        name=name,
        mime_type="text/markdown",
        created_time=datetime.now(timezone.utc),
        modified_time=datetime.now(timezone.utc),
        size=100,
        creator_name="Aviv R",
        creator_email=None,
        last_modifying_user_name="Aviv R",
        last_modifying_user_email=None,
        parents=[],
        web_view_link="http://drive/F1",
    )


def _task_desc() -> str:
    return (
        f"body\n\n### Definition of Done\n"
        "- [ ] one\n- [ ] two\n- [ ] three\n\n"
        f"{AGENT_MARKER}"
    )


def _t(summary: str, anchor: str) -> ExtractedTask:
    return ExtractedTask(summary=summary, description=_task_desc(), source_anchor=anchor)


def _single(tasks: list[ExtractedTask]) -> ExtractionResult:
    return ExtractionResult(
        file_id="F1",
        file_name="V11.md",
        epic=ExtractedEpic(
            summary="Dashboard work",
            description=f"x\n\n{AGENT_MARKER}",
        ),
        tasks=tasks,
    )


def _project_tree(epics: list[dict]) -> dict:
    return {
        "project_key": "CENTPM",
        "epic_count": len(epics),
        "child_count": sum(len(e.get("children") or []) for e in epics),
        "orphan_count": 0,
        "epics": epics,
    }


def _file_epic_result(
    file_id: str,
    matched_key: str | None,
    decisions: list[MatchDecision],
) -> FileEpicResult:
    return FileEpicResult(
        file_id=file_id,
        file_name="V11.md",
        section_index=0,
        extracted_epic_summary="Dashboard work",
        extracted_epic_description="x",
        extracted_epic_assignee_raw=None,
        matched_jira_key=matched_key,
        epic_match_confidence=0.95,
        epic_match_reason="cached",
        task_decisions=decisions,
        orphan_keys=[],
    )


def _seed_cache_full(
    cache: Cache,
    file_id: str,
    file_text: str,
    ext: ExtractionResult | MultiExtractionResult,
    matched_key: str | None,
    task_decisions: list[MatchDecision],
) -> None:
    """Populate every layer of the cache for a previously-seen file."""
    content_sha = "OLD-SHA"
    cache.set_classification(
        file_id=file_id, modified_time="t", content_sha=content_sha,
        role="single_epic" if isinstance(ext, ExtractionResult) else "multi_epic",
        confidence=1.0, reason="x",
    )
    cache.set_extraction(
        file_id=file_id, modified_time="t", content_sha=content_sha,
        extraction_payload=serialize_extraction(ext),
    )
    diff = _compute_diff_payload(ext, file_text)
    cache.set_diff_payload(
        file_id=file_id,
        chunks=diff["chunks"],
        task_anchors=diff["task_anchors"],
    )
    fr = _file_epic_result(file_id, matched_key, task_decisions)
    cache.set_match(
        file_id=file_id,
        modified_time="t",
        content_sha=content_sha,
        prompt_sha=compute_matcher_prompt_sha(),
        topology_sha="ANY",
        results=[file_epic_result_to_json(fr)],
    )


# ----------------------------------------------------------------------
# Tier 3 file-level cache
# ----------------------------------------------------------------------


def test_tier3_hit_returns_cached_results_no_llm(tmp_path, monkeypatch):
    cache = Cache()
    text = "# T\n## A\nSEC-1 generate JWT secret in vault.\n"
    p = tmp_path / "f.md"
    p.write_text(text)

    ext = _single([_t("Generate JWT secret task", "SEC-1 generate JWT secret")])
    decisions = [MatchDecision(item_index=0, candidate_key="CENTPM-101", confidence=0.9, reason="ok")]
    _seed_cache_full(cache, "F1", text, ext, "CENTPM-100", decisions)

    project = _project_tree([
        {"key": "CENTPM-100", "summary": "Security", "description": "",
         "children": [{"key": "CENTPM-101", "summary": "JWT", "status": "Backlog", "description": ""}]},
    ])

    # Any LLM call here would be a bug.
    monkeypatch.setattr(
        matcher, "chat",
        lambda **_: (_ for _ in ()).throw(AssertionError("matcher should not call LLM")),
    )

    hits = {"n": 0}
    def _hit():
        hits["n"] += 1
    result = match_with_cache(
        [(_df(), ext)],
        project,
        cache,
        content_shas={"F1": "OLD-SHA"},
        local_paths={"F1": p},
        use_cache=True,
        matcher_batch_size=4,
        matcher_max_workers=3,
        on_cache_hits_match=_hit,
    )
    assert hits["n"] == 1
    assert len(result.file_results) == 1
    assert result.file_results[0].matched_jira_key == "CENTPM-100"
    assert result.file_results[0].task_decisions[0].candidate_key == "CENTPM-101"


def test_tier3_stale_matched_key_drops_and_refalls_back_to_full(tmp_path, monkeypatch):
    """Cached matched_jira_key was deleted from Jira → drop, fall back."""
    cache = Cache()
    text = "# T\n## A\nALPHA generate JWT secret.\n"
    p = tmp_path / "f.md"
    p.write_text(text)
    ext = _single([_t("Task one", "ALPHA generate JWT")])
    _seed_cache_full(
        cache, "F1", text, ext, "CENTPM-DELETED",
        [MatchDecision(0, "CENTPM-101", 0.9, "ok")],
    )

    project = _project_tree([
        # CENTPM-DELETED no longer exists.
        {"key": "CENTPM-200", "summary": "Other", "description": "", "children": []},
    ])

    # Stub the matcher's full run.
    fresh = MatcherResult(file_results=[
        _file_epic_result("F1", None, [MatchDecision(0, None, 0.0, "no match")])
    ])
    monkeypatch.setattr(file_match, "run_matcher", lambda *a, **kw: fresh)

    hits = {"n": 0}
    result = match_with_cache(
        [(_df(), ext)], project, cache,
        content_shas={"F1": "OLD-SHA"},
        local_paths={"F1": p},
        use_cache=True,
        matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: hits.__setitem__("n", hits["n"] + 1),
    )
    # Did not count as a cache hit (we re-ran).
    assert hits["n"] == 0
    assert result.file_results[0].matched_jira_key is None


# ----------------------------------------------------------------------
# Per-task cache (partial Stage 2)
# ----------------------------------------------------------------------


def test_per_task_cache_only_dirty_tasks_run_stage2(tmp_path, monkeypatch):
    """File content changed; one task's chunk changed and one didn't.
    Stage 2 should run only on the dirty task."""
    cache = Cache()
    old_text = (
        "# T\n\n"
        "## A. Section\nALPHA generate JWT secret in vault.\n\n"
        "## B. Section\nBRAVO configure rate limiting policy.\n"
    )
    new_text = (
        "# T\n\n"
        "## A. Section\nALPHA generate JWT secret EDITED in vault.\n\n"
        "## B. Section\nBRAVO configure rate limiting policy.\n"
    )
    p = tmp_path / "f.md"
    p.write_text(new_text)

    ext_old = _single([
        _t("JWT secret task", "ALPHA generate JWT"),
        _t("Rate limit task", "BRAVO configure rate"),
    ])
    decisions_cached = [
        MatchDecision(0, "CENTPM-101", 0.95, "cached A"),
        MatchDecision(1, "CENTPM-102", 0.92, "cached B"),
    ]
    _seed_cache_full(cache, "F1", old_text, ext_old, "CENTPM-100", decisions_cached)

    # The current run is the SAME extraction (e.g. tasks unchanged from
    # the merged perspective). Tier 3 will MISS because content_sha
    # changed. Per-task path should kick in.
    ext_new = _single([
        _t("JWT secret task EDITED", "ALPHA generate JWT EDITED"),  # dirty
        _t("Rate limit task", "BRAVO configure rate"),                # clean
    ])

    project = _project_tree([
        {"key": "CENTPM-100", "summary": "Security", "description": "", "children": [
            {"key": "CENTPM-101", "summary": "JWT", "status": "Backlog", "description": ""},
            {"key": "CENTPM-102", "summary": "Rate limiting", "status": "Backlog", "description": ""},
        ]},
    ])

    # Stub match_grouped so we can assert which items were sent.
    sent_groups: list = []
    def _fake_match_grouped(groups, *, kind, batch_size, max_workers):
        sent_groups.extend(groups)
        # Return a fresh decision for the dirty task.
        out = []
        for g in groups:
            decisions = [
                MatchDecision(i, "CENTPM-101", 0.99, f"refreshed {i}")
                for i in range(len(g.items))
            ]
            out.append(GroupResult(group_id=g.group_id, decisions=decisions))
        return out
    monkeypatch.setattr(file_match, "match_grouped", _fake_match_grouped)

    # `run_matcher` would be called only if the per-task path bails out.
    monkeypatch.setattr(
        file_match, "run_matcher",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("run_matcher should not be called; per-task path expected")
        ),
    )

    result = match_with_cache(
        [(_df(), ext_new)], project, cache,
        content_shas={"F1": "NEW-SHA"},  # different from cached "OLD-SHA"
        local_paths={"F1": p},
        use_cache=True,
        matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: None,
    )

    # match_grouped called exactly once with ONE group containing ONE item
    # (the dirty task). The clean task was reused from cache.
    assert len(sent_groups) == 1
    assert len(sent_groups[0].items) == 1
    assert "EDITED" in sent_groups[0].items[0].summary

    fr = result.file_results[0]
    # The clean task kept its cached decision (CENTPM-102).
    assert fr.task_decisions[1].candidate_key == "CENTPM-102"
    assert fr.task_decisions[1].reason == "cached B"
    # The dirty task got a refreshed decision.
    assert fr.task_decisions[0].candidate_key == "CENTPM-101"
    assert "refreshed" in fr.task_decisions[0].reason


def test_per_task_cache_all_clean_no_llm(tmp_path, monkeypatch):
    """File content_sha changed but NO chunk changed (whitespace tail).
    Per-task path should fall through (return None) so caller falls back
    to full match."""
    cache = Cache()
    text = "# T\n## A\nALPHA generate JWT secret.\n"
    p = tmp_path / "f.md"
    p.write_text(text)

    ext = _single([_t("JWT secret task", "ALPHA generate JWT")])
    _seed_cache_full(cache, "F1", text, ext, "CENTPM-100", [
        MatchDecision(0, "CENTPM-101", 0.95, "cached"),
    ])

    project = _project_tree([
        {"key": "CENTPM-100", "summary": "Security", "description": "", "children": [
            {"key": "CENTPM-101", "summary": "JWT", "status": "Backlog", "description": ""},
        ]},
    ])

    # Per-task path returns None when no chunks changed; runner uses
    # run_matcher for a fresh full match.
    fresh = MatcherResult(file_results=[
        _file_epic_result("F1", "CENTPM-100", [
            MatchDecision(0, "CENTPM-101", 0.95, "fresh fallback")
        ])
    ])
    monkeypatch.setattr(file_match, "run_matcher", lambda *a, **kw: fresh)

    result = match_with_cache(
        [(_df(), ext)], project, cache,
        # Different sha but same content (Tier 3 misses on prompt_sha
        # comparison? Actually prompt_sha is identical, content_sha is
        # different — so Tier 3 misses; per-task path tries; finds no
        # changed chunks; falls back; full run_matcher invoked).
        content_shas={"F1": "DIFFERENT-SHA"},
        local_paths={"F1": p},
        use_cache=True,
        matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: None,
    )
    assert result.file_results[0].task_decisions[0].reason == "fresh fallback"


def test_per_task_cache_schema_mismatch_falls_back(tmp_path, monkeypatch):
    """Cached has 1 epic-result; current extraction has 2 sub-epics
    (multi_epic with new section). Per-task path can't safely splice
    → returns None → full match."""
    cache = Cache()
    text = "# T\n\n## A. Sec\nALPHA-task body.\n"
    p = tmp_path / "f.md"
    p.write_text(text)

    ext_old = _single([_t("Old task", "ALPHA-task body")])
    _seed_cache_full(cache, "F1", text, ext_old, "CENTPM-100", [
        MatchDecision(0, "CENTPM-101", 0.95, "cached"),
    ])

    # Current extraction has TWO sub-epics — schema mismatch with the 1-FR cache.
    ext_new = MultiExtractionResult(
        file_id="F1", file_name="May1.md",
        epics=[
            ExtractedEpicWithTasks(
                summary="E1", description=f"x\n\n{AGENT_MARKER}",
                assignee_name=None, tasks=[_t("E1 t1", "E1 anchor t1")],
            ),
            ExtractedEpicWithTasks(
                summary="E2", description=f"x\n\n{AGENT_MARKER}",
                assignee_name=None, tasks=[_t("E2 t1", "E2 anchor t1")],
            ),
        ],
    )

    project = _project_tree([
        {"key": "CENTPM-100", "summary": "Security", "description": "", "children": []},
    ])

    fresh_called = {"n": 0}
    def _fake_run(*a, **kw):
        fresh_called["n"] += 1
        return MatcherResult(file_results=[
            _file_epic_result("F1", None, []),
            _file_epic_result("F1", None, []),
        ])
    monkeypatch.setattr(file_match, "run_matcher", _fake_run)

    result = match_with_cache(
        [(_df(), ext_new)], project, cache,
        content_shas={"F1": "NEW-SHA"},
        local_paths={"F1": p},
        use_cache=True,
        matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: None,
    )
    assert fresh_called["n"] == 1  # full match invoked


# ----------------------------------------------------------------------
# No-cache path
# ----------------------------------------------------------------------


def test_use_cache_false_runs_full_matcher(tmp_path, monkeypatch):
    cache = Cache()
    p = tmp_path / "f.md"
    p.write_text("# T\n## A\nALPHA-task.\n")
    ext = _single([_t("Some task", "ALPHA-task")])

    project = _project_tree([
        {"key": "CENTPM-100", "summary": "Security", "description": "", "children": []},
    ])

    fresh = MatcherResult(file_results=[
        _file_epic_result("F1", None, [MatchDecision(0, None, 0.0, "fresh, no cache")])
    ])
    monkeypatch.setattr(file_match, "run_matcher", lambda *a, **kw: fresh)

    hits = {"n": 0}
    result = match_with_cache(
        [(_df(), ext)], project, cache,
        content_shas={"F1": "X"},
        local_paths={"F1": p},
        use_cache=False,
        matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: hits.__setitem__("n", hits["n"] + 1),
    )
    assert hits["n"] == 0
    assert result.file_results[0].task_decisions[0].reason == "fresh, no cache"


def test_per_task_cache_dirty_under_no_matched_epic(tmp_path, monkeypatch):
    """File previously had no matched epic (cached as create_epic). On
    a re-run with content changes, dirty tasks are flagged as
    'no candidate match' rather than fed through Stage 2 (no candidates
    to match against)."""
    cache = Cache()
    old_text = "# T\n\n## A. Section\nALPHA generate something specific.\n"
    new_text = "# T\n\n## A. Section\nALPHA generate something specific EDITED.\n"
    p = tmp_path / "f.md"
    p.write_text(new_text)
    ext_old = _single([_t("Some specific task", "ALPHA generate something specific")])
    _seed_cache_full(cache, "F1", old_text, ext_old, None, [
        MatchDecision(0, None, 0.0, "no match cached"),
    ])

    ext_new = _single([_t("Some specific task EDITED", "ALPHA generate something EDITED")])
    project = _project_tree([
        {"key": "CENTPM-200", "summary": "Other", "description": "", "children": []},
    ])

    # If the partial path tries to call match_grouped, that's fine —
    # but for a None-matched-epic file, partial path should NOT call
    # match_grouped (no candidates) and should just mark dirty tasks
    # with candidate_key=None.
    monkeypatch.setattr(
        file_match, "match_grouped",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("match_grouped must not be called for None-matched epic")
        ),
    )
    # run_matcher must not be called either — the per-task path handles it.
    monkeypatch.setattr(
        file_match, "run_matcher",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("run_matcher must not be called; per-task handled it")
        ),
    )

    result = match_with_cache(
        [(_df(), ext_new)], project, cache,
        content_shas={"F1": "NEW-SHA"},
        local_paths={"F1": p},
        use_cache=True,
        matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: None,
    )
    fr = result.file_results[0]
    assert fr.matched_jira_key is None
    # The dirty task got an updated decision with candidate_key=None
    # and a reason indicating new task in changed chunk.
    assert fr.task_decisions[0].candidate_key is None
    assert "new task in changed chunk" in fr.task_decisions[0].reason


def test_per_task_cache_corrupted_match_payload_falls_back(tmp_path, monkeypatch):
    """A cached matcher result that fails to deserialize causes the
    Tier 3 hit path to bail; runner should fall through to a fresh
    full match."""
    cache = Cache()
    text = "# T\n## A\nALPHA-task.\n"
    p = tmp_path / "f.md"
    p.write_text(text)
    ext = _single([_t("Some task", "ALPHA-task")])
    _seed_cache_full(cache, "F1", text, ext, "CENTPM-100", [
        MatchDecision(0, "CENTPM-101", 0.95, "cached"),
    ])
    # Corrupt the cached results so file_epic_result_from_json fails.
    cache.files["F1"].matcher_payload["results"] = [{"BROKEN": True}]

    project = _project_tree([
        {"key": "CENTPM-100", "summary": "Security", "description": "", "children": []},
    ])

    fresh = MatcherResult(file_results=[
        _file_epic_result("F1", None, [MatchDecision(0, None, 0.0, "fresh after corrupt")])
    ])
    monkeypatch.setattr(file_match, "run_matcher", lambda *a, **kw: fresh)

    result = match_with_cache(
        [(_df(), ext)], project, cache,
        content_shas={"F1": "OLD-SHA"},
        local_paths={"F1": p},
        use_cache=True,
        matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: None,
    )
    assert result.file_results[0].task_decisions[0].reason == "fresh after corrupt"


def test_empty_extractions_returns_empty_result():
    cache = Cache()
    project = _project_tree([])
    result = match_with_cache(
        [], project, cache,
        content_shas={}, local_paths={},
        use_cache=True,
        matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: None,
    )
    assert result.file_results == []
