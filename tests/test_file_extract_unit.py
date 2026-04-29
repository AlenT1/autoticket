"""Unit tests for pipeline.file_extract.

Covers:
  - _changed_chunk_ids: same / one changed / new chunk / removed chunk
  - _compute_diff_payload: builds chunks dict + task_anchors dict
  - _merge_diff_into_cached: single_epic edit + add + epic_changed;
                              multi_epic edit in one section
  - _try_diff_aware_extract: success path + fallbacks (no cache, no
                             changed chunks, malformed LLM response)
  - extract_or_reuse: Tier 2 hit, diff-aware path, cold path,
                       failure path

All LLM calls are stubbed; no network.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest

from jira_task_agent.cache import Cache, serialize_extraction
from jira_task_agent.drive.client import DriveFile
from jira_task_agent.pipeline import extractor, file_extract
from jira_task_agent.pipeline.classifier import ClassifyResult
from jira_task_agent.pipeline.extractor import (
    AGENT_MARKER,
    DiffExtractionResult,
    ExtractedEpic,
    ExtractedEpicWithTasks,
    ExtractedTask,
    ExtractionResult,
    MultiExtractionResult,
)
from jira_task_agent.pipeline.file_extract import (
    _changed_chunk_ids,
    _compute_diff_payload,
    _merge_diff_into_cached,
    _try_diff_aware_extract,
    extract_or_reuse,
)


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------


def _df(file_id: str = "F1", name: str = "V11_Dashboard_Tasks.md") -> DriveFile:
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


def _classify(role: str = "single_epic") -> ClassifyResult:
    return ClassifyResult(file_id="F1", role=role, confidence=0.99, reason="x")


def _task_desc(body: str = "body") -> str:
    return (
        f"{body}\n\n### Definition of Done\n"
        "- [ ] one\n- [ ] two\n- [ ] three\n\n"
        f"{AGENT_MARKER}"
    )


def _task(summary: str, anchor: str, body: str = "body") -> ExtractedTask:
    return ExtractedTask(
        summary=summary,
        description=_task_desc(body),
        source_anchor=anchor,
    )


def _single(tasks: list[ExtractedTask]) -> ExtractionResult:
    return ExtractionResult(
        file_id="F1",
        file_name="V11.md",
        epic=ExtractedEpic(
            summary="Dashboard improvements",
            description=f"Body.\n\n{AGENT_MARKER}",
        ),
        tasks=tasks,
    )


def _multi(epics: list[tuple[str, list[ExtractedTask]]]) -> MultiExtractionResult:
    return MultiExtractionResult(
        file_id="F1",
        file_name="May1.md",
        epics=[
            ExtractedEpicWithTasks(
                summary=summary,
                description=f"Body of {summary}.\n\n{AGENT_MARKER}",
                assignee_name=None,
                tasks=tasks,
            )
            for summary, tasks in epics
        ],
    )


# ----------------------------------------------------------------------
# _changed_chunk_ids
# ----------------------------------------------------------------------


def test_changed_chunk_ids_no_changes():
    cached = {"A|0": "a1", "B|0": "b1"}
    current = {"A|0": "a1", "B|0": "b1"}
    assert _changed_chunk_ids(cached, current) == []


def test_changed_chunk_ids_one_changed():
    cached = {"A|0": "a1", "B|0": "b1"}
    current = {"A|0": "a2", "B|0": "b1"}
    assert _changed_chunk_ids(cached, current) == ["A|0"]


def test_changed_chunk_ids_new_chunk_added():
    cached = {"A|0": "a1"}
    current = {"A|0": "a1", "C|0": "c1"}
    assert _changed_chunk_ids(cached, current) == ["C|0"]


def test_changed_chunk_ids_chunk_removed_not_returned():
    """Removed chunks aren't 'changed' from this function's perspective."""
    cached = {"A|0": "a1", "B|0": "b1"}
    current = {"A|0": "a1"}
    assert _changed_chunk_ids(cached, current) == []


# ----------------------------------------------------------------------
# _compute_diff_payload
# ----------------------------------------------------------------------


def test_compute_diff_payload_maps_tasks_to_chunks():
    text = (
        "# Title\n\n"
        "## A. Section\nSEC-1 first task body.\nSEC-2 second task body.\n\n"
        "## B. Section\nB-1 third task body.\n"
    )
    ext = _single([
        _task("First task", "SEC-1 first task"),
        _task("Second task", "SEC-2 second task"),
        _task("Third task", "B-1 third task"),
    ])
    diff = _compute_diff_payload(ext, text)
    assert "chunks" in diff and "task_anchors" in diff
    # Tasks 1+2 should map to A. Section, task 3 to B. Section
    a1 = diff["task_anchors"]["SEC-1 first task"]["chunk_id"]
    a2 = diff["task_anchors"]["SEC-2 second task"]["chunk_id"]
    b1 = diff["task_anchors"]["B-1 third task"]["chunk_id"]
    assert a1 == a2
    assert "A. Section" in a1
    assert "B. Section" in b1


def test_compute_diff_payload_drops_unlocatable_anchors():
    text = "# Title\n\n## A. Section\nbody.\n"
    ext = _single([_task("ghost", "anchor-not-in-doc-XYZ")])
    diff = _compute_diff_payload(ext, text)
    assert "anchor-not-in-doc-XYZ" not in diff["task_anchors"]


# ----------------------------------------------------------------------
# _merge_diff_into_cached — single_epic
# ----------------------------------------------------------------------


def test_merge_single_epic_edit_replaces_only_changed_task():
    cached_ext = _single([
        _task("Task A", "anchor-A", "body A"),
        _task("Task B", "anchor-B", "body B"),
    ])
    cached_anchors = {
        "anchor-A": {"chunk_id": "Sec A|0", "body_sha": "sha-A"},
        "anchor-B": {"chunk_id": "Sec B|0", "body_sha": "sha-B"},
    }
    diff = DiffExtractionResult(
        file_id="F1",
        file_name="V11.md",
        epic_changed=False,
        epic=None,
        tasks=[_task("Task A edited", "anchor-A", "edited body A")],
    )
    merged = _merge_diff_into_cached(
        cached_extraction=serialize_extraction(cached_ext),
        cached_anchors=cached_anchors,
        current_chunks={},  # not used by single_epic merge
        changed_chunk_ids=["Sec A|0"],
        diff_result=diff,
        drive_file=_df(),
    )
    assert isinstance(merged, ExtractionResult)
    summaries = [t.summary for t in merged.tasks]
    # Task A replaced; Task B preserved.
    assert "Task A edited" in summaries
    assert "Task B" in summaries
    assert "Task A" not in summaries  # the original was dropped


def test_merge_single_epic_added_task():
    cached_ext = _single([_task("Task A", "anchor-A")])
    cached_anchors = {"anchor-A": {"chunk_id": "Sec A|0", "body_sha": "sha-A"}}
    diff = DiffExtractionResult(
        file_id="F1",
        file_name="V11.md",
        epic_changed=False,
        epic=None,
        tasks=[
            _task("Task A", "anchor-A", "body A"),
            _task("Brand new", "anchor-NEW"),
        ],
    )
    merged = _merge_diff_into_cached(
        cached_extraction=serialize_extraction(cached_ext),
        cached_anchors=cached_anchors,
        current_chunks={},
        changed_chunk_ids=["Sec A|0"],
        diff_result=diff,
        drive_file=_df(),
    )
    summaries = [t.summary for t in merged.tasks]
    # Old "Task A" dropped (chunk is in changed_set), both fresh tasks added.
    assert "Brand new" in summaries
    # The fresh "Task A" replaces the old one.
    assert summaries.count("Task A") == 1


def test_merge_single_epic_epic_changed_replaces_epic():
    cached_ext = _single([_task("T1", "a1")])
    cached_anchors = {"a1": {"chunk_id": "Sec A|0", "body_sha": "sha"}}
    new_epic = ExtractedEpic(
        summary="Updated epic title",
        description=f"updated desc\n\n{AGENT_MARKER}",
    )
    diff = DiffExtractionResult(
        file_id="F1",
        file_name="V11.md",
        epic_changed=True,
        epic=new_epic,
        tasks=[],
    )
    merged = _merge_diff_into_cached(
        cached_extraction=serialize_extraction(cached_ext),
        cached_anchors=cached_anchors,
        current_chunks={},
        changed_chunk_ids=["overview|0"],  # the epic-bearing chunk changed
        diff_result=diff,
        drive_file=_df(),
    )
    assert merged.epic.summary == "Updated epic title"
    # Tasks unchanged: T1 lives in Sec A|0 (not in changed_set).
    assert [t.summary for t in merged.tasks] == ["T1"]


def test_merge_single_epic_no_changes_when_chunk_not_in_set():
    """Defensive — diff returns nothing, all cached tasks survive."""
    cached_ext = _single([_task("T1", "a1"), _task("T2", "a2")])
    cached_anchors = {
        "a1": {"chunk_id": "Sec A|0", "body_sha": "sha"},
        "a2": {"chunk_id": "Sec B|0", "body_sha": "sha"},
    }
    diff = DiffExtractionResult(
        file_id="F1",
        file_name="V11.md",
        epic_changed=False,
        epic=None,
        tasks=[],
    )
    merged = _merge_diff_into_cached(
        cached_extraction=serialize_extraction(cached_ext),
        cached_anchors=cached_anchors,
        current_chunks={},
        changed_chunk_ids=["unrelated|0"],
        diff_result=diff,
        drive_file=_df(),
    )
    assert [t.summary for t in merged.tasks] == ["T1", "T2"]


# ----------------------------------------------------------------------
# _merge_diff_into_cached — multi_epic
# ----------------------------------------------------------------------


def test_merge_multi_epic_edit_in_one_section():
    """Edit a section that owns one task; another task in a sibling
    chunk under the same epic survives unchanged."""
    cached_ext = _multi([
        ("Security Hardening", [_task("SEC-A", "sec-a"), _task("SEC-B", "sec-b")]),
        ("Monitoring", [_task("MON-A", "mon-a")]),
    ])
    cached_anchors = {
        # SEC-A and SEC-B happen to live in DIFFERENT chunks (e.g.
        # section A has H3-anchored sub-blocks, or the chunker split
        # them differently).
        "sec-a": {"chunk_id": "A. Security Hardening|0", "body_sha": "sha"},
        "sec-b": {"chunk_id": "A. Security Hardening other|0", "body_sha": "sha"},
        "mon-a": {"chunk_id": "C. Monitoring|0", "body_sha": "sha"},
    }
    diff = DiffExtractionResult(
        file_id="F1",
        file_name="May1.md",
        epic_changed=False,
        epic=None,
        tasks=[_task("SEC-A edited", "sec-a", "new body")],
    )
    merged = _merge_diff_into_cached(
        cached_extraction=serialize_extraction(cached_ext),
        cached_anchors=cached_anchors,
        current_chunks={},
        changed_chunk_ids=["A. Security Hardening|0"],  # only SEC-A's chunk
        diff_result=diff,
        drive_file=_df("F1", "May1.md"),
    )
    assert isinstance(merged, MultiExtractionResult)
    sec_epic = next(e for e in merged.epics if e.summary == "Security Hardening")
    mon_epic = next(e for e in merged.epics if e.summary == "Monitoring")
    sec_summaries = [t.summary for t in sec_epic.tasks]
    # SEC-B (unchanged chunk) preserved; SEC-A (changed chunk) replaced.
    assert "SEC-B" in sec_summaries
    assert "SEC-A edited" in sec_summaries
    assert "SEC-A" not in sec_summaries  # original dropped
    # Monitoring epic untouched.
    assert [t.summary for t in mon_epic.tasks] == ["MON-A"]


def test_merge_multi_epic_new_task_routed_by_heading_match():
    cached_ext = _multi([
        ("Security Hardening", [_task("SEC-A", "sec-a")]),
        ("Monitoring", [_task("MON-A", "mon-a")]),
    ])
    cached_anchors = {
        "sec-a": {"chunk_id": "A. Security Hardening|0", "body_sha": "sha"},
        "mon-a": {"chunk_id": "C. Monitoring|0", "body_sha": "sha"},
    }
    # A brand-new task in the Monitoring section.
    diff = DiffExtractionResult(
        file_id="F1",
        file_name="May1.md",
        epic_changed=False,
        epic=None,
        tasks=[_task("MON-NEW", "mon-new")],
    )
    merged = _merge_diff_into_cached(
        cached_extraction=serialize_extraction(cached_ext),
        cached_anchors=cached_anchors,
        current_chunks={},
        changed_chunk_ids=["C. Monitoring|0"],
        diff_result=diff,
        drive_file=_df("F1", "May1.md"),
    )
    mon_epic = next(e for e in merged.epics if e.summary == "Monitoring")
    summaries = [t.summary for t in mon_epic.tasks]
    assert "MON-NEW" in summaries


# ----------------------------------------------------------------------
# _try_diff_aware_extract
# ----------------------------------------------------------------------


def test_diff_aware_returns_none_when_no_cached_state(tmp_path, monkeypatch):
    """No prior extraction in cache → can't do diff-aware."""
    cache = Cache()
    p = tmp_path / "f.md"
    p.write_text("# T\n## A\nbody")
    out = _try_diff_aware_extract(
        _df(),
        local_path=p,
        content_sha="C",
        root_context="",
        cache=cache,
    )
    assert out is None


def test_diff_aware_returns_none_when_no_changed_chunks(tmp_path, monkeypatch):
    """Cached chunks match current shas → defer to caller's full path."""
    cache = Cache()
    text = "# T\n\n## A. Section\nbody A.\n"
    p = tmp_path / "f.md"
    p.write_text(text)
    # Seed cache with extraction + diff payload matching the file.
    cached_ext = _single([_task("T1", "a1")])
    cache.set_classification(
        file_id="F1", modified_time="t", content_sha="OLD-SHA",
        role="single_epic", confidence=1.0, reason="x",
    )
    cache.set_extraction(
        file_id="F1", modified_time="t", content_sha="OLD-SHA",
        extraction_payload=serialize_extraction(cached_ext),
    )
    # Compute current shas and write them as the cached chunks (zero diff).
    diff_payload = _compute_diff_payload(cached_ext, text)
    cache.set_diff_payload(
        file_id="F1",
        chunks=diff_payload["chunks"],
        task_anchors=diff_payload["task_anchors"],
    )
    # New content_sha (so Tier 2 missed) but chunks identical.
    out = _try_diff_aware_extract(
        _df(),
        local_path=p,
        content_sha="NEW-SHA",
        root_context="",
        cache=cache,
    )
    assert out is None  # caller falls back to full re-extract


def test_diff_aware_calls_llm_and_returns_merged(tmp_path, monkeypatch):
    cache = Cache()
    # Each section's body has a UNIQUE anchor string so the locator
    # can pick the right chunk deterministically.
    old_text = (
        "# Title\n\n"
        "## A. Section\nALPHA-task generate JWT secret in vault.\n\n"
        "## B. Section\nBRAVO-task configure rate limiting middleware.\n"
    )
    new_text = (
        "# Title\n\n"
        "## A. Section\nALPHA-task EDITED generate JWT secret in vault now.\n\n"
        "## B. Section\nBRAVO-task configure rate limiting middleware.\n"
    )
    p = tmp_path / "f.md"
    p.write_text(new_text)
    cached_ext = _single([
        _task("Task A — JWT secret", "ALPHA-task generate JWT"),
        _task("Task B — rate limit", "BRAVO-task configure rate limiting"),
    ])
    cache.set_classification(
        file_id="F1", modified_time="t", content_sha="OLD",
        role="single_epic", confidence=1.0, reason="x",
    )
    cache.set_extraction(
        file_id="F1", modified_time="t", content_sha="OLD",
        extraction_payload=serialize_extraction(cached_ext),
    )
    diff_payload = _compute_diff_payload(cached_ext, old_text)
    cache.set_diff_payload(
        file_id="F1",
        chunks=diff_payload["chunks"],
        task_anchors=diff_payload["task_anchors"],
    )

    # Stub the diff extractor LLM.
    def _fake_chat(**_kwargs):
        return ({
            "epic_changed": False,
            "epic": None,
            "tasks": [{
                "summary": "Task A — JWT secret edited",
                "description": _task_desc("new body"),
                "source_anchor": "ALPHA-task generate JWT EDITED",
                "assignee": None,
            }],
        }, {"model": "stub"})
    monkeypatch.setattr(extractor, "chat", _fake_chat)

    out = _try_diff_aware_extract(
        _df(),
        local_path=p,
        content_sha="NEW",
        root_context="",
        cache=cache,
    )
    assert out is not None
    summaries = [t.summary for t in out.tasks]
    # Task A replaced with edited version; Task B preserved (chunk unchanged).
    assert "Task A — JWT secret edited" in summaries
    assert "Task B — rate limit" in summaries


def test_diff_aware_falls_through_on_llm_error(tmp_path, monkeypatch):
    cache = Cache()
    old_text = "# T\n\n## A\nold body."
    new_text = "# T\n\n## A\nnew body."
    p = tmp_path / "f.md"
    p.write_text(new_text)
    cached_ext = _single([_task("T1", "a1 old body")])
    cache.set_classification(
        file_id="F1", modified_time="t", content_sha="OLD",
        role="single_epic", confidence=1.0, reason="x",
    )
    cache.set_extraction(
        file_id="F1", modified_time="t", content_sha="OLD",
        extraction_payload=serialize_extraction(cached_ext),
    )
    cache.set_diff_payload(
        file_id="F1",
        chunks=_compute_diff_payload(cached_ext, old_text)["chunks"],
        task_anchors=_compute_diff_payload(cached_ext, old_text)["task_anchors"],
    )
    # Stub returns malformed response so extract_diff_aware raises.
    monkeypatch.setattr(
        extractor, "chat",
        lambda **_: (["not a dict"], {"model": "stub"}),
    )
    out = _try_diff_aware_extract(
        _df(),
        local_path=p,
        content_sha="NEW",
        root_context="",
        cache=cache,
    )
    assert out is None  # caller will fall back to full re-extract


# ----------------------------------------------------------------------
# extract_or_reuse — top-level decision tree
# ----------------------------------------------------------------------


class _Counters:
    def __init__(self):
        self.ok = 0
        self.failed: list[str] = []
        self.cache_hits = 0

    def hooks(self):
        def _ok():
            self.ok += 1
        def _failed(msg: str):
            self.failed.append(msg)
        def _hit():
            self.cache_hits += 1
        return _ok, _failed, _hit


def test_extract_or_reuse_tier2_hit(tmp_path, monkeypatch):
    cache = Cache()
    cached_ext = _single([_task("T", "a")])
    cache.set_classification(
        file_id="F1", modified_time="t", content_sha="C",
        role="single_epic", confidence=1.0, reason="x",
    )
    cache.set_extraction(
        file_id="F1", modified_time="t", content_sha="C",
        extraction_payload=serialize_extraction(cached_ext),
    )
    p = tmp_path / "f.md"
    p.write_text("# T\n## A\nbody")

    # Any LLM call here would be a bug.
    def _bomb(**_):
        raise AssertionError("LLM should not be called on Tier 2 hit")
    monkeypatch.setattr(extractor, "chat", _bomb)

    counters = _Counters()
    ok, failed, hit = counters.hooks()
    out = extract_or_reuse(
        _df(),
        classification=_classify(),
        local_path=p,
        content_sha="C",
        root_context="",
        cache=cache,
        use_cache=True,
        on_extract_ok=ok,
        on_extract_failed=failed,
        on_cache_hit_extract=hit,
    )
    assert out is not None
    assert counters.ok == 1
    assert counters.cache_hits == 1
    assert counters.failed == []


def test_extract_or_reuse_cold_path_calls_extractor(tmp_path, monkeypatch):
    cache = Cache()
    p = tmp_path / "f.md"
    p.write_text("# T\n## A. Section\nFresh content.\n")

    # Stub the cold extractor (extract_from_file) — it goes through
    # the same `chat` call.
    def _fake(**_):
        return ({
            "epic": {
                "summary": "Dashboard improvements",
                "description": "body.",
                "assignee": None,
            },
            "tasks": [{
                "summary": "Cold-extracted task",
                "description": _task_desc("cold body"),
                "source_anchor": "cold-1",
                "assignee": None,
            }],
        }, {"model": "stub"})
    monkeypatch.setattr(extractor, "chat", _fake)

    counters = _Counters()
    ok, failed, hit = counters.hooks()
    out = extract_or_reuse(
        _df(),
        classification=_classify(),
        local_path=p,
        content_sha="NEW",
        root_context="",
        cache=cache,
        use_cache=True,
        on_extract_ok=ok,
        on_extract_failed=failed,
        on_cache_hit_extract=hit,
    )
    assert out is not None
    assert out.tasks[0].summary == "Cold-extracted task"
    assert counters.ok == 1
    assert counters.cache_hits == 0
    # And it got persisted.
    assert cache.get_extraction("F1", "NEW") is not None
    assert cache.get_chunks("F1")  # diff payload populated


def test_extract_or_reuse_failed_path(tmp_path, monkeypatch):
    cache = Cache()
    p = tmp_path / "f.md"
    p.write_text("# T")
    monkeypatch.setattr(
        extractor, "chat",
        lambda **_: (None, {"model": "stub"}),  # triggers ExtractionError
    )
    counters = _Counters()
    ok, failed, hit = counters.hooks()
    out = extract_or_reuse(
        _df(),
        classification=_classify(),
        local_path=p,
        content_sha="X",
        root_context="",
        cache=cache,
        use_cache=False,  # bypass cache to force cold path
        on_extract_ok=ok,
        on_extract_failed=failed,
        on_cache_hit_extract=hit,
    )
    assert out is None
    assert counters.ok == 0
    assert len(counters.failed) == 1
    assert "extract failed" in counters.failed[0]


def test_extract_or_reuse_use_cache_false_bypasses_tier2(tmp_path, monkeypatch):
    """--no-cache equivalent: even with Tier 2 entry, run cold extract."""
    cache = Cache()
    cached_ext = _single([_task("STALE", "stale-anchor")])
    cache.set_classification(
        file_id="F1", modified_time="t", content_sha="C",
        role="single_epic", confidence=1.0, reason="x",
    )
    cache.set_extraction(
        file_id="F1", modified_time="t", content_sha="C",
        extraction_payload=serialize_extraction(cached_ext),
    )
    p = tmp_path / "f.md"
    p.write_text("# T\n## A\nbody")

    # Cold extractor returns something fresh.
    def _fake(**_):
        return ({
            "epic": {"summary": "Fresh epic title", "description": "x", "assignee": None},
            "tasks": [{
                "summary": "Freshly extracted task",
                "description": _task_desc("b"),
                "source_anchor": "fresh-1",
                "assignee": None,
            }],
        }, {"model": "stub"})
    monkeypatch.setattr(extractor, "chat", _fake)

    counters = _Counters()
    ok, failed, hit = counters.hooks()
    out = extract_or_reuse(
        _df(),
        classification=_classify(),
        local_path=p,
        content_sha="C",
        root_context="",
        cache=cache,
        use_cache=False,
        on_extract_ok=ok,
        on_extract_failed=failed,
        on_cache_hit_extract=hit,
    )
    assert out is not None
    assert out.tasks[0].summary == "Freshly extracted task"
    assert counters.cache_hits == 0  # use_cache=False → no Tier 2 read


def test_all_tasks_with_multi_extraction():
    multi = _multi([
        ("E1", [_task("T1", "a1"), _task("T2", "a2")]),
        ("E2", [_task("T3", "a3")]),
    ])
    flat = file_extract._all_tasks(multi)
    assert [t.summary for t in flat] == ["T1", "T2", "T3"]


def test_all_tasks_with_single_extraction():
    single = _single([_task("T1", "a1"), _task("T2", "a2")])
    flat = file_extract._all_tasks(single)
    assert [t.summary for t in flat] == ["T1", "T2"]


def test_extract_or_reuse_tier2_payload_unusable_falls_through(tmp_path, monkeypatch):
    """Tier 2 entry exists but deserialize blows up → log warning and
    fall through to fresh extract."""
    cache = Cache()
    cache.set_classification(
        file_id="F1", modified_time="t", content_sha="C",
        role="single_epic", confidence=1.0, reason="x",
    )
    # Manually corrupt the payload so deserialize_extraction raises.
    cache.files["F1"].extraction_payload = {"type": "single_epic", "BROKEN": True}

    p = tmp_path / "f.md"
    p.write_text("# T\n## A\nfresh body.")

    def _fresh(**_):
        return ({
            "epic": {"summary": "Recovered fresh epic", "description": "x", "assignee": None},
            "tasks": [{
                "summary": "Recovery task",
                "description": _task_desc("recovered"),
                "source_anchor": "recovery-1",
                "assignee": None,
            }],
        }, {"model": "stub"})
    monkeypatch.setattr(extractor, "chat", _fresh)

    counters = _Counters()
    ok, failed, hit = counters.hooks()
    out = extract_or_reuse(
        _df(),
        classification=_classify(),
        local_path=p,
        content_sha="C",
        root_context="",
        cache=cache,
        use_cache=True,
        on_extract_ok=ok,
        on_extract_failed=failed,
        on_cache_hit_extract=hit,
    )
    assert out is not None
    assert out.tasks[0].summary == "Recovery task"
    # We didn't count it as a cache hit since the payload was unusable.
    assert counters.cache_hits == 0


def test_extract_or_reuse_multi_epic_cold_path(tmp_path, monkeypatch):
    cache = Cache()
    p = tmp_path / "f.md"
    p.write_text("# T\n\n## A. Sec\nbody.\n\n## B. Sec\nbody.\n")

    def _fake(**_):
        return ({
            "epics": [{
                "summary": "Section one epic",
                "description": "body section one.",
                "assignee": None,
                "tasks": [{
                    "summary": "Section A task one",
                    "description": _task_desc("body"),
                    "source_anchor": "A-1",
                    "assignee": None,
                }],
            }],
        }, {"model": "stub"})
    monkeypatch.setattr(extractor, "chat", _fake)

    counters = _Counters()
    ok, failed, hit = counters.hooks()
    out = extract_or_reuse(
        _df(),
        classification=_classify(role="multi_epic"),
        local_path=p,
        content_sha="NEW",
        root_context="",
        cache=cache,
        use_cache=True,
        on_extract_ok=ok,
        on_extract_failed=failed,
        on_cache_hit_extract=hit,
    )
    assert out is not None
    assert isinstance(out, MultiExtractionResult)
    assert len(out.epics) == 1
    assert out.epics[0].summary == "Section one epic"


def test_extract_or_reuse_takes_diff_aware_path_when_applicable(tmp_path, monkeypatch):
    """End-to-end through extract_or_reuse: cache has prior state, file
    content changed, only one chunk's body differs → diff-aware path
    fires, returns merged extraction."""
    cache = Cache()
    old_text = (
        "# T\n\n"
        "## A. Section\nALPHA generate JWT secret in vault.\n\n"
        "## B. Section\nBRAVO configure rate limiting policy.\n"
    )
    new_text = (
        "# T\n\n"
        "## A. Section\nALPHA generate JWT secret EDITED in vault now.\n\n"
        "## B. Section\nBRAVO configure rate limiting policy.\n"
    )
    p = tmp_path / "f.md"
    p.write_text(new_text)

    cached_ext = _single([
        _task("Task A — JWT secret", "ALPHA generate JWT"),
        _task("Task B — rate limit", "BRAVO configure rate limiting"),
    ])
    cache.set_classification(
        file_id="F1", modified_time="t", content_sha="OLD",
        role="single_epic", confidence=1.0, reason="x",
    )
    cache.set_extraction(
        file_id="F1", modified_time="t", content_sha="OLD",
        extraction_payload=serialize_extraction(cached_ext),
    )
    diff_old = _compute_diff_payload(cached_ext, old_text)
    cache.set_diff_payload(
        file_id="F1",
        chunks=diff_old["chunks"],
        task_anchors=diff_old["task_anchors"],
    )

    # The diff LLM returns just the edited task.
    def _fake(**_):
        return ({
            "epic_changed": False,
            "epic": None,
            "tasks": [{
                "summary": "Task A — JWT secret EDITED",
                "description": _task_desc("edited body"),
                "source_anchor": "ALPHA generate JWT EDITED",
                "assignee": None,
            }],
        }, {"model": "stub"})
    monkeypatch.setattr(extractor, "chat", _fake)

    counters = _Counters()
    ok, failed, hit = counters.hooks()
    out = extract_or_reuse(
        _df(),
        classification=_classify(),
        local_path=p,
        content_sha="NEW",  # different from cached "OLD" → Tier 2 misses
        root_context="",
        cache=cache,
        use_cache=True,
        on_extract_ok=ok,
        on_extract_failed=failed,
        on_cache_hit_extract=hit,
    )
    assert out is not None
    summaries = [t.summary for t in out.tasks]
    assert "Task A — JWT secret EDITED" in summaries
    assert "Task B — rate limit" in summaries  # preserved from unchanged chunk
    # extract_ok was bumped, but cache_hit_extract wasn't — it's a
    # diff-aware result, not a Tier 2 hit.
    assert counters.ok == 1
    assert counters.cache_hits == 0


def test_extract_or_reuse_unknown_role_returns_none(tmp_path):
    """Defensive: classification.role='root' should never reach this
    function in production, but if it does we return None safely."""
    cache = Cache()
    p = tmp_path / "f.md"
    p.write_text("# T")
    counters = _Counters()
    ok, failed, hit = counters.hooks()
    out = extract_or_reuse(
        _df(),
        classification=_classify(role="root"),
        local_path=p,
        content_sha="X",
        root_context="",
        cache=cache,
        use_cache=False,
        on_extract_ok=ok,
        on_extract_failed=failed,
        on_cache_hit_extract=hit,
    )
    assert out is None
    assert counters.ok == 0
    assert counters.failed == []  # silent skip, not a failure
