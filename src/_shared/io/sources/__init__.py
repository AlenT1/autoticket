"""Pluggable input sources.

Each :class:`Source` impl yields :class:`RawDocument`s — content + metadata,
tracker-agnostic. Bodies do format-specific parsing themselves
(``file_to_jira`` parses bug-list markdown; ``jira_task_agent`` classifies
and extracts epic/task structure).
"""
from .base import RawDocument, Source
from .single_file import SingleFileSource

__all__ = ["RawDocument", "Source", "SingleFileSource"]
