"""structlog setup with run_id correlation."""

from __future__ import annotations

import logging
import sys
import uuid
from typing import Any

import structlog

_RUN_ID = uuid.uuid4().hex[:12]
_CONFIGURED = False


def _add_run_id(_: Any, __: Any, event_dict: dict[str, Any]) -> dict[str, Any]:
    event_dict["run_id"] = _RUN_ID
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Configure structlog. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level = getattr(logging, level.upper(), logging.INFO)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _add_run_id,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)


def get_run_id() -> str:
    return _RUN_ID
