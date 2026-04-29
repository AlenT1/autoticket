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
