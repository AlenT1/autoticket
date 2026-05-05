"""Compatibility shim — JiraClient moved to ``_shared.io.sinks.jira.client``.

Re-exports preserved so existing imports continue working. New code should
import directly from ``_shared.io.sinks.jira.client``.
"""
from _shared.io.sinks.jira.client import (  # noqa: F401
    JiraClient,
    _build_auth_header,
    _load_token,
    _normalize_host,
    _normalize_issue,
    get_issue,
    list_epic_children,
    list_epics,
)
