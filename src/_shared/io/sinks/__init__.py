"""Pluggable output sinks (tracker-agnostic).

A :class:`TicketSink` accepts generic :class:`Ticket`s and translates them
to the target tracker's REST shape. Today: Jira only. Future stage-3 impls:
Monday, Linear, GitHub Issues — each one a new file implementing the
protocol.

Strategies (identification, assignee resolution, etc.) plug in per-tool so
f2j and jira_task_agent can share the sink while keeping their distinct
idempotency / assignee / epic-routing semantics.
"""
from .base import (
    AssigneeResolver,
    EpicRouter,
    IdentificationStrategy,
    Ticket,
    TicketSink,
)

__all__ = [
    "Ticket",
    "TicketSink",
    "IdentificationStrategy",
    "AssigneeResolver",
    "EpicRouter",
]
