"""Pluggable per-tool strategies for the Jira sink.

Bodies pick the strategies that fit their identity / assignee / epic
conventions; the sink stays generic.
"""
from .assignee import (
    PassthroughAssigneeResolver,
    PickerWithCacheStrategy,
    StaticMapStrategy,
)
from .epic_router import DeterministicChainStrategy, NoOpStrategy
from .identification import CacheTrustStrategy, LabelSearchStrategy

__all__ = [
    # identification
    "LabelSearchStrategy",
    "CacheTrustStrategy",
    # assignee
    "PassthroughAssigneeResolver",
    "StaticMapStrategy",
    "PickerWithCacheStrategy",
    # epic router
    "NoOpStrategy",
    "DeterministicChainStrategy",
]
