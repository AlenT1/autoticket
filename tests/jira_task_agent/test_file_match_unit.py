"""Unit tests for `pipeline.file_match`.

Three branches of `match_with_cache` covered with stubbed LLM:

  - cache hit (file unchanged + prompts unchanged)
  - partial (cache exists + dirty_anchors set; only dirty items re-run)
  - fresh (no usable cache)

Stale `matched_jira_key` invalidation is also covered.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from jira_task_agent.cache import Cache, serialize_extraction
from jira_task_agent.drive.client import DriveFile
from jira_task_agent.pipeline import file_match
from jira_task_agent.pipeline.extractor import (
    AGENT_MARKER,
    ExtractedEpic,
    ExtractedEpicWithTasks,
    ExtractedTask,
    ExtractionResult,
    MultiExtractionResult,
)
from jira_task_agent.pipeline.file_match import match_with_cache
from jira_task_agent.pipeline.matcher import (
    FileEpicResult,
    GroupResult,
    MatchDecision,
    MatcherResult,
    compute_matcher_prompt_sha,
    file_epic_result_to_json,
)


def _df(file_id: str = "F1", name: str = "F.md") -> DriveFile:
    return DriveFile(
        id=file_id, name=name, mime_type="text/markdown",
        created_time=datetime.now(timezone.utc),
        modified_time=datetime.now(timezone.utc),
        size=100,
        creator_name=None, creator_email=None,
        last_modifying_user_name=None, last_modifying_user_email=None,
        parents=[], web_view_link=f"http://drive/{file_id}",
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
        file_id="F1", file_name="F.md",
        epic=ExtractedEpic(summary="Dashboard work", description=f"x\n\n{AGENT_MARKER}"),
        tasks=tasks,
    )


def _multi(epics: list[tuple[str, list[ExtractedTask]]]) -> MultiExtractionResult:
    return MultiExtractionResult(
        file_id="F1", file_name="F.md",
        epics=[
            ExtractedEpicWithTasks(
                summary=s, description=f"d\n\n{AGENT_MARKER}",
                assignee_name=None, tasks=ts,
            )
            for s, ts in epics
        ],
    )


def _project(epics: list[dict]) -> dict:
    return {
        "project_key": "CENTPM",
        "epic_count": len(epics),
        "child_count": sum(len(e.get("children") or []) for e in epics),
        "orphan_count": 0,
        "epics": epics,
    }


def _fr(
    file_id: str,
    section_index: int,
    matched_key: str | None,
    decisions: list[MatchDecision],
    anchors: list[str | None] | None = None,
    *,
    epic_summary: str = "Dashboard work",
    epic_description: str = "x",
) -> FileEpicResult:
    return FileEpicResult(
        file_id=file_id, file_name="F.md", section_index=section_index,
        extracted_epic_summary=epic_summary,
        extracted_epic_description=epic_description,
        extracted_epic_assignee_raw=None,
        matched_jira_key=matched_key,
        epic_match_confidence=0.95 if matched_key else 0.0,
        epic_match_reason="cached" if matched_key else "no match",
        task_decisions=decisions,
        task_anchors=list(anchors or [None] * len(decisions)),
        orphan_keys=[],
    )


def _seed(
    cache: Cache,
    file_id: str,
    ext,
    file_results: list[FileEpicResult],
    *,
    content_sha: str = "OLD",
) -> None:
    role = "single_epic" if isinstance(ext, ExtractionResult) else "multi_epic"
    cache.set_classification(
        file_id=file_id, modified_time="t", content_sha=content_sha,
        role=role, confidence=1.0, reason="x",
    )
    cache.set_extraction(
        file_id=file_id, modified_time="t", content_sha=content_sha,
        extraction_payload=serialize_extraction(ext),
    )
    cache.set_match(
        file_id=file_id, modified_time="t", content_sha=content_sha,
        prompt_sha=compute_matcher_prompt_sha(),
        topology_sha="ANY",
        results=[file_epic_result_to_json(fr) for fr in file_results],
    )


def _bomb(*a, **kw):
    raise AssertionError("unexpected matcher LLM call")


def test_cache_hit_reuses_cached_results(monkeypatch):
    cache = Cache()
    ext = _single([_t("T", "anchor-T")])
    cached_fr = _fr("F1", 0, "CENTPM-100",
                    [MatchDecision(0, "CENTPM-101", 0.95, "cached")])
    _seed(cache, "F1", ext, [cached_fr])

    project = _project([
        {"key": "CENTPM-100", "summary": "Sec", "description": "", "children": [
            {"key": "CENTPM-101", "summary": "T", "status": "Backlog", "description": ""},
        ]},
    ])

    monkeypatch.setattr(file_match, "match", _bomb)
    monkeypatch.setattr(file_match, "match_grouped", _bomb)
    monkeypatch.setattr(file_match, "run_matcher", _bomb)

    hits = {"n": 0}
    result = match_with_cache(
        [(_df(), ext)], project, cache,
        content_shas={"F1": "OLD"},
        use_cache=True, matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: hits.__setitem__("n", hits["n"] + 1),
    )
    assert hits["n"] == 1
    assert result.file_results[0].matched_jira_key == "CENTPM-100"


def test_partial_only_dirty_task_reruns_stage1_and_dirty_stage2(monkeypatch):
    """Even when only a task is dirty, Stage 1 re-pairs the section's
    epic against the full project tree (so a cached miss can self-heal).
    Stage 2 runs only for the dirty task when Stage 1 confirms the
    cached match."""
    cache = Cache()
    ext = _single([_t("T1", "a1"), _t("T2", "a2")])
    cached_decisions = [
        MatchDecision(0, "CENTPM-101", 0.95, "cached A"),
        MatchDecision(1, "CENTPM-102", 0.92, "cached B"),
    ]
    _seed(cache, "F1", ext, [_fr(
        "F1", 0, "CENTPM-100", cached_decisions, anchors=["a1", "a2"],
    )])

    project = _project([
        {"key": "CENTPM-100", "summary": "Sec", "description": "", "children": [
            {"key": "CENTPM-101", "summary": "T1", "status": "Backlog", "description": ""},
            {"key": "CENTPM-102", "summary": "T2", "status": "Backlog", "description": ""},
        ]},
    ])

    s1_calls = {"items": []}
    def _fake_match(items, candidates, *, kind):
        s1_calls["items"].extend(items)
        assert kind == "epic"
        return [MatchDecision(0, "CENTPM-100", 0.95, "stage1 confirmed")]
    monkeypatch.setattr(file_match, "match", _fake_match)
    monkeypatch.setattr(file_match, "run_matcher", _bomb)

    sent: list = []
    def _fake_grouped(groups, *, kind, batch_size, max_workers):
        sent.extend(groups)
        return [
            GroupResult(
                group_id=g.group_id,
                decisions=[
                    MatchDecision(i, "CENTPM-101", 0.99, f"refreshed {i}")
                    for i in range(len(g.items))
                ],
            )
            for g in groups
        ]
    monkeypatch.setattr(file_match, "match_grouped", _fake_grouped)

    result = match_with_cache(
        [(_df(), ext)], project, cache,
        content_shas={"F1": "NEW"},
        use_cache=True, matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: None,
        dirty_anchors_per_file={"F1": {"a1"}},
    )

    assert len(s1_calls["items"]) == 1
    assert len(sent) == 1 and len(sent[0].items) == 1
    assert sent[0].items[0].summary == "T1"

    fr = result.file_results[0]
    assert fr.task_decisions[0].reason.startswith("refreshed")
    assert fr.task_decisions[0].candidate_key == "CENTPM-101"
    assert fr.task_decisions[1].reason == "cached B"
    assert fr.task_decisions[1].candidate_key == "CENTPM-102"


def test_partial_self_heals_cached_none_via_stage1(monkeypatch):
    """A section cached with matched_jira_key=None gets re-paired when
    a task under it goes dirty — the cached miss self-heals."""
    cache = Cache()
    ext = _single([_t("T", "a")])
    _seed(cache, "F1", ext, [_fr(
        "F1", 0, None, [MatchDecision(0, None, 0.0, "no match")],
        anchors=["a"],
    )])

    project = _project([
        {"key": "CENTPM-100", "summary": "Real epic", "description": "",
         "children": [
             {"key": "CENTPM-101", "summary": "T", "status": "Backlog", "description": ""},
         ]},
    ])

    monkeypatch.setattr(
        file_match, "match",
        lambda items, candidates, *, kind: [
            MatchDecision(0, "CENTPM-100", 0.95, "stage1 found it")
        ],
    )
    monkeypatch.setattr(file_match, "run_matcher", _bomb)
    monkeypatch.setattr(
        file_match, "match_grouped",
        lambda groups, **kw: [
            GroupResult(
                group_id=g.group_id,
                decisions=[
                    MatchDecision(i, "CENTPM-101", 0.99, "found")
                    for i in range(len(g.items))
                ],
            )
            for g in groups
        ],
    )

    result = match_with_cache(
        [(_df(), ext)], project, cache,
        content_shas={"F1": "NEW"},
        use_cache=True, matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: None,
        dirty_anchors_per_file={"F1": {"a"}},
    )
    fr = result.file_results[0]
    assert fr.matched_jira_key == "CENTPM-100"
    assert fr.task_decisions[0].candidate_key == "CENTPM-101"


def test_partial_dirty_epic_reruns_stage1_and_stage2(monkeypatch):
    cache = Cache()
    ext = _single([_t("T1", "a1"), _t("T2", "a2")])
    cached_decisions = [
        MatchDecision(0, "CENTPM-101", 0.95, "cached A"),
        MatchDecision(1, "CENTPM-102", 0.92, "cached B"),
    ]
    _seed(cache, "F1", ext, [_fr(
        "F1", 0, "CENTPM-100", cached_decisions, anchors=["a1", "a2"],
    )])

    project = _project([
        {"key": "CENTPM-100", "summary": "Sec", "description": "", "children": [
            {"key": "CENTPM-101", "summary": "T1", "status": "Backlog", "description": ""},
            {"key": "CENTPM-102", "summary": "T2", "status": "Backlog", "description": ""},
        ]},
        {"key": "CENTPM-200", "summary": "Other Sec", "description": "", "children": [
            {"key": "CENTPM-201", "summary": "T1", "status": "Backlog", "description": ""},
            {"key": "CENTPM-202", "summary": "T2", "status": "Backlog", "description": ""},
        ]},
    ])

    s1_calls = {"n": 0}

    def _fake_match(items, candidates, *, kind):
        s1_calls["n"] += 1
        assert kind == "epic"
        return [MatchDecision(0, "CENTPM-200", 0.95, "epic re-decided")]
    monkeypatch.setattr(file_match, "match", _fake_match)
    monkeypatch.setattr(file_match, "run_matcher", _bomb)

    s2_calls = {"items": []}

    def _fake_grouped(groups, *, kind, batch_size, max_workers):
        for g in groups:
            s2_calls["items"].extend(g.items)
        return [
            GroupResult(
                group_id=g.group_id,
                decisions=[
                    MatchDecision(i, "CENTPM-201", 0.98, f"new-epic-task {i}")
                    for i in range(len(g.items))
                ],
            )
            for g in groups
        ]
    monkeypatch.setattr(file_match, "match_grouped", _fake_grouped)

    result = match_with_cache(
        [(_df(), ext)], project, cache,
        content_shas={"F1": "NEW"},
        use_cache=True, matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: None,
        dirty_anchors_per_file={"F1": {"<epic>:0"}},
    )
    assert s1_calls["n"] == 1
    assert len(s2_calls["items"]) == 2  # all tasks rerun under new epic
    fr = result.file_results[0]
    assert fr.matched_jira_key == "CENTPM-200"
    assert all("new-epic-task" in d.reason for d in fr.task_decisions)


def test_partial_new_subepic_full_match_for_new_section_only(monkeypatch):
    cache = Cache()
    ext_old = _multi([("E1", [_t("T1", "a1")])])
    cached_decisions = [MatchDecision(0, "CENTPM-201", 0.95, "cached")]
    _seed(
        cache, "F1", ext_old,
        [_fr("F1", 0, "CENTPM-200", cached_decisions,
             anchors=["a1"], epic_summary="E1")],
    )

    ext_new = _multi([
        ("E1", [_t("T1", "a1")]),
        ("E2 brand new", [_t("T2", "a2")]),
    ])

    project = _project([
        {"key": "CENTPM-200", "summary": "E1", "description": "", "children": [
            {"key": "CENTPM-201", "summary": "T1", "status": "Backlog", "description": ""},
        ]},
        {"key": "CENTPM-300", "summary": "E2 brand new", "description": "", "children": []},
    ])

    s1_items = {"items": []}

    def _fake_match(items, candidates, *, kind):
        s1_items["items"].extend(items)
        return [MatchDecision(0, "CENTPM-300", 0.95, "new section matched")]
    monkeypatch.setattr(file_match, "match", _fake_match)
    monkeypatch.setattr(file_match, "run_matcher", _bomb)

    s2_calls: list = []

    def _fake_grouped(groups, *, kind, batch_size, max_workers):
        s2_calls.extend(groups)
        return [
            GroupResult(
                group_id=g.group_id,
                decisions=[
                    MatchDecision(i, None, 0.0, "no candidate")
                    for i in range(len(g.items))
                ],
            )
            for g in groups
        ]
    monkeypatch.setattr(file_match, "match_grouped", _fake_grouped)

    result = match_with_cache(
        [(_df(), ext_new)], project, cache,
        content_shas={"F1": "NEW"},
        use_cache=True, matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: None,
        dirty_anchors_per_file={"F1": {"<epic>:1", "a2"}},
    )
    assert len(s1_items["items"]) == 1
    assert s1_items["items"][0].summary == "E2 brand new"
    assert len(s2_calls) == 1
    assert len(s2_calls[0].items) == 1

    assert len(result.file_results) == 2
    assert result.file_results[0].matched_jira_key == "CENTPM-200"
    assert result.file_results[0].task_decisions[0].reason == "cached"
    assert result.file_results[1].matched_jira_key == "CENTPM-300"


def test_stale_matched_key_drops_cache_and_falls_back(monkeypatch):
    cache = Cache()
    ext = _single([_t("T", "a")])
    _seed(cache, "F1", ext, [_fr("F1", 0, "CENTPM-DELETED",
                                  [MatchDecision(0, "CENTPM-101", 0.9, "cached")])])

    project = _project([
        {"key": "CENTPM-200", "summary": "Other", "description": "", "children": []},
    ])

    fresh = MatcherResult(file_results=[
        _fr("F1", 0, None, [MatchDecision(0, None, 0.0, "no match")])
    ])
    monkeypatch.setattr(file_match, "run_matcher", lambda *a, **kw: fresh)
    monkeypatch.setattr(file_match, "match", _bomb)
    monkeypatch.setattr(file_match, "match_grouped", _bomb)

    result = match_with_cache(
        [(_df(), ext)], project, cache,
        content_shas={"F1": "OLD"},
        use_cache=True, matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: None,
        dirty_anchors_per_file={"F1": {"a"}},
    )
    assert result.file_results[0].matched_jira_key is None


def test_fresh_full_match_when_no_cache(monkeypatch):
    cache = Cache()
    ext = _single([_t("T", "a")])
    project = _project([
        {"key": "CENTPM-100", "summary": "Sec", "description": "", "children": []},
    ])

    fresh = MatcherResult(file_results=[
        _fr("F1", 0, None, [MatchDecision(0, None, 0.0, "fresh")])
    ])
    called = {"n": 0}

    def _fake_run(*a, **kw):
        called["n"] += 1
        return fresh
    monkeypatch.setattr(file_match, "run_matcher", _fake_run)
    monkeypatch.setattr(file_match, "match", _bomb)
    monkeypatch.setattr(file_match, "match_grouped", _bomb)

    result = match_with_cache(
        [(_df(), ext)], project, cache,
        content_shas={"F1": "X"},
        use_cache=True, matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: None,
    )
    assert called["n"] == 1
    assert result.file_results[0].task_decisions[0].reason == "fresh"


def test_use_cache_false_runs_full_matcher(monkeypatch):
    cache = Cache()
    ext = _single([_t("T", "a")])
    cached_fr = _fr("F1", 0, "CENTPM-100", [MatchDecision(0, "CENTPM-101", 0.95, "cached")])
    _seed(cache, "F1", ext, [cached_fr])

    project = _project([
        {"key": "CENTPM-100", "summary": "Sec", "description": "", "children": [
            {"key": "CENTPM-101", "summary": "T", "status": "Backlog", "description": ""},
        ]},
    ])

    fresh = MatcherResult(file_results=[
        _fr("F1", 0, None, [MatchDecision(0, None, 0.0, "fresh fallback")])
    ])
    monkeypatch.setattr(file_match, "run_matcher", lambda *a, **kw: fresh)
    monkeypatch.setattr(file_match, "match", _bomb)
    monkeypatch.setattr(file_match, "match_grouped", _bomb)

    result = match_with_cache(
        [(_df(), ext)], project, cache,
        content_shas={"F1": "X"},
        use_cache=False, matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: None,
    )
    assert result.file_results[0].task_decisions[0].reason == "fresh fallback"


def test_partial_persists_so_rerun_is_cache_hit(monkeypatch):
    cache = Cache()
    ext = _single([_t("T1", "a1"), _t("T2", "a2")])
    _seed(
        cache, "F1", ext,
        [_fr("F1", 0, "CENTPM-100", [
            MatchDecision(0, "CENTPM-101", 0.95, "cached A"),
            MatchDecision(1, "CENTPM-102", 0.92, "cached B"),
        ], anchors=["a1", "a2"])],
    )

    project = _project([
        {"key": "CENTPM-100", "summary": "Sec", "description": "", "children": [
            {"key": "CENTPM-101", "summary": "T1", "status": "Backlog", "description": ""},
            {"key": "CENTPM-102", "summary": "T2", "status": "Backlog", "description": ""},
        ]},
    ])

    monkeypatch.setattr(
        file_match, "match",
        lambda items, candidates, *, kind: [
            MatchDecision(0, "CENTPM-100", 0.95, "stage1 confirmed")
        ],
    )
    monkeypatch.setattr(file_match, "run_matcher", _bomb)
    monkeypatch.setattr(file_match, "match_grouped", lambda groups, **kw: [
        GroupResult(
            group_id=g.group_id,
            decisions=[
                MatchDecision(i, "CENTPM-101", 0.99, "refreshed")
                for i in range(len(g.items))
            ],
        )
        for g in groups
    ])

    match_with_cache(
        [(_df(), ext)], project, cache,
        content_shas={"F1": "NEW"},
        use_cache=True, matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: None,
        dirty_anchors_per_file={"F1": {"a1"}},
    )

    # Rerun with the same content_sha — must be a pure cache hit, no
    # match_grouped, no match, no run_matcher.
    monkeypatch.setattr(file_match, "match_grouped", _bomb)

    hits = {"n": 0}
    result = match_with_cache(
        [(_df(), ext)], project, cache,
        content_shas={"F1": "NEW"},
        use_cache=True, matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: hits.__setitem__("n", hits["n"] + 1),
    )
    assert hits["n"] == 1
    fr = result.file_results[0]
    assert fr.task_decisions[0].reason == "refreshed"
    assert fr.task_decisions[1].reason == "cached B"


def test_empty_extractions_returns_empty_result():
    cache = Cache()
    project = _project([])
    result = match_with_cache(
        [], project, cache,
        content_shas={}, use_cache=True,
        matcher_batch_size=4, matcher_max_workers=3,
        on_cache_hits_match=lambda: None,
    )
    assert result.file_results == []
