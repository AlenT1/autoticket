"""Unit tests for `pipeline.file_extract`.

Covers the three branches of `extract_or_reuse`: Tier 2 hit, the
unified-diff warm path, and the cold path. LLM calls are stubbed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from jira_task_agent.cache import Cache, serialize_extraction
from jira_task_agent.drive.client import DriveFile
from jira_task_agent.pipeline import extractor as extractor_mod
from jira_task_agent.pipeline import file_extract
from jira_task_agent.pipeline.classifier import ClassifyResult
from jira_task_agent.pipeline.extractor import (
    AGENT_MARKER,
    DiffLabels,
    ExtractedEpic,
    ExtractedEpicWithTasks,
    ExtractedTask,
    ExtractionResult,
    MultiExtractionResult,
    TargetedBodies,
)
from jira_task_agent.pipeline.file_extract import (
    _sanitize_labels_against_source,
    compute_dirty,
    extract_or_reuse,
)


# ----------------------------------------------------------------------
# compute_dirty — pure-Python diff between cached and merged extraction
# ----------------------------------------------------------------------


def test_compute_dirty_single_epic_no_change():
    a = _single([_task("T", "anchor-T", "body")])
    assert compute_dirty(a, a) == set()


def test_compute_dirty_single_epic_task_modified():
    cached = _single([_task("T", "anchor-T", "body")])
    merged = _single([_task("T edited", "anchor-T", "edited")])
    assert compute_dirty(cached, merged) == {"anchor-T"}


def test_compute_dirty_single_epic_task_added():
    cached = _single([_task("T1", "a1")])
    merged = _single([_task("T1", "a1"), _task("T2 new", "a2-new")])
    assert compute_dirty(cached, merged) == {"a2-new"}


def test_compute_dirty_single_epic_epic_changed():
    cached = _single([_task("T", "a")])
    merged = ExtractionResult(
        file_id="F1", file_name="F.md",
        epic=ExtractedEpic(summary="Renamed title", description=f"x\n\n{AGENT_MARKER}"),
        tasks=[_task("T", "a")],
    )
    assert compute_dirty(cached, merged) == {"<epic>:0"}


def test_compute_dirty_multi_epic_new_subepic():
    cached = _multi([("E1", [_task("T1", "a1")])])
    merged = _multi([
        ("E1", [_task("T1", "a1")]),
        ("E2 new section", [_task("T2", "a2")]),
    ])
    assert compute_dirty(cached, merged) == {"a2", "<epic>:1"}


def test_compute_dirty_multi_epic_subepic_renamed():
    cached = _multi([("Original", [_task("T1", "a1")])])
    merged = _multi([("Renamed section", [_task("T1", "a1")])])
    assert compute_dirty(cached, merged) == {"<epic>:0"}


def test_compute_dirty_drops_whitespace_only_task_diff():
    """Defensive guard: if the merged body is byte-equal after
    whitespace/casing normalization, treat the task as unchanged."""
    cached = _single([_task("T", "a", "Body line one.\nBody line two.")])
    merged = _single([_task("T", "a", "Body line one.   Body line two.")])
    assert compute_dirty(cached, merged) == set()


def test_compute_dirty_drops_whitespace_only_epic_diff():
    cached = ExtractionResult(
        file_id="F1", file_name="F.md",
        epic=ExtractedEpic(summary="E", description=f"Body.\n\n{AGENT_MARKER}"),
        tasks=[_task("T", "a")],
    )
    merged = ExtractionResult(
        file_id="F1", file_name="F.md",
        epic=ExtractedEpic(summary="E", description=f"BODY.   \n\n  {AGENT_MARKER}"),
        tasks=[_task("T", "a")],
    )
    assert compute_dirty(cached, merged) == set()


def test_compute_dirty_keeps_real_task_change():
    """The defensive guard must not over-fire — real semantic changes
    must still be flagged."""
    cached = _single([_task("T", "a", "First version of the body.")])
    merged = _single([_task("T", "a", "Completely different body content.")])
    assert compute_dirty(cached, merged) == {"a"}


# ----------------------------------------------------------------------
# _sanitize_labels_against_source — drops LLM over-emission against the
# deterministic source-doc text. This is the safety net that prevents an
# LLM `modified_anchors=[T1, T2]` claim from causing a write when only
# T2's source bullet actually changed.
# ----------------------------------------------------------------------


def _sanitize_doc(t1_tail: str = "", t2_tail: str = "") -> str:
    """Build a single_epic source doc with T1 and T2 bullets."""
    return (
        "# Test Epic\n\n"
        "Standing test container. Owner: Saar.\n\n"
        "## Tasks\n\n"
        f"- T1 First task — body line one.{t1_tail}\n\n"
        f"- T2 Second task — body line two.{t2_tail}\n"
    )


def _sanitize_cached_extraction() -> ExtractionResult:
    return _single([
        _task("T1 First task", "T1 First task"),
        _task("T2 Second task", "T2 Second task"),
    ])


def test_sanitizer_drops_modified_when_bullet_unchanged():
    cached_text = _sanitize_doc()
    current_text = _sanitize_doc(t2_tail=" Owner: Saar.")
    labels = DiffLabels(
        modified_anchors=["T1 First task", "T2 Second task"],
        removed_anchors=[], added=[], new_subepics=[], epic_changed=False,
    )
    out = _sanitize_labels_against_source(
        labels, _sanitize_cached_extraction(), cached_text, current_text,
    )
    assert out.modified_anchors == ["T2 Second task"]
    assert out.epic_changed is False


def test_sanitizer_keeps_modified_when_bullet_changed():
    cached_text = _sanitize_doc()
    current_text = _sanitize_doc(t1_tail=" extra", t2_tail=" Owner: Saar.")
    labels = DiffLabels(
        modified_anchors=["T1 First task", "T2 Second task"],
        removed_anchors=[], added=[], new_subepics=[], epic_changed=False,
    )
    out = _sanitize_labels_against_source(
        labels, _sanitize_cached_extraction(), cached_text, current_text,
    )
    assert sorted(out.modified_anchors) == ["T1 First task", "T2 Second task"]


def test_sanitizer_drops_epic_changed_when_intro_unchanged():
    cached_text = _sanitize_doc()
    current_text = _sanitize_doc(t2_tail=" Owner: Saar.")
    labels = DiffLabels(
        modified_anchors=[], removed_anchors=[], added=[],
        new_subepics=[], epic_changed=True,
    )
    out = _sanitize_labels_against_source(
        labels, _sanitize_cached_extraction(), cached_text, current_text,
    )
    assert out.epic_changed is False


def test_sanitizer_keeps_epic_changed_when_intro_changed():
    cached_text = _sanitize_doc()
    current_text = cached_text.replace(
        "Standing test container. Owner: Saar.",
        "Standing test container, expanded scope. Owner: Saar.",
    )
    labels = DiffLabels(
        modified_anchors=[], removed_anchors=[], added=[],
        new_subepics=[], epic_changed=True,
    )
    out = _sanitize_labels_against_source(
        labels, _sanitize_cached_extraction(), cached_text, current_text,
    )
    assert out.epic_changed is True


def test_sanitizer_drops_unknown_anchor():
    """If the LLM returns an anchor that isn't in the cached extraction,
    drop it — it's hallucination. Empirically, unknown anchors are
    never the right answer (the LLM should echo the cached anchor)."""
    cached_text = _sanitize_doc()
    current_text = _sanitize_doc(t2_tail=" Owner: Saar.")
    labels = DiffLabels(
        modified_anchors=["totally-hallucinated-anchor"],
        removed_anchors=[], added=[], new_subepics=[], epic_changed=False,
    )
    out = _sanitize_labels_against_source(
        labels, _sanitize_cached_extraction(), cached_text, current_text,
    )
    assert out.modified_anchors == []


# ----------------------------------------------------------------------
# _fuzzy_lookup_anchor — recover cached anchor from LLM-prefixed format
# ----------------------------------------------------------------------


def test_fuzzy_lookup_anchor_matches_section_slash_prefix():
    """LLM violates rule B with `"<section> / <cached>"`; fuzzy fallback
    recovers by splitting on `' / '`."""
    cached = {
        "T1 First task": "bullet T1 body",
        "T2 Second task": "bullet T2 body",
    }
    out = file_extract._fuzzy_lookup_anchor(
        "Section A / T2 Second task", cached,
    )
    assert out == "T2 Second task"


def test_fuzzy_lookup_anchor_matches_double_colon_prefix():
    """LLM emits `"<file>::<cached>"`; recover via `'::'` separator."""
    cached = {"T1 First task": "b1", "T2 Second task": "b2"}
    out = file_extract._fuzzy_lookup_anchor(
        "File.md::T1 First task", cached,
    )
    assert out == "T1 First task"


def test_fuzzy_lookup_anchor_suffix_match_picks_longest():
    """When the emitted anchor ends with one of several cached anchors,
    pick the LONGEST match to avoid false positives on short prefixes."""
    cached = {
        "T1": "short",
        "T1 First task with detail": "long",
    }
    out = file_extract._fuzzy_lookup_anchor(
        "Section A / T1 First task with detail", cached,
    )
    assert out == "T1 First task with detail"


def test_fuzzy_lookup_anchor_returns_none_for_truly_hallucinated():
    """Anchor that doesn't suffix-match or split-match any cached entry
    returns None — caller drops as hallucination."""
    cached = {"T1 First task": "b"}
    out = file_extract._fuzzy_lookup_anchor(
        "made-up-anchor-name-completely-unrelated", cached,
    )
    assert out is None


def test_fuzzy_lookup_anchor_returns_none_for_empty_inputs():
    assert file_extract._fuzzy_lookup_anchor("", {"T1": "b"}) is None
    assert file_extract._fuzzy_lookup_anchor("X", {}) is None


# ----------------------------------------------------------------------
# _filter_real_modified — exact + fuzzy fallback paths
# ----------------------------------------------------------------------


def test_filter_real_modified_keeps_exact_match():
    """The simple case still works: exact-match anchor whose bullet
    actually changed in the current text is kept."""
    out = file_extract._filter_real_modified(
        ["T1 First task"],
        cached_bullets={"T1 First task": "old bullet body"},
        current_text="document with new bullet body",
    )
    assert out == ["T1 First task"]


def test_filter_real_modified_recovers_section_prefixed_anchor():
    """LLM emits `"<section> / <cached>"`; filter recovers via fuzzy
    fallback and returns the cached anchor (NOT the LLM-emitted form)."""
    out = file_extract._filter_real_modified(
        ["Section A / T1 First task"],
        cached_bullets={"T1 First task": "old bullet body"},
        current_text="document with new bullet body",
    )
    assert out == ["T1 First task"], (
        "fuzzy recovery should return the cached anchor, not the "
        "LLM-emitted prefixed form"
    )


def test_filter_real_modified_recovered_anchor_still_drops_if_unchanged():
    """Fuzzy recovery does not bypass the over-emission check: even
    if the anchor recovers, if its cached bullet is byte-identical
    in the current text, drop it."""
    out = file_extract._filter_real_modified(
        ["Section A / T1 First task"],
        cached_bullets={"T1 First task": "still here"},
        current_text="this current text contains still here unchanged",
    )
    assert out == [], (
        "recovered anchor should still drop when bullet is unchanged"
    )


def test_filter_real_modified_drops_truly_hallucinated_anchor():
    """An anchor that has no cached match AND no fuzzy recovery is
    dropped (existing behavior — hallucination guard)."""
    out = file_extract._filter_real_modified(
        ["totally-made-up-name"],
        cached_bullets={"T1 First task": "old"},
        current_text="document with new",
    )
    assert out == []


# ----------------------------------------------------------------------
# _lookup_key_for_anchor — compound-anchor task-suffix splitting
# ----------------------------------------------------------------------


def test_lookup_key_simple_anchor_returns_unchanged():
    assert file_extract._lookup_key_for_anchor("T1 First task") == "T1 First task"


def test_lookup_key_compound_anchor_returns_task_suffix():
    """The cold extractor produces compound anchors like
    "<section> / <task>" for multi-epic files. The lookup key is the
    task suffix (after the last separator)."""
    assert file_extract._lookup_key_for_anchor(
        "Section A / T1 First task",
    ) == "T1 First task"


def test_lookup_key_compound_anchor_uses_LAST_separator():
    """If somehow the section name itself contains ' / ', split on the
    LAST occurrence — the task suffix is what's after the last ' / '."""
    assert file_extract._lookup_key_for_anchor(
        "Section A / sub-area / TASK-9",
    ) == "TASK-9"


# ----------------------------------------------------------------------
# _extract_task_bullets — compound-anchor end-to-end
# ----------------------------------------------------------------------


def _make_compound_extraction() -> MultiExtractionResult:
    """Multi-epic extraction whose anchors use the LLM's compound
    "<section> / <task>" convention."""
    desc = (
        "context.\n\n### Goal\nshipped\n\n"
        "### Implementation steps\n1. do it. File: x. Done when: ok.\n\n"
        "### Definition of Done\n- [ ] step done\n- [ ] tests pass\n\n"
        "### Source\n- Doc: F.md\n- Last edited by: dev\n\n"
        f"{AGENT_MARKER}"
    )
    epic_a = ExtractedEpicWithTasks(
        summary="Section A area",
        description="A body",
        assignee_name=None,
        tasks=[
            ExtractedTask(
                summary="A1 task",
                description=desc,
                source_anchor="A. Section / A1",
                assignee_name=None,
            ),
            ExtractedTask(
                summary="A2 task",
                description=desc,
                source_anchor="A. Section / A2",
                assignee_name=None,
            ),
        ],
    )
    return MultiExtractionResult(file_id="F1", file_name="F.md", epics=[epic_a])


def test_extract_task_bullets_handles_compound_anchors():
    """Compound anchors like 'A. Section / A1' don't appear literally
    in the source markdown — the section heading and the bullet are on
    different lines. Bug fix: split on ' / ' and match the task suffix
    on the bullet's line."""
    cached_text = (
        "# Doc\n\n"
        "Overview text.\n\n"
        "## A. Section\n\n"
        "- A1: do the first thing\n"
        "- A2: do the second thing\n"
    )
    cached = _make_compound_extraction()
    out = file_extract._extract_task_bullets(cached_text, cached)
    assert "A. Section / A1" in out, (
        f"compound anchor 'A. Section / A1' should map to its bullet; got keys={list(out.keys())}"
    )
    assert "A. Section / A2" in out
    assert "A1" in out["A. Section / A1"]
    assert "A2" in out["A. Section / A2"]


def test_extract_task_bullets_handles_simple_anchors():
    """Backwards-compat: non-compound anchors (the cold extractor's
    convention for single-epic files) still match by literal substring."""
    cached_text = (
        "# Single Epic\n\n"
        "- T1 First task: do the first thing\n"
        "- T2 Second task: do the second thing\n"
    )
    desc = f"body\n\n{AGENT_MARKER}"
    cached = _single([
        _task("T1 First task", "T1 First task"),
        _task("T2 Second task", "T2 Second task"),
    ])
    out = file_extract._extract_task_bullets(cached_text, cached)
    assert "T1 First task" in out
    assert "T2 Second task" in out


def test_extract_task_bullets_two_compound_anchors_dont_claim_same_line():
    """Two anchors with the same task suffix shouldn't both claim the
    same line (consumed-set guard)."""
    cached_text = (
        "## A. Section\n\n"
        "- T1: A area task\n\n"
        "## B. Other Section\n\n"
        "- T1: B area task\n"
    )
    desc = f"body\n\n{AGENT_MARKER}"
    epic_a = ExtractedEpicWithTasks(
        summary="A",
        description="A body",
        assignee_name=None,
        tasks=[
            ExtractedTask(
                summary="A T1", description=desc,
                source_anchor="A. Section / T1", assignee_name=None,
            ),
        ],
    )
    epic_b = ExtractedEpicWithTasks(
        summary="B",
        description="B body",
        assignee_name=None,
        tasks=[
            ExtractedTask(
                summary="B T1", description=desc,
                source_anchor="B. Other Section / T1", assignee_name=None,
            ),
        ],
    )
    cached = MultiExtractionResult(
        file_id="F1", file_name="F.md", epics=[epic_a, epic_b],
    )
    out = file_extract._extract_task_bullets(cached_text, cached)
    # Each compound anchor gets its OWN bullet, not the same one.
    assert "A. Section / T1" in out
    assert "B. Other Section / T1" in out
    assert "A area task" in out["A. Section / T1"]
    assert "B area task" in out["B. Other Section / T1"]


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------


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


def _classify(role: str = "single_epic") -> ClassifyResult:
    return ClassifyResult(file_id="F1", role=role, confidence=1.0, reason="x")


def _task_desc(body: str = "body") -> str:
    return (
        f"{body}\n\n### Definition of Done\n"
        "- [ ] one\n- [ ] two\n- [ ] three\n\n"
        f"{AGENT_MARKER}"
    )


def _task(summary: str, anchor: str, body: str = "body") -> ExtractedTask:
    return ExtractedTask(
        summary=summary, description=_task_desc(body), source_anchor=anchor,
    )


def _single(tasks: list[ExtractedTask]) -> ExtractionResult:
    return ExtractionResult(
        file_id="F1", file_name="F.md",
        epic=ExtractedEpic(summary="Title", description=f"x\n\n{AGENT_MARKER}"),
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


class _Hooks:
    def __init__(self):
        self.ok = 0
        self.failed: list[str] = []
        self.cache_hits = 0

    def triple(self):
        return (
            lambda: self.__setattr__("ok", self.ok + 1),
            lambda m: self.failed.append(m),
            lambda: self.__setattr__("cache_hits", self.cache_hits + 1),
        )


def _seed_warm_cache(cache: Cache, ext, file_text: str) -> None:
    """Populate cache so the diff path is usable: extraction + file_text
    in diff_payload."""
    cache.set_extraction(
        file_id="F1", modified_time="t", content_sha="OLD",
        extraction_payload=serialize_extraction(ext),
    )
    cache.set_file_text("F1", file_text)


# ----------------------------------------------------------------------
# Tier 2 hit
# ----------------------------------------------------------------------


def test_tier2_hit_returns_empty_dirty(tmp_path, monkeypatch):
    cache = Cache()
    cached = _single([_task("T", "anchor-T")])
    cache.set_extraction(
        file_id="F1", modified_time="t", content_sha="C",
        extraction_payload=serialize_extraction(cached),
    )
    p = tmp_path / "f.md"
    p.write_text("# T\n## A\nbody")
    monkeypatch.setattr(
        extractor_mod, "chat",
        lambda **_: (_ for _ in ()).throw(AssertionError("no LLM on Tier 2")),
    )
    hooks = _Hooks()
    ok, fail, hit = hooks.triple()
    out, dirty = extract_or_reuse(
        _df(), classification=_classify(),
        local_path=p, content_sha="C", root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=ok, on_extract_failed=fail, on_cache_hit_extract=hit,
    )
    assert out is not None
    assert dirty == set()
    assert hooks.cache_hits == 1


# ----------------------------------------------------------------------
# Cold path
# ----------------------------------------------------------------------


def test_cold_path_returns_dirty_none(tmp_path, monkeypatch):
    cache = Cache()
    p = tmp_path / "f.md"
    p.write_text("# T\n## A\nfresh body.")

    def _fake(**_):
        return ({
            "epic": {
                "summary": "Dashboard improvements",  # >=12 chars
                "description": "body.", "assignee": None,
            },
            "tasks": [{
                "summary": "Cold-extracted task",
                "description": _task_desc("body"),
                "source_anchor": "cold-1",
                "assignee": None,
            }],
        }, {"model": "stub"})
    monkeypatch.setattr(extractor_mod, "chat", _fake)

    hooks = _Hooks()
    ok, fail, hit = hooks.triple()
    out, dirty = extract_or_reuse(
        _df(), classification=_classify(),
        local_path=p, content_sha="NEW", root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=ok, on_extract_failed=fail, on_cache_hit_extract=hit,
    )
    assert out is not None, f"failures: {hooks.failed}"
    assert out.tasks[0].summary == "Cold-extracted task"
    assert dirty is None
    assert cache.get_extraction("F1", "NEW") is not None
    assert cache.get_file_text("F1") == "# T\n## A\nfresh body."


def test_cold_failure_returns_none(tmp_path, monkeypatch):
    cache = Cache()
    p = tmp_path / "f.md"
    p.write_text("# T")
    monkeypatch.setattr(
        extractor_mod, "chat",
        lambda **_: (None, {"model": "stub"}),
    )
    hooks = _Hooks()
    ok, fail, hit = hooks.triple()
    out, dirty = extract_or_reuse(
        _df(), classification=_classify(),
        local_path=p, content_sha="X", root_context="",
        cache=cache, use_cache=False,
        on_extract_ok=ok, on_extract_failed=fail, on_cache_hit_extract=hit,
    )
    assert out is None
    assert hooks.failed and "extract failed" in hooks.failed[0]


def test_unknown_role_returns_none(tmp_path):
    cache = Cache()
    p = tmp_path / "f.md"
    p.write_text("# T")
    hooks = _Hooks()
    ok, fail, hit = hooks.triple()
    out, dirty = extract_or_reuse(
        _df(), classification=_classify(role="root"),
        local_path=p, content_sha="X", root_context="",
        cache=cache, use_cache=False,
        on_extract_ok=ok, on_extract_failed=fail, on_cache_hit_extract=hit,
    )
    assert out is None
    assert hooks.failed == []


# ----------------------------------------------------------------------
# Diff path — single_epic
# ----------------------------------------------------------------------


def test_diff_path_single_epic_modified_only(tmp_path, monkeypatch):
    """One task body edited → dirty = {that one anchor}."""
    cache = Cache()
    cached = _single([
        _task("Task A", "anchor-A", "body A"),
        _task("Task B", "anchor-B", "body B"),
    ])
    _seed_warm_cache(
        cache, cached,
        "# T\n- anchor-A — A body line.\n- anchor-B — B body line.\n",
    )

    mutated_path = tmp_path / "f.md"
    mutated_path.write_text(
        "# T\n- anchor-A — A body line edited.\n- anchor-B — B body line.\n"
    )

    # Stub both LLM calls: diff returns the label, targeted returns the
    # body. apply_changes matches bodies to cached tasks by source_anchor,
    # so the targeted-body anchor must echo the modified anchor.
    monkeypatch.setattr(file_extract, "extract_diff", lambda *a, **kw: DiffLabels(
        modified_anchors=["anchor-A"], removed_anchors=[],
        added=[], new_subepics=[], epic_changed=False,
    ))
    monkeypatch.setattr(file_extract, "extract_targeted", lambda *a, **kw: TargetedBodies(
        tasks=[_task("Task A edited", "anchor-A", "edited body")],
        task_sections=[None],
        epics=[], epic_sections=[],
    ))

    hooks = _Hooks()
    ok, fail, hit = hooks.triple()
    out, dirty = extract_or_reuse(
        _df(), classification=_classify(),
        local_path=mutated_path, content_sha="NEW", root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=ok, on_extract_failed=fail, on_cache_hit_extract=hit,
    )
    assert out is not None
    summaries = [t.summary for t in out.tasks]
    assert "Task A edited" in summaries
    assert "Task B" in summaries
    assert "Task A" not in summaries
    assert dirty == {"anchor-A"}


def test_diff_path_drops_hallucinated_body_for_unsolicited_anchor(
    tmp_path, monkeypatch,
):
    """Bug 1 regression: when extract_targeted returns a body for a
    cached task that was NOT in modified_anchors, that body must be
    dropped — not appended as if it were a new add."""
    cache = Cache()
    cached = _single([
        _task("Task A", "anchor-A", "body A"),
        _task("Task B", "anchor-B", "body B"),
    ])
    _seed_warm_cache(
        cache, cached,
        "# T\n- anchor-A — A body\n- anchor-B — B body\n",
    )

    p = tmp_path / "f.md"
    p.write_text("# T\n- anchor-A — A body edited\n- anchor-B — B body\n")

    # Diff says only A was modified.
    monkeypatch.setattr(file_extract, "extract_diff", lambda *a, **kw: DiffLabels(
        modified_anchors=["anchor-A"], removed_anchors=[],
        added=[], new_subepics=[], epic_changed=False,
    ))
    # LLM hallucinates: returns a body for B too even though we didn't ask.
    monkeypatch.setattr(file_extract, "extract_targeted", lambda *a, **kw: TargetedBodies(
        tasks=[
            _task("Task A edited", "anchor-A", "edited A body"),
            _task("Task B (rewritten)", "anchor-B", "rewritten B body"),
        ],
        task_sections=[None, None],
        epics=[], epic_sections=[],
    ))

    hooks = _Hooks()
    ok, fail, hit = hooks.triple()
    out, dirty = extract_or_reuse(
        _df(), classification=_classify(),
        local_path=p, content_sha="NEW", root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=ok, on_extract_failed=fail, on_cache_hit_extract=hit,
    )
    summaries = [t.summary for t in out.tasks]
    assert "Task A edited" in summaries
    assert "Task B" in summaries
    assert "Task B (rewritten)" not in summaries
    assert dirty == {"anchor-A"}


def test_diff_path_drops_hallucinated_epic_body(tmp_path, monkeypatch):
    """Bug 1 regression: when extract_targeted returns an epic body
    but labels.epic_changed=False and no new_subepics requested, drop
    it — don't let it leak into merged.epic."""
    cache = Cache()
    cached = _single([_task("Task A", "anchor-A")])
    _seed_warm_cache(cache, cached, "# Epic\nA bullet.\n")

    p = tmp_path / "f.md"
    p.write_text("# Epic\nA bullet edited.\n")

    monkeypatch.setattr(file_extract, "extract_diff", lambda *a, **kw: DiffLabels(
        modified_anchors=["anchor-A"], removed_anchors=[],
        added=[], new_subepics=[], epic_changed=False,
    ))
    monkeypatch.setattr(file_extract, "extract_targeted", lambda *a, **kw: TargetedBodies(
        tasks=[_task("Task A edited", "anchor-A", "edited A body")],
        task_sections=[None],
        epics=[ExtractedEpicWithTasks(
            summary="Hallucinated epic rename", description="rewritten",
            assignee_name=None, tasks=[],
        )],
        epic_sections=[None],
    ))

    hooks = _Hooks()
    ok, fail, hit = hooks.triple()
    out, dirty = extract_or_reuse(
        _df(), classification=_classify(),
        local_path=p, content_sha="NEW", root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=ok, on_extract_failed=fail, on_cache_hit_extract=hit,
    )
    # Epic body unchanged — hallucination dropped.
    assert "<epic>:0" not in dirty
    assert out.epic.summary == cached.epic.summary


def test_diff_path_single_epic_added_appended(tmp_path, monkeypatch):
    cache = Cache()
    cached = _single([_task("Task A", "anchor-A")])
    _seed_warm_cache(cache, cached, "old text\n")
    p = tmp_path / "f.md"
    p.write_text("new text with extra task\n")

    monkeypatch.setattr(file_extract, "extract_diff", lambda *a, **kw: DiffLabels(
        modified_anchors=[], removed_anchors=[],
        added=[{"summary": "Brand new", "section": None}],
        new_subepics=[], epic_changed=False,
    ))
    monkeypatch.setattr(file_extract, "extract_targeted", lambda *a, **kw: TargetedBodies(
        tasks=[_task("Brand new", "anchor-NEW")],
        task_sections=[None],
        epics=[], epic_sections=[],
    ))

    hooks = _Hooks()
    ok, fail, hit = hooks.triple()
    out, dirty = extract_or_reuse(
        _df(), classification=_classify(),
        local_path=p, content_sha="NEW", root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=ok, on_extract_failed=fail, on_cache_hit_extract=hit,
    )
    assert {t.summary for t in out.tasks} == {"Task A", "Brand new"}
    assert dirty == {"anchor-NEW"}


def test_diff_path_removed_drops_by_anchor(tmp_path, monkeypatch):
    cache = Cache()
    cached = _single([_task("T1", "a1"), _task("T2", "a2")])
    _seed_warm_cache(cache, cached, "old\n")
    p = tmp_path / "f.md"
    p.write_text("new (T2 deleted)\n")

    monkeypatch.setattr(file_extract, "extract_diff", lambda *a, **kw: DiffLabels(
        modified_anchors=[], removed_anchors=["a2"],
        added=[], new_subepics=[], epic_changed=False,
    ))
    monkeypatch.setattr(file_extract, "extract_targeted", lambda *a, **kw: TargetedBodies(
        tasks=[], task_sections=[], epics=[], epic_sections=[],
    ))

    hooks = _Hooks()
    ok, fail, hit = hooks.triple()
    out, dirty = extract_or_reuse(
        _df(), classification=_classify(),
        local_path=p, content_sha="NEW", root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=ok, on_extract_failed=fail, on_cache_hit_extract=hit,
    )
    assert [t.summary for t in out.tasks] == ["T1"]
    # Removed anchors are NOT counted as dirty (the doc deleted them; no
    # write action is needed beyond the cached state diverging — the
    # reconciler currently has no orphan-on-delete behavior either).
    assert dirty == set()


def test_diff_path_no_real_diff_falls_back_to_cold(tmp_path, monkeypatch):
    """If cached_text == current_text the diff is empty → no LLM call,
    we fall through to cold (which the test stubs to avoid Drive)."""
    cache = Cache()
    cached = _single([_task("T", "a")])
    _seed_warm_cache(cache, cached, "same content\n")
    p = tmp_path / "f.md"
    p.write_text("same content\n")  # identical → empty diff

    cold_called = {"n": 0}

    def _fake(**_):
        cold_called["n"] += 1
        return ({
            "epic": {"summary": "Recovered fresh title", "description": "x", "assignee": None},
            "tasks": [{
                "summary": "Cold task",
                "description": _task_desc("body"),
                "source_anchor": "cold-1",
                "assignee": None,
            }],
        }, {"model": "stub"})
    monkeypatch.setattr(extractor_mod, "chat", _fake)

    hooks = _Hooks()
    ok, fail, hit = hooks.triple()
    out, dirty = extract_or_reuse(
        _df(), classification=_classify(),
        local_path=p, content_sha="NEW",  # mismatch → Tier 2 misses
        root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=ok, on_extract_failed=fail, on_cache_hit_extract=hit,
    )
    assert out is not None
    assert cold_called["n"] == 1  # cold extractor invoked
    assert dirty is None  # cold path semantics


# ----------------------------------------------------------------------
# Diff path — multi_epic
# ----------------------------------------------------------------------


def test_diff_path_multi_epic_added_routed_by_section_summary(tmp_path, monkeypatch):
    cache = Cache()
    cached = _multi([
        ("Security", [_task("SEC-A", "sec-a")]),
        ("Monitoring", [_task("MON-A", "mon-a")]),
    ])
    _seed_warm_cache(cache, cached, "old multi\n")
    p = tmp_path / "f.md"
    p.write_text("new multi with MON-NEW\n")

    monkeypatch.setattr(file_extract, "extract_diff", lambda *a, **kw: DiffLabels(
        modified_anchors=[], removed_anchors=[],
        added=[{"summary": "MON-NEW burst", "section": "Monitoring"}],
        new_subepics=[], epic_changed=False,
    ))
    monkeypatch.setattr(file_extract, "extract_targeted", lambda *a, **kw: TargetedBodies(
        tasks=[_task("MON-NEW burst", "MON-NEW anchor")],
        task_sections=["Monitoring"],
        epics=[], epic_sections=[],
    ))

    hooks = _Hooks()
    ok, fail, hit = hooks.triple()
    out, dirty = extract_or_reuse(
        _df(), classification=_classify(role="multi_epic"),
        local_path=p, content_sha="NEW", root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=ok, on_extract_failed=fail, on_cache_hit_extract=hit,
    )
    assert isinstance(out, MultiExtractionResult)
    sec = next(e for e in out.epics if e.summary == "Security")
    mon = next(e for e in out.epics if e.summary == "Monitoring")
    assert {t.source_anchor for t in mon.tasks} == {"mon-a", "MON-NEW anchor"}
    assert {t.source_anchor for t in sec.tasks} == {"sec-a"}
    assert dirty == {"MON-NEW anchor"}


def test_diff_path_multi_epic_new_subepic_with_tasks(tmp_path, monkeypatch):
    cache = Cache()
    cached = _multi([("E1", [_task("T1", "t1")])])
    _seed_warm_cache(cache, cached, "old\n")
    p = tmp_path / "f.md"
    p.write_text("new with section J\n")

    new_sub = ExtractedEpicWithTasks(
        summary="Disaster recovery readiness",
        description=f"Validate runbooks.\n\n{AGENT_MARKER}",
        assignee_name=None, tasks=[],
    )
    monkeypatch.setattr(file_extract, "extract_diff", lambda *a, **kw: DiffLabels(
        modified_anchors=[], removed_anchors=[],
        added=[
            {"summary": "DR-1 cadence", "section": "Disaster recovery readiness"},
            {"summary": "DR-2 restore", "section": "Disaster recovery readiness"},
        ],
        new_subepics=[{"summary": "Disaster recovery readiness"}],
        epic_changed=False,
    ))
    monkeypatch.setattr(file_extract, "extract_targeted", lambda *a, **kw: TargetedBodies(
        tasks=[_task("DR-1 cadence", "DR-1"), _task("DR-2 restore", "DR-2")],
        task_sections=["Disaster recovery readiness", "Disaster recovery readiness"],
        epics=[new_sub],
        epic_sections=["Disaster recovery readiness"],
    ))

    hooks = _Hooks()
    ok, fail, hit = hooks.triple()
    out, dirty = extract_or_reuse(
        _df(), classification=_classify(role="multi_epic"),
        local_path=p, content_sha="NEW", root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=ok, on_extract_failed=fail, on_cache_hit_extract=hit,
    )
    assert len(out.epics) == 2
    new = next(e for e in out.epics if "Disaster" in e.summary)
    assert {t.source_anchor for t in new.tasks} == {"DR-1", "DR-2"}
    # Two task anchors plus the new sub-epic's token (section index 1).
    assert dirty == {"DR-1", "DR-2", "<epic>:1"}


# ----------------------------------------------------------------------
# Cache reuse
# ----------------------------------------------------------------------


def test_use_cache_false_bypasses_tier2(tmp_path, monkeypatch):
    """--no-cache equivalent: cold extract runs even when Tier 2 hit."""
    cache = Cache()
    cached = _single([_task("STALE", "stale-a")])
    cache.set_extraction(
        file_id="F1", modified_time="t", content_sha="C",
        extraction_payload=serialize_extraction(cached),
    )
    p = tmp_path / "f.md"
    p.write_text("# T\n## A\nbody")

    def _fake(**_):
        return ({
            "epic": {"summary": "Recovered fresh title", "description": "x", "assignee": None},
            "tasks": [{
                "summary": "Fresh task",
                "description": _task_desc("body"),
                "source_anchor": "fresh-1",
                "assignee": None,
            }],
        }, {"model": "stub"})
    monkeypatch.setattr(extractor_mod, "chat", _fake)

    hooks = _Hooks()
    ok, fail, hit = hooks.triple()
    out, dirty = extract_or_reuse(
        _df(), classification=_classify(),
        local_path=p, content_sha="C", root_context="",
        cache=cache, use_cache=False,
        on_extract_ok=ok, on_extract_failed=fail, on_cache_hit_extract=hit,
    )
    assert out is not None
    assert out.tasks[0].summary == "Fresh task"
    assert hooks.cache_hits == 0  # use_cache=False → no Tier 2 read


def _run_loop(
    monkeypatch, tmp_path,
    *,
    cached, role, mutated_text,
    labels, bodies, expected_dirty, expected_post_state,
):
    """Cold seed → mutate → warm extract → rerun with same content.
    Asserts dirty set after warm, then asserts the rerun is a Tier 2
    hit (no LLM calls) returning the post-mutation state."""
    cache = Cache()
    # Realistic cached file_text: each cached task's anchor appears as
    # a bullet line so the sanitizer's bullet-location logic can find
    # them. Otherwise the new "drop unknown modified_anchor" guard
    # would refuse to mark anchors as dirty.
    seed_lines = ["# Cached file\n"]
    if hasattr(cached, "epics"):
        for e in cached.epics:
            seed_lines.append(f"## {e.summary}\n")
            for t in e.tasks:
                seed_lines.append(f"- {t.source_anchor} — body\n")
    else:
        seed_lines.append(f"## {cached.epic.summary}\n")
        for t in cached.tasks:
            seed_lines.append(f"- {t.source_anchor} — body\n")
    _seed_warm_cache(cache, cached, "".join(seed_lines))

    mutated_path = tmp_path / "f.md"
    mutated_path.write_text(mutated_text)

    monkeypatch.setattr(file_extract, "extract_diff", lambda *a, **kw: labels)
    monkeypatch.setattr(file_extract, "extract_targeted", lambda *a, **kw: bodies)

    hooks = _Hooks()
    ok, fail, hit = hooks.triple()
    _, dirty = extract_or_reuse(
        _df(), classification=_classify(role=role),
        local_path=mutated_path, content_sha="NEW", root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=ok, on_extract_failed=fail, on_cache_hit_extract=hit,
    )
    assert dirty == expected_dirty
    assert cache.get_extraction("F1", "NEW") is not None
    assert cache.get_file_text("F1") == mutated_text

    bomb = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no LLM on Tier 2"))
    monkeypatch.setattr(extractor_mod, "chat", bomb)
    monkeypatch.setattr(file_extract, "extract_diff", bomb)
    monkeypatch.setattr(file_extract, "extract_targeted", bomb)

    out2, dirty2 = extract_or_reuse(
        _df(), classification=_classify(role=role),
        local_path=mutated_path, content_sha="NEW", root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=ok, on_extract_failed=fail, on_cache_hit_extract=hit,
    )
    assert dirty2 == set()
    expected_post_state(out2)


def test_persistence_loop_modify_task(tmp_path, monkeypatch):
    cached = _single([_task("Task A", "anchor-A", "body A"), _task("Task B", "anchor-B", "body B")])
    _run_loop(
        monkeypatch, tmp_path,
        cached=cached, role="single_epic",
        mutated_text="# T\n## A\nA body line edited.\n## B\nB body line.\n",
        labels=DiffLabels(
            modified_anchors=["anchor-A"], removed_anchors=[],
            added=[], new_subepics=[], epic_changed=False,
        ),
        bodies=TargetedBodies(
            tasks=[_task("Task A edited", "anchor-A", "edited body")],
            task_sections=[None],
            epics=[], epic_sections=[],
        ),
        expected_dirty={"anchor-A"},
        expected_post_state=lambda out: (
            {t.summary for t in out.tasks} == {"Task A edited", "Task B"}
        ),
    )


def test_persistence_loop_add_task(tmp_path, monkeypatch):
    cached = _single([_task("Task A", "anchor-A")])
    _run_loop(
        monkeypatch, tmp_path,
        cached=cached, role="single_epic",
        mutated_text="# T\n## A\nbody.\n## B\nNEW task body.\n",
        labels=DiffLabels(
            modified_anchors=[], removed_anchors=[],
            added=[{"summary": "New task", "section": None}],
            new_subepics=[], epic_changed=False,
        ),
        bodies=TargetedBodies(
            tasks=[_task("New task", "anchor-NEW", "new body")],
            task_sections=[None],
            epics=[], epic_sections=[],
        ),
        expected_dirty={"anchor-NEW"},
        expected_post_state=lambda out: (
            {t.summary for t in out.tasks} == {"Task A", "New task"}
        ),
    )


def test_persistence_loop_update_epic(tmp_path, monkeypatch):
    """Single-epic body change → dirty={'<epic>:0'}; epic replaced."""
    cached = _single([_task("T1", "a1")])
    new_epic_full = ExtractedEpicWithTasks(
        summary="Updated epic title",
        description=f"updated body.\n\n{AGENT_MARKER}",
        assignee_name=None, tasks=[],
    )
    _run_loop(
        monkeypatch, tmp_path,
        cached=cached, role="single_epic",
        mutated_text="# Updated epic title\n## A\nbody.\n",
        labels=DiffLabels(
            modified_anchors=[], removed_anchors=[],
            added=[], new_subepics=[], epic_changed=True,
        ),
        bodies=TargetedBodies(
            tasks=[], task_sections=[],
            epics=[new_epic_full], epic_sections=[None],
        ),
        expected_dirty={"<epic>:0"},
        expected_post_state=lambda out: (
            out.epic.summary == "Updated epic title"
            and [t.source_anchor for t in out.tasks] == ["a1"]
        ),
    )


def test_persistence_loop_add_subepic(tmp_path, monkeypatch):
    """Multi-epic add new sub-epic with one task → dirty has the
    sub-epic token + the new task anchor."""
    cached = _multi([("E1", [_task("T1", "a1")])])
    new_sub = ExtractedEpicWithTasks(
        summary="New section title",
        description=f"new section body\n\n{AGENT_MARKER}",
        assignee_name=None, tasks=[],
    )
    _run_loop(
        monkeypatch, tmp_path,
        cached=cached, role="multi_epic",
        mutated_text="# T\n\n## A. E1\nbody.\n\n## B. New\nNew task body.\n",
        labels=DiffLabels(
            modified_anchors=[], removed_anchors=[],
            added=[{"summary": "New task", "section": "New section title"}],
            new_subepics=[{"summary": "New section title"}],
            epic_changed=False,
        ),
        bodies=TargetedBodies(
            tasks=[_task("New task", "anchor-NEW", "new body")],
            task_sections=["New section title"],
            epics=[new_sub],
            epic_sections=["New section title"],
        ),
        expected_dirty={"anchor-NEW", "<epic>:1"},
        expected_post_state=lambda out: (
            len(out.epics) == 2
            and out.epics[1].summary == "New section title"
            and any(t.source_anchor == "anchor-NEW" for t in out.epics[1].tasks)
        ),
    )


def test_corrupt_tier2_payload_falls_through_to_diff_or_cold(tmp_path, monkeypatch):
    """If the cached payload can't be deserialized, log + fall through."""
    cache = Cache()
    cache.set_extraction(
        file_id="F1", modified_time="t", content_sha="C",
        extraction_payload={"type": "single_epic", "BROKEN": True},
    )
    p = tmp_path / "f.md"
    p.write_text("# T\n## A\nbody")

    def _fake(**_):
        return ({
            "epic": {"summary": "Recovered fresh title", "description": "x", "assignee": None},
            "tasks": [{
                "summary": "Recovery task",
                "description": _task_desc("body"),
                "source_anchor": "rec-1",
                "assignee": None,
            }],
        }, {"model": "stub"})
    monkeypatch.setattr(extractor_mod, "chat", _fake)

    hooks = _Hooks()
    ok, fail, hit = hooks.triple()
    out, dirty = extract_or_reuse(
        _df(), classification=_classify(),
        local_path=p, content_sha="C", root_context="",
        cache=cache, use_cache=True,
        on_extract_ok=ok, on_extract_failed=fail, on_cache_hit_extract=hit,
    )
    assert out is not None
    assert out.tasks[0].summary == "Recovery task"
    assert hooks.cache_hits == 0
