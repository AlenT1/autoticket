"""file-to-jira-tickets — turn markdown bug lists into Jira tickets, enriched by AI agents.

Public API for embedding inside another project. The CLI is in ``cli.py``; the
functions below are re-exported for library callers.
"""

from .config import AppConfig, RepoAlias, load_config
from .enrich.orchestrator import run_enrich
from .jira import upload_state
from .models import (
    BugRecord,
    BugStage,
    EnrichedBug,
    IntermediateFile,
    ParsedBug,
    UploadResult,
)
from .parse import parse_markdown, read_and_decode
from .state import StateStore, load_state, save_state

__version__ = "0.1.0"

__all__ = [
    "AppConfig",
    "BugRecord",
    "BugStage",
    "EnrichedBug",
    "IntermediateFile",
    "ParsedBug",
    "RepoAlias",
    "StateStore",
    "UploadResult",
    "__version__",
    "load_config",
    "load_state",
    "parse_markdown",
    "read_and_decode",
    "run_enrich",
    "save_state",
    "upload_state",
]
