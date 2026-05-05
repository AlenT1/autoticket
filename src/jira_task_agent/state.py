"""Tiny cursor file. The only thing the agent persists across runs.

Schema:
  {
    "last_run_at": "<ISO 8601 with tz>",
    "last_run_status": "ok" | "error" | "never"
  }
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class State:
    last_run_at: datetime | None
    last_run_status: str  # "ok" | "error" | "never"


DEFAULT_STATE_PATH = Path("data/state.json")


def _path(state_path: Path | None) -> Path:
    return state_path or DEFAULT_STATE_PATH


def load(state_path: Path | None = None) -> State:
    p = _path(state_path)
    if not p.exists():
        return State(last_run_at=None, last_run_status="never")
    data = json.loads(p.read_text(encoding="utf-8"))
    raw = data.get("last_run_at")
    return State(
        last_run_at=datetime.fromisoformat(raw) if raw else None,
        last_run_status=str(data.get("last_run_status", "never")),
    )


def save(state: State, state_path: Path | None = None) -> None:
    p = _path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_run_at": state.last_run_at.isoformat() if state.last_run_at else None,
        "last_run_status": state.last_run_status,
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
