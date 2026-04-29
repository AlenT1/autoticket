"""Per-file cache (`cache.json`) so warm runs skip work for files that
haven't changed.

Two tiers populated by the runner:

  Tier 1 — classification cache.
    Skip the LLM classify call when `(file_id, modified_time)` matches.
    A file's modification time is the strongest "did anything change?"
    signal Drive gives us; we only re-classify when the doc moved.

  Tier 2 — extraction cache.
    Skip the big LLM extract call when `(file_id, content_sha)` matches.
    Content hash defends against the rare case of mtime moving without
    content actually changing (touch / metadata edits).

Lifecycle:
  * Load cache at start of a run.
  * For each file: probe cache; reuse cached values on hit; otherwise
    do the work and update the cache with the result.
  * Save cache once at the end of the run (atomic via tempfile + rename).
  * Cache schema version mismatch → drop the cache, treat as cold.
  * Files no longer present in Drive remain in the cache as stale
    entries (harmless; nothing reads them). Periodic clean is optional.

Cache file: `cache.json` next to `state.json`. Single small JSON; one
read/write per run. No new dependencies.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .pipeline.extractor import (
    ExtractedEpic,
    ExtractedEpicWithTasks,
    ExtractedTask,
    ExtractionResult,
    MultiExtractionResult,
)


CACHE_VERSION = 1
DEFAULT_CACHE_PATH = Path("data/cache.json")


# ----------------------------------------------------------------------
# extraction <-> JSON round-trip
# ----------------------------------------------------------------------


def serialize_extraction(
    ext: ExtractionResult | MultiExtractionResult,
) -> dict[str, Any]:
    """Convert an ExtractionResult / MultiExtractionResult to plain dict.

    Symmetric with `deserialize_extraction`. Used so cached results can
    round-trip through `cache.json`.
    """
    if isinstance(ext, MultiExtractionResult):
        return {
            "type": "multi_epic",
            "file_id": ext.file_id,
            "file_name": ext.file_name,
            "epics": [
                {
                    "summary": e.summary,
                    "description": e.description,
                    "assignee_name": e.assignee_name,
                    "tasks": [
                        {
                            "summary": t.summary,
                            "description": t.description,
                            "source_anchor": t.source_anchor,
                            "assignee_name": t.assignee_name,
                        }
                        for t in e.tasks
                    ],
                }
                for e in ext.epics
            ],
        }
    # ExtractionResult (single_epic)
    return {
        "type": "single_epic",
        "file_id": ext.file_id,
        "file_name": ext.file_name,
        "epic": {
            "summary": ext.epic.summary,
            "description": ext.epic.description,
            "assignee_name": ext.epic.assignee_name,
        },
        "tasks": [
            {
                "summary": t.summary,
                "description": t.description,
                "source_anchor": t.source_anchor,
                "assignee_name": t.assignee_name,
            }
            for t in ext.tasks
        ],
    }


def deserialize_extraction(
    data: dict[str, Any],
) -> ExtractionResult | MultiExtractionResult:
    """Inverse of `serialize_extraction`."""
    if data.get("type") == "multi_epic":
        return MultiExtractionResult(
            file_id=data["file_id"],
            file_name=data["file_name"],
            epics=[
                ExtractedEpicWithTasks(
                    summary=e["summary"],
                    description=e["description"],
                    assignee_name=e.get("assignee_name"),
                    tasks=[
                        ExtractedTask(
                            summary=t["summary"],
                            description=t["description"],
                            source_anchor=t.get("source_anchor", ""),
                            assignee_name=t.get("assignee_name"),
                        )
                        for t in e.get("tasks") or []
                    ],
                )
                for e in data.get("epics") or []
            ],
        )
    # single_epic
    return ExtractionResult(
        file_id=data["file_id"],
        file_name=data["file_name"],
        epic=ExtractedEpic(
            summary=data["epic"]["summary"],
            description=data["epic"]["description"],
            assignee_name=data["epic"].get("assignee_name"),
        ),
        tasks=[
            ExtractedTask(
                summary=t["summary"],
                description=t["description"],
                source_anchor=t.get("source_anchor", ""),
                assignee_name=t.get("assignee_name"),
            )
            for t in data.get("tasks") or []
        ],
    )


# ----------------------------------------------------------------------
# Cache schema
# ----------------------------------------------------------------------


@dataclass
class FileCacheEntry:
    file_id: str
    modified_time: str        # ISO string from Drive
    content_sha: str          # sha256 of the local file bytes
    role: str | None          # last-known classifier role
    classification_confidence: float | None = None
    classification_reason: str | None = None
    extraction_payload: dict | None = None  # serialize_extraction output
    cached_at: str = ""

    def to_json(self) -> dict:
        return {
            "modified_time": self.modified_time,
            "content_sha": self.content_sha,
            "role": self.role,
            "classification_confidence": self.classification_confidence,
            "classification_reason": self.classification_reason,
            "extraction_payload": self.extraction_payload,
            "cached_at": self.cached_at,
        }

    @classmethod
    def from_json(cls, file_id: str, raw: dict) -> "FileCacheEntry":
        return cls(
            file_id=file_id,
            modified_time=raw.get("modified_time", ""),
            content_sha=raw.get("content_sha", ""),
            role=raw.get("role"),
            classification_confidence=raw.get("classification_confidence"),
            classification_reason=raw.get("classification_reason"),
            extraction_payload=raw.get("extraction_payload"),
            cached_at=raw.get("cached_at", ""),
        )


@dataclass
class Cache:
    version: int = CACHE_VERSION
    files: dict[str, FileCacheEntry] = field(default_factory=dict)

    # ---- I/O ------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "Cache":
        p = path or DEFAULT_CACHE_PATH
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt cache → cold restart.
            return cls()
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return cls()
        files_raw = data.get("files") or {}
        files = {
            fid: FileCacheEntry.from_json(fid, raw)
            for fid, raw in files_raw.items()
            if isinstance(raw, dict)
        }
        return cls(version=CACHE_VERSION, files=files)

    def save(self, path: Path | None = None) -> None:
        p = path or DEFAULT_CACHE_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.version,
            "files": {fid: e.to_json() for fid, e in self.files.items()},
        }
        # Atomic write via tempfile in same dir, then rename.
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=str(p.parent or "."), delete=False, encoding="utf-8"
        )
        try:
            json.dump(payload, tmp, indent=2, ensure_ascii=False)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.replace(tmp.name, p)

    # ---- Tier 1: classification cache ----------------------------------

    def get_classification(
        self, file_id: str, modified_time: str
    ) -> tuple[str, float | None, str | None] | None:
        """Returns (role, confidence, reason) if cached for the same
        modified_time; otherwise None.
        """
        entry = self.files.get(file_id)
        if entry is None:
            return None
        if entry.modified_time != modified_time:
            return None
        if entry.role is None:
            return None
        return (entry.role, entry.classification_confidence, entry.classification_reason)

    def set_classification(
        self,
        *,
        file_id: str,
        modified_time: str,
        content_sha: str,
        role: str,
        confidence: float | None,
        reason: str | None,
    ) -> None:
        entry = self.files.get(file_id) or FileCacheEntry(
            file_id=file_id,
            modified_time=modified_time,
            content_sha=content_sha,
            role=None,
        )
        # If mtime moved, drop any stale extraction payload — it could be
        # for the previous content.
        if entry.modified_time != modified_time or entry.content_sha != content_sha:
            entry.extraction_payload = None
        entry.modified_time = modified_time
        entry.content_sha = content_sha
        entry.role = role
        entry.classification_confidence = confidence
        entry.classification_reason = reason
        entry.cached_at = datetime.now().isoformat(timespec="seconds")
        self.files[file_id] = entry

    # ---- Tier 2: extraction cache --------------------------------------

    def get_extraction(self, file_id: str, content_sha: str) -> dict | None:
        """Returns the cached extraction payload (dict) when content_sha
        matches; None otherwise."""
        entry = self.files.get(file_id)
        if entry is None or entry.extraction_payload is None:
            return None
        if entry.content_sha != content_sha:
            return None
        return entry.extraction_payload

    def set_extraction(
        self,
        *,
        file_id: str,
        modified_time: str,
        content_sha: str,
        extraction_payload: dict,
    ) -> None:
        entry = self.files.get(file_id) or FileCacheEntry(
            file_id=file_id,
            modified_time=modified_time,
            content_sha=content_sha,
            role=None,
        )
        entry.modified_time = modified_time
        entry.content_sha = content_sha
        entry.extraction_payload = extraction_payload
        entry.cached_at = datetime.now().isoformat(timespec="seconds")
        self.files[file_id] = entry


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def file_content_sha(local_path: Path) -> str:
    """sha256 of file bytes, hex. Used for tier-2 invalidation."""
    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
