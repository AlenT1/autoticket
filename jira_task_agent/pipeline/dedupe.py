"""Detect content-duplicate Drive files (e.g. a markdown upload + its
Google-Docs auto-converted twin) and pick a canonical copy.

Google Docs exports markdown with extra escaping (`Rev 5\\)` vs `Rev 5)`,
`\\+`, `\\-`) and aligned table dividers (`| :---- |` vs `|----|`). If we
strip those before hashing, the two siblings hash identically and we can
demote the non-canonical ones to `skip` so they don't get classified or
extracted twice.

Canonical-pick rule: prefer `text/markdown` upload over the Google-Doc
export; tie-break on most-recently-modified.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..drive.client import DriveFile

_MIN_CHARS_FOR_DEDUPE = 200  # don't try to dedupe tiny files

# Strip backslash-escapes before non-alphanumerics that Google Docs sprinkles in.
_ESC_RE = re.compile(r"\\([^a-zA-Z0-9])")
# Collapse a whole table-divider cell — between two `|`, allow optional
# spaces/colons around 3+ dashes — into a single canonical form.
# This handles `|---|` vs `| :---- |` vs `| ---: |` etc. uniformly.
_DIVIDER_CELL_RE = re.compile(r"\|\s*:?-{3,}:?\s*\|")
# Standalone divider sequences like `:----:` / `----:` not embedded in a `|`.
_DIVIDER_RE = re.compile(r":?-{3,}:?")
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    text = _ESC_RE.sub(r"\1", text)
    # Whole-cell first: `|...---...|` -> `|---|`
    text = _DIVIDER_CELL_RE.sub("|---|", text)
    # Then any free-floating `:----:` style sequences.
    text = _DIVIDER_RE.sub("---", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def _content_hash(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    norm = _normalize(text)
    if len(norm) < _MIN_CHARS_FOR_DEDUPE:
        return None
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def find_duplicate_copies(
    files: list[DriveFile],
    local_paths: dict[str, Path],
) -> dict[str, str]:
    """Return `{duplicate_file_id: canonical_file_id}`.

    Files not in any duplicate group are not in the returned dict.
    """
    by_hash: dict[str, list[DriveFile]] = {}
    for f in files:
        path = local_paths.get(f.id)
        if not path:
            continue
        h = _content_hash(path)
        if h is None:
            continue
        by_hash.setdefault(h, []).append(f)

    out: dict[str, str] = {}
    for group in by_hash.values():
        if len(group) <= 1:
            continue
        group.sort(
            key=lambda f: (
                f.mime_type != "text/markdown",  # markdown first
                -f.modified_time.timestamp(),    # then most recent
            )
        )
        canonical = group[0]
        for f in group[1:]:
            out[f.id] = canonical.id
    return out
