"""Locate which chunk owns each extracted task.

After extraction, each `ExtractedTask` has a `source_anchor` (a short
identifier the LLM produced from the task's content, e.g.
`"V11-1 Collect user feedback"`, `"Step 5: Move components"`,
`"SEC-1 Generate JWT secret"`).

To do per-chunk caching, we need a mapping `task -> owning_chunk_id`.
Pure-function search: for each task, find the chunk whose body contains
the source_anchor text. That chunk owns the task.

Identity is stable across edits as long as the source_anchor doesn't
move sections — which is normally the case (anchors are content-based
strings the LLM derives from the task itself, not its position).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .chunker import Chunk
from .extractor import ExtractedTask


@dataclass(frozen=True)
class AnchorLocation:
    """Where a task's source_anchor was found.

    chunk_id    — chunk_id of the owning chunk, or None if not found.
    body_sha    — sha of that chunk's body (for cache invalidation).
    confidence  — "exact" if the anchor literal is in the chunk body;
                  "substring" if a normalized partial match;
                  "none" if not located.
    """
    chunk_id: str | None
    body_sha: str | None
    confidence: str


def _normalize(s: str) -> str:
    """Lowercase + collapse whitespace. Used for fuzzy fallback only."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def locate_task(task: ExtractedTask, chunks: list[Chunk]) -> AnchorLocation:
    """Find which chunk contains this task's source_anchor.

    Strategy:
      1. Exact substring match of `source_anchor` in any chunk body.
      2. Fallback: split anchor into significant tokens (length >= 4)
         and find the chunk whose normalized body contains the most
         tokens. This handles anchors like `"V11-1 Collect user
         feedback"` where the doc has `"V11-1 Collect user feedback
         on dashboard"` — exact substring fails but token overlap
         succeeds.
      3. Last resort: substring match of the task's `summary`.

    Returns AnchorLocation; `chunk_id=None` means not located. The
    runner treats unlocated tasks as "must re-match" — safest fallback.
    """
    anchor = (task.source_anchor or "").strip()
    summary = (task.summary or "").strip()

    if not chunks:
        return AnchorLocation(chunk_id=None, body_sha=None, confidence="none")

    # Strategy 1 — exact substring of the anchor.
    if anchor:
        for c in chunks:
            if anchor and anchor in c.body:
                return AnchorLocation(
                    chunk_id=c.chunk_id, body_sha=c.body_sha, confidence="exact"
                )

    # Strategy 2 — token overlap on the anchor (case-insensitive,
    # whitespace-collapsed).
    if anchor:
        tokens = [
            t for t in re.split(r"[\s\W]+", anchor.lower()) if len(t) >= 4
        ]
        if tokens:
            best_chunk: Chunk | None = None
            best_hits = 0
            for c in chunks:
                body_norm = _normalize(c.body)
                hits = sum(1 for tok in tokens if tok in body_norm)
                if hits > best_hits:
                    best_hits = hits
                    best_chunk = c
            # Require at least half the tokens to match to claim a hit.
            if best_chunk is not None and best_hits >= max(1, len(tokens) // 2):
                return AnchorLocation(
                    chunk_id=best_chunk.chunk_id,
                    body_sha=best_chunk.body_sha,
                    confidence="substring",
                )

    # Strategy 3 — substring match on the task summary, last resort.
    if summary:
        for c in chunks:
            if summary in c.body:
                return AnchorLocation(
                    chunk_id=c.chunk_id, body_sha=c.body_sha, confidence="substring"
                )

    return AnchorLocation(chunk_id=None, body_sha=None, confidence="none")


def locate_tasks(
    tasks: list[ExtractedTask], chunks: list[Chunk]
) -> list[AnchorLocation]:
    """Locate every task. Output parallel to `tasks`."""
    return [locate_task(t, chunks) for t in tasks]
