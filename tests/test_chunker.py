"""Unit tests for the markdown chunker."""
from __future__ import annotations

from jira_task_agent.pipeline.chunker import Chunk, chunk_markdown


def test_empty_input_returns_no_chunks():
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n  \n") == []


def test_no_headings_yields_one_chunk():
    text = "Just a paragraph with no headings.\n\nAnother paragraph."
    chunks = chunk_markdown(text)
    assert len(chunks) == 1
    assert chunks[0].heading == "(no heading)"
    # LangChain may normalize whitespace, so we don't compare bytes exactly;
    # we assert the content survives the round-trip semantically.
    assert "Just a paragraph" in chunks[0].body
    assert "Another paragraph" in chunks[0].body
    assert chunks[0].ordinal == 0


def test_single_h1_yields_one_chunk():
    text = "# Title\n\nbody body body."
    chunks = chunk_markdown(text)
    assert len(chunks) == 1
    assert chunks[0].h1 == "Title"
    assert chunks[0].h2 == ""
    assert chunks[0].heading == "Title"


def test_h2_creates_one_chunk_per_section():
    text = (
        "# Title\n\n"
        "intro text\n\n"
        "## A. First\nfirst body\n\n"
        "## B. Second\nsecond body\n"
    )
    chunks = chunk_markdown(text)
    headings = [c.heading for c in chunks]
    assert "A. First" in headings
    assert "B. Second" in headings


def test_chunk_id_handles_duplicate_headings_safely():
    """LangChain's splitter merges sections with identical H2 names into
    one chunk (we observed this behavior). Our ordinal logic acts as a
    safety net for splitters that don't merge — verify that at minimum
    we don't crash and we produce stable chunk_ids."""
    text = (
        "# Top\n\n"
        "## Same\nfirst\n\n"
        "## Same\nsecond\n"
    )
    chunks = chunk_markdown(text)
    assert len(chunks) >= 1
    # All chunk_ids must be unique within a file.
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))


def test_body_sha_is_deterministic():
    text = "# Title\n\n## A. Sec\nbody.\n"
    a = chunk_markdown(text)
    b = chunk_markdown(text)
    assert [c.body_sha for c in a] == [c.body_sha for c in b]


def test_body_sha_changes_on_content_edit():
    text_a = "# Title\n\n## A. Sec\noriginal body.\n"
    text_b = "# Title\n\n## A. Sec\nedited body.\n"
    sha_a = next(c.body_sha for c in chunk_markdown(text_a) if c.heading == "A. Sec")
    sha_b = next(c.body_sha for c in chunk_markdown(text_b) if c.heading == "A. Sec")
    assert sha_a != sha_b


def test_unaffected_chunk_sha_survives_edit_in_other_chunk():
    """Editing one section must not change the sha of an unrelated section.
    This is the core property the cache relies on."""
    text_a = (
        "# Title\n\n"
        "## A. First\nfirst body\n\n"
        "## B. Second\nsecond body\n"
    )
    text_b = (
        "# Title\n\n"
        "## A. First\nFIRST EDITED body\n\n"
        "## B. Second\nsecond body\n"
    )
    a_sha_a = next(c.body_sha for c in chunk_markdown(text_a) if c.heading == "A. First")
    a_sha_b = next(c.body_sha for c in chunk_markdown(text_b) if c.heading == "A. First")
    b_sha_a = next(c.body_sha for c in chunk_markdown(text_a) if c.heading == "B. Second")
    b_sha_b = next(c.body_sha for c in chunk_markdown(text_b) if c.heading == "B. Second")
    assert a_sha_a != a_sha_b, "edited chunk's sha must change"
    assert b_sha_a == b_sha_b, "untouched chunk's sha must not change"


def test_reorder_preserves_chunk_ids():
    """Section reorder must not break cache identity (id is heading-based)."""
    text_a = (
        "# Top\n\n"
        "## A. First\nfirst\n\n"
        "## B. Second\nsecond\n"
    )
    text_b = (
        "# Top\n\n"
        "## B. Second\nsecond\n\n"
        "## A. First\nfirst\n"
    )
    ids_a = sorted(c.chunk_id for c in chunk_markdown(text_a))
    ids_b = sorted(c.chunk_id for c in chunk_markdown(text_b))
    assert ids_a == ids_b
