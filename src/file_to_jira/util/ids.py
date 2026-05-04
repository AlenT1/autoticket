"""Stable ID generation."""

from __future__ import annotations

import hashlib


def compute_bug_id(source_file_sha256: str, raw_title: str, source_line_start: int) -> str:
    """Stable per-bug hash. Survives reordering of bugs and re-runs of the parser."""
    payload = f"{source_file_sha256}|{raw_title}|{source_line_start}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def file_sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def file_sha256_path(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
