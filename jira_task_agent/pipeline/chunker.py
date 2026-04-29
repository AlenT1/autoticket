"""Deterministic markdown chunker.

Wraps `langchain-text-splitters.MarkdownHeaderTextSplitter` with a
content-hash identity and a stable `chunk_id`. Output is the unit of
caching for extract + matcher decisions.

Behavior:
  - Splits on H1 + H2 boundaries by default. Tasks-with-headings inside
    a section (e.g. V0_Lior's `### Step N:`) stay together inside one
    chunk so we don't fragment a single epic into per-task chunks.
  - Each chunk carries its heading path (h1, h2) so callers can rebuild
    semantic context without re-parsing.
  - Body sha = sha256 of the chunk body bytes (heading text included).
    Stable identity for cache lookup; changes when anything in the chunk
    changes.
  - Chunk id = `(h2 or h1, ordinal)`. Ordinal disambiguates duplicate
    headings within the same file (rare but possible). Survives reorder
    because identity is heading-text + ordinal, not file position.

NemoClaw (one heading, no sub-sections) → 1 chunk = whole file.
V11_Dashboard_Tasks (1 H1 + a few H2) → small number of chunks.
May1 (1 H1 + 14 H2) → 14 chunks; the 9 epic ones get extracted, the 5
context ones extract to empty and get cached as such.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from langchain_text_splitters import MarkdownHeaderTextSplitter


_HEADERS_TO_SPLIT_ON = [
    ("#", "h1"),
    ("##", "h2"),
]


@dataclass(frozen=True)
class Chunk:
    """One slice of a markdown file.

    chunk_id   — stable across edits as long as the heading text and its
                 occurrence ordinal don't change. Used as the cache key.
    heading    — the most-specific heading at this chunk (h2 if any,
                 else h1, else "(no heading)"). Human-readable.
    h1 / h2    — the heading hierarchy walked down to this chunk.
    body       — the chunk's text content. Heading line is included so
                 that body sha covers both the title and the body.
    body_sha   — sha256 hex of `body`. Cache invalidation key.
    ordinal    — 0-indexed position among chunks sharing the same
                 heading text in this file (almost always 0).
    """
    chunk_id: str
    heading: str
    h1: str
    h2: str
    body: str
    body_sha: str
    ordinal: int


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_markdown(text: str) -> list[Chunk]:
    """Split markdown into chunks. Pure function, no LLM, no I/O.

    Returns at least one chunk for any non-empty input. Files with no
    headings degenerate to a single chunk holding the entire file.
    """
    if not text or not text.strip():
        return []

    splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=_HEADERS_TO_SPLIT_ON,
        # `strip_headers=False` keeps the heading line inside the body
        # so the extractor sees natural context, and the body sha
        # changes if someone renames the heading.
        strip_headers=False,
    )

    docs = splitter.split_text(text)
    if not docs:
        # No headings — treat the whole file as one chunk.
        body = text
        return [
            Chunk(
                chunk_id="(no heading)|0",
                heading="(no heading)",
                h1="",
                h2="",
                body=body,
                body_sha=_sha(body),
                ordinal=0,
            )
        ]

    # Track ordinals per heading text so duplicates get distinct ids.
    ordinals: dict[str, int] = {}
    out: list[Chunk] = []
    for d in docs:
        meta = d.metadata or {}
        h1 = (meta.get("h1") or "").strip()
        h2 = (meta.get("h2") or "").strip()
        heading = h2 or h1 or "(no heading)"
        ordinal = ordinals.get(heading, 0)
        ordinals[heading] = ordinal + 1
        body = d.page_content or ""
        out.append(
            Chunk(
                chunk_id=f"{heading}|{ordinal}",
                heading=heading,
                h1=h1,
                h2=h2,
                body=body,
                body_sha=_sha(body),
                ordinal=ordinal,
            )
        )
    return out
