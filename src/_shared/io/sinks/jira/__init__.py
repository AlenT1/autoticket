"""Jira-flavored :class:`TicketSink` impl + plug-in strategies."""
from .client import JiraClient, JiraError, WhoamiResult
from .sink import CapturingJiraSink, JiraSink

__all__ = [
    "CapturingJiraSink",
    "JiraClient",
    "JiraError",
    "JiraSink",
    "WhoamiResult",
]
