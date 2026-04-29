"""Unit tests for the source_anchor → chunk locator."""
from __future__ import annotations

from jira_task_agent.pipeline.anchor_locator import (
    AnchorLocation,
    locate_task,
    locate_tasks,
)
from jira_task_agent.pipeline.chunker import Chunk, chunk_markdown
from jira_task_agent.pipeline.extractor import ExtractedTask


def _t(summary: str, anchor: str) -> ExtractedTask:
    return ExtractedTask(
        summary=summary,
        description="body\n\n### Definition of Done\n- [ ] done\n",
        source_anchor=anchor,
    )


def test_exact_anchor_match():
    text = (
        "# Top\n\n"
        "## A. Section\nSEC-1 Generate JWT secret. Body of A.\n\n"
        "## B. Other\nB body.\n"
    )
    chunks = chunk_markdown(text)
    loc = locate_task(_t("Generate JWT secret", "SEC-1 Generate JWT secret"), chunks)
    assert loc.confidence == "exact"
    assert loc.chunk_id.startswith("A. Section")


def test_token_overlap_match_when_exact_fails():
    """LLM might shorten the anchor relative to the doc. Token overlap
    catches it."""
    text = (
        "# Top\n\n"
        "## A. Section\nV11-1 Collect user feedback on the dashboard surface.\n"
    )
    chunks = chunk_markdown(text)
    # Anchor missing the trailing "on the dashboard surface" — exact fails.
    loc = locate_task(
        _t("Collect feedback", "V11-1 Collect user feedback dashboard"),
        chunks,
    )
    assert loc.chunk_id is not None
    assert loc.confidence in ("exact", "substring")


def test_summary_fallback():
    text = "# Top\n\n## A. Section\nNo formal anchor here, just a description of the work to add some labels.\n"
    chunks = chunk_markdown(text)
    loc = locate_task(
        _t("add some labels", "no-such-anchor-XYZ"),
        chunks,
    )
    assert loc.chunk_id is not None


def test_unlocatable_returns_none():
    text = "# Top\n\n## A. Section\nNothing matching.\n"
    chunks = chunk_markdown(text)
    loc = locate_task(
        _t("some title", "completely-unrelated-anchor-string-XYZ"),
        chunks,
    )
    assert loc.chunk_id is None
    assert loc.confidence == "none"


def test_locate_tasks_parallel_to_input():
    text = (
        "# Top\n\n"
        "## A. Section\nSEC-1 task one body.\nSEC-2 task two body.\n\n"
        "## B. Section\nB-1 task three body.\n"
    )
    chunks = chunk_markdown(text)
    tasks = [
        _t("task one", "SEC-1 task one"),
        _t("task three", "B-1 task three"),
        _t("ghost", "no-such"),
    ]
    locs = locate_tasks(tasks, chunks)
    assert len(locs) == 3
    assert locs[0].chunk_id.startswith("A. Section")
    assert locs[1].chunk_id.startswith("B. Section")
    assert locs[2].chunk_id is None


def test_empty_chunks_returns_none():
    loc = locate_task(_t("x", "y"), [])
    assert loc == AnchorLocation(chunk_id=None, body_sha=None, confidence="none")
