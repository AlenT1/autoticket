"""Unit tests for the per-file cache.

Covers:
  - Round-trip serialize/deserialize of single_epic + multi_epic extractions.
  - Tier 1 (classification): hit on matching modified_time, miss otherwise.
  - Tier 2 (extraction): hit on matching content_sha, miss otherwise.
  - mtime change drops stale extraction payload.
  - Cache file load/save round-trip via tempfile.
  - Corrupt or version-mismatched file → cold start.
"""
from __future__ import annotations

import json
from pathlib import Path

from jira_task_agent.cache import (
    CACHE_VERSION,
    Cache,
    deserialize_extraction,
    file_content_sha,
    serialize_extraction,
)
from jira_task_agent.pipeline.extractor import (
    AGENT_MARKER,
    ExtractedEpic,
    ExtractedEpicWithTasks,
    ExtractedTask,
    ExtractionResult,
    MultiExtractionResult,
)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _task_desc(text: str) -> str:
    return (
        f"{text}\n\n### Definition of Done\n- [ ] one\n- [ ] two\n- [ ] three\n\n"
        f"{AGENT_MARKER}"
    )


def _single() -> ExtractionResult:
    return ExtractionResult(
        file_id="F1",
        file_name="F1.md",
        epic=ExtractedEpic(
            summary="My Epic", description=f"Epic body.\n\n{AGENT_MARKER}",
            assignee_name="Lior + Aviv",
        ),
        tasks=[
            ExtractedTask(
                summary="Step 1", description=_task_desc("Body 1"),
                source_anchor="anc-1", assignee_name="Lior",
            ),
            ExtractedTask(
                summary="Step 2", description=_task_desc("Body 2"),
                source_anchor="anc-2", assignee_name=None,
            ),
        ],
    )


def _multi() -> MultiExtractionResult:
    return MultiExtractionResult(
        file_id="F2",
        file_name="F2.md",
        epics=[
            ExtractedEpicWithTasks(
                summary="Section A", description=f"Body A.\n\n{AGENT_MARKER}",
                assignee_name="Sharon",
                tasks=[
                    ExtractedTask(
                        summary="A-1", description=_task_desc("ax"),
                        source_anchor="A-1", assignee_name="Sharon",
                    )
                ],
            ),
            ExtractedEpicWithTasks(
                summary="Section B", description=f"Body B.\n\n{AGENT_MARKER}",
                assignee_name="Yuval",
                tasks=[
                    ExtractedTask(
                        summary="B-1", description=_task_desc("bx"),
                        source_anchor="B-1", assignee_name="Yuval",
                    )
                ],
            ),
        ],
    )


# ----------------------------------------------------------------------
# serialize/deserialize round-trip
# ----------------------------------------------------------------------


def test_serialize_roundtrip_single_epic():
    ext = _single()
    payload = serialize_extraction(ext)
    back = deserialize_extraction(payload)
    assert isinstance(back, ExtractionResult)
    assert back.file_id == ext.file_id
    assert back.epic.summary == ext.epic.summary
    assert back.epic.assignee_name == "Lior + Aviv"
    assert [t.summary for t in back.tasks] == ["Step 1", "Step 2"]
    assert back.tasks[0].assignee_name == "Lior"
    assert back.tasks[1].assignee_name is None


def test_serialize_roundtrip_multi_epic():
    ext = _multi()
    payload = serialize_extraction(ext)
    back = deserialize_extraction(payload)
    assert isinstance(back, MultiExtractionResult)
    assert len(back.epics) == 2
    assert back.epics[0].summary == "Section A"
    assert back.epics[1].assignee_name == "Yuval"
    assert back.epics[0].tasks[0].source_anchor == "A-1"


# ----------------------------------------------------------------------
# Tier 1: classification cache
# ----------------------------------------------------------------------


def test_classification_hit_on_matching_mtime():
    c = Cache()
    c.set_classification(
        file_id="F1", modified_time="2026-04-28T12:00:00+00:00",
        content_sha="abc", role="single_epic", confidence=0.95, reason="x",
    )
    hit = c.get_classification("F1", "2026-04-28T12:00:00+00:00")
    assert hit == ("single_epic", 0.95, "x")


def test_classification_miss_on_changed_mtime():
    c = Cache()
    c.set_classification(
        file_id="F1", modified_time="2026-04-28T12:00:00+00:00",
        content_sha="abc", role="single_epic", confidence=0.95, reason="x",
    )
    assert c.get_classification("F1", "2026-04-29T00:00:00+00:00") is None


def test_classification_miss_on_unknown_file():
    c = Cache()
    assert c.get_classification("NEVER", "2026-04-28T12:00:00+00:00") is None


# ----------------------------------------------------------------------
# Tier 2: extraction cache
# ----------------------------------------------------------------------


def test_extraction_hit_on_matching_sha():
    c = Cache()
    c.set_extraction(
        file_id="F1", modified_time="2026-04-28T00:00:00+00:00",
        content_sha="hash-1",
        extraction_payload=serialize_extraction(_single()),
    )
    payload = c.get_extraction("F1", "hash-1")
    assert payload is not None
    assert payload["type"] == "single_epic"


def test_extraction_miss_on_changed_sha():
    c = Cache()
    c.set_extraction(
        file_id="F1", modified_time="2026-04-28T00:00:00+00:00",
        content_sha="hash-1",
        extraction_payload=serialize_extraction(_single()),
    )
    assert c.get_extraction("F1", "hash-2") is None


def test_mtime_change_drops_extraction_payload():
    """If an entry exists for F1 with content_sha=A and we then write a
    new classification for F1 at a different mtime/sha, the extraction
    payload (which was for the old content) MUST be dropped."""
    c = Cache()
    c.set_extraction(
        file_id="F1", modified_time="2026-04-28T00:00:00+00:00",
        content_sha="hash-old",
        extraction_payload=serialize_extraction(_single()),
    )
    assert c.get_extraction("F1", "hash-old") is not None

    # New classification with new content_sha — old extraction must be evicted.
    c.set_classification(
        file_id="F1", modified_time="2026-04-29T00:00:00+00:00",
        content_sha="hash-new", role="single_epic", confidence=0.9, reason="ok",
    )
    assert c.get_extraction("F1", "hash-old") is None
    assert c.get_extraction("F1", "hash-new") is None  # nothing cached for new


# ----------------------------------------------------------------------
# load/save
# ----------------------------------------------------------------------


def test_save_then_load_round_trip(tmp_path: Path):
    c = Cache()
    c.set_classification(
        file_id="F1", modified_time="m1", content_sha="s1",
        role="single_epic", confidence=0.9, reason="r",
    )
    c.set_extraction(
        file_id="F1", modified_time="m1", content_sha="s1",
        extraction_payload=serialize_extraction(_single()),
    )

    p = tmp_path / "cache.json"
    c.save(p)
    assert p.exists()
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["version"] == CACHE_VERSION
    assert "F1" in raw["files"]

    c2 = Cache.load(p)
    assert c2.get_classification("F1", "m1") == ("single_epic", 0.9, "r")
    assert c2.get_extraction("F1", "s1") is not None


def test_load_missing_file_returns_empty_cache(tmp_path: Path):
    p = tmp_path / "doesnt-exist.json"
    c = Cache.load(p)
    assert c.files == {}


def test_load_corrupt_file_returns_empty_cache(tmp_path: Path):
    p = tmp_path / "corrupt.json"
    p.write_text("{not valid json")
    c = Cache.load(p)
    assert c.files == {}


def test_load_version_mismatch_returns_empty_cache(tmp_path: Path):
    p = tmp_path / "old.json"
    p.write_text(json.dumps({"version": 999, "files": {"F1": {}}}))
    c = Cache.load(p)
    assert c.files == {}


# ----------------------------------------------------------------------
# file_content_sha
# ----------------------------------------------------------------------


def test_file_content_sha_changes_with_content(tmp_path: Path):
    p = tmp_path / "x.txt"
    p.write_text("hello")
    a = file_content_sha(p)
    p.write_text("hello world")
    b = file_content_sha(p)
    assert a != b
    assert len(a) == 64  # sha256 hex


# ----------------------------------------------------------------------
# Tier 3: matcher decision cache
# ----------------------------------------------------------------------


def _sample_match_results() -> list[dict]:
    return [
        {
            "file_id": "F1",
            "file_name": "F1.md",
            "section_index": 0,
            "extracted_epic_summary": "Sec",
            "extracted_epic_description": "body",
            "extracted_epic_assignee_raw": None,
            "matched_jira_key": "CENTPM-1",
            "epic_match_confidence": 0.95,
            "epic_match_reason": "ok",
            "task_decisions": [],
            "orphan_keys": [],
        }
    ]


def test_match_hit_when_content_and_prompt_match():
    c = Cache()
    c.set_match(
        file_id="F1",
        modified_time="t",
        content_sha="C",
        prompt_sha="P",
        topology_sha="T",
        results=_sample_match_results(),
    )
    hit = c.get_match("F1", content_sha="C", prompt_sha="P")
    assert hit is not None
    assert hit[0]["matched_jira_key"] == "CENTPM-1"


def test_match_miss_on_content_sha_change():
    c = Cache()
    c.set_match(
        file_id="F1", modified_time="t", content_sha="C", prompt_sha="P",
        topology_sha="T", results=_sample_match_results(),
    )
    assert c.get_match("F1", content_sha="C2", prompt_sha="P") is None


def test_match_hit_survives_topology_change():
    """Doc-only invalidation: if Jira changes (topology_sha would
    differ) but the doc and prompts didn't, the cached decision must
    still be served. Dev edits in Jira don't force re-matching."""
    c = Cache()
    c.set_match(
        file_id="F1", modified_time="t", content_sha="C", prompt_sha="P",
        topology_sha="T-old", results=_sample_match_results(),
    )
    # Same lookup key — get_match doesn't even take topology_sha now.
    assert c.get_match("F1", content_sha="C", prompt_sha="P") is not None


def test_match_miss_on_prompt_change():
    c = Cache()
    c.set_match(
        file_id="F1", modified_time="t", content_sha="C", prompt_sha="P",
        topology_sha="T", results=_sample_match_results(),
    )
    assert c.get_match("F1", content_sha="C", prompt_sha="P2") is None


def test_match_miss_on_unknown_file():
    c = Cache()
    assert c.get_match("ghost", content_sha="x", prompt_sha="z") is None


def test_set_extraction_invalidates_matcher_payload():
    """A fresh extraction must drop any prior matcher decision — the
    matcher's input changed."""
    c = Cache()
    c.set_match(
        file_id="F1", modified_time="t", content_sha="C", prompt_sha="P",
        topology_sha="T", results=_sample_match_results(),
    )
    assert c.get_match("F1", content_sha="C", prompt_sha="P") is not None
    c.set_extraction(
        file_id="F1", modified_time="t", content_sha="C2",
        extraction_payload={"some": "payload"},
    )
    assert c.get_match("F1", content_sha="C", prompt_sha="P") is None
    assert c.get_match("F1", content_sha="C2", prompt_sha="P") is None


def test_drop_match_clears_only_matcher_payload():
    c = Cache()
    c.set_extraction(
        file_id="F1", modified_time="t", content_sha="C",
        extraction_payload={"x": 1},
    )
    c.set_match(
        file_id="F1", modified_time="t", content_sha="C", prompt_sha="P",
        topology_sha="T", results=_sample_match_results(),
    )
    c.drop_match("F1")
    assert c.get_match("F1", content_sha="C", prompt_sha="P") is None
    assert c.get_extraction("F1", "C") == {"x": 1}


def test_match_round_trip_via_save_load(tmp_path: Path):
    p = tmp_path / "cache.json"
    c = Cache()
    c.set_match(
        file_id="F1", modified_time="t", content_sha="C", prompt_sha="P",
        topology_sha="T", results=_sample_match_results(),
    )
    c.save(p)
    c2 = Cache.load(p)
    hit = c2.get_match("F1", content_sha="C", prompt_sha="P")
    assert hit is not None
    assert hit[0]["matched_jira_key"] == "CENTPM-1"


# ----------------------------------------------------------------------
# topology + prompt sha helpers
# ----------------------------------------------------------------------


def test_topology_sha_stable_for_same_input():
    from jira_task_agent.pipeline.matcher import compute_project_topology_sha
    tree = {
        "epics": [
            {"key": "K1", "summary": "S1", "description": "d1",
             "children": [{"key": "C1", "summary": "cs", "status": "Backlog", "description": ""}]},
        ],
    }
    assert compute_project_topology_sha(tree) == compute_project_topology_sha(tree)


def test_topology_sha_changes_on_child_status():
    from jira_task_agent.pipeline.matcher import compute_project_topology_sha
    base_child = {"key": "C1", "summary": "cs", "status": "Backlog", "description": ""}
    tree = {"epics": [{"key": "K1", "summary": "S1", "description": "", "children": [base_child]}]}
    a = compute_project_topology_sha(tree)
    base_child["status"] = "Done"
    b = compute_project_topology_sha(tree)
    assert a != b


def test_topology_sha_changes_on_epic_added():
    from jira_task_agent.pipeline.matcher import compute_project_topology_sha
    tree = {"epics": [{"key": "K1", "summary": "S1", "description": "", "children": []}]}
    a = compute_project_topology_sha(tree)
    tree["epics"].append({"key": "K2", "summary": "S2", "description": "", "children": []})
    b = compute_project_topology_sha(tree)
    assert a != b


def test_diff_payload_round_trip():
    c = Cache()
    c.set_classification(
        file_id="F1", modified_time="t", content_sha="C", role="single_epic",
        confidence=1.0, reason="x",
    )
    c.set_diff_payload(
        file_id="F1",
        chunks={"A. Sec|0": "sha-A", "B. Sec|0": "sha-B"},
        task_anchors={
            "SEC-1 anchor": {"chunk_id": "A. Sec|0", "body_sha": "sha-A"},
            "SEC-2 anchor": {"chunk_id": "A. Sec|0", "body_sha": "sha-A"},
        },
    )
    assert c.get_chunks("F1") == {"A. Sec|0": "sha-A", "B. Sec|0": "sha-B"}
    anchors = c.get_task_anchors("F1")
    assert anchors["SEC-1 anchor"]["chunk_id"] == "A. Sec|0"
    assert anchors["SEC-2 anchor"]["body_sha"] == "sha-A"


def test_diff_payload_save_load(tmp_path: Path):
    p = tmp_path / "cache.json"
    c = Cache()
    c.set_classification(
        file_id="F1", modified_time="t", content_sha="C", role="single_epic",
        confidence=1.0, reason="x",
    )
    c.set_diff_payload(
        file_id="F1",
        chunks={"A|0": "sha-A"},
        task_anchors={"a1": {"chunk_id": "A|0", "body_sha": "sha-A"}},
    )
    c.save(p)
    c2 = Cache.load(p)
    assert c2.get_chunks("F1") == {"A|0": "sha-A"}
    assert c2.get_task_anchors("F1")["a1"]["chunk_id"] == "A|0"


def test_diff_payload_missing_file_returns_empty():
    c = Cache()
    assert c.get_chunks("ghost") == {}
    assert c.get_task_anchors("ghost") == {}


def test_diff_payload_set_requires_existing_entry():
    """set_diff_payload silently does nothing when file_id has no entry —
    classification must be set first. This prevents partial entries."""
    c = Cache()
    c.set_diff_payload(file_id="ghost", chunks={"a": "1"}, task_anchors={})
    assert "ghost" not in c.files


def test_topology_sha_stable_under_reorder():
    from jira_task_agent.pipeline.matcher import compute_project_topology_sha
    a = compute_project_topology_sha({"epics": [
        {"key": "K1", "summary": "S1", "description": "", "children": []},
        {"key": "K2", "summary": "S2", "description": "", "children": []},
    ]})
    b = compute_project_topology_sha({"epics": [
        {"key": "K2", "summary": "S2", "description": "", "children": []},
        {"key": "K1", "summary": "S1", "description": "", "children": []},
    ]})
    assert a == b  # sorted internally — order in payload doesn't matter
