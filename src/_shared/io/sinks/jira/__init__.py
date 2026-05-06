"""Jira-flavored :class:`TicketSink` impl + plug-in strategies."""
from .client import JiraClient, JiraError, WhoamiResult
from .field_discovery import (
    FieldInfo,
    FieldMap,
    build_field_map,
    discover_create_meta,
    discover_fields_from_issue,
)
from .sink import CapturingJiraSink, JiraSink

__all__ = [
    "CapturingJiraSink",
    "FieldInfo",
    "FieldMap",
    "JiraClient",
    "JiraError",
    "JiraSink",
    "WhoamiResult",
    "build_field_map",
    "discover_create_meta",
    "discover_fields_from_issue",
]
