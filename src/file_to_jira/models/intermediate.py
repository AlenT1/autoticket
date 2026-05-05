"""The intermediate state file that holds all bugs across stages."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from .bug import BugRecord


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class IntermediateFile(BaseModel):
    schema_version: int = 1
    run_id: str
    source_file: str
    source_file_sha256: str
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)
    bugs: list[BugRecord] = Field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = _now_iso()
