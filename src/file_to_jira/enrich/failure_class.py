"""Failure classification for the enrichment loop.

Borrowed from a sibling project's hard-won taxonomy. Each class drives a
different retry strategy at the orchestrator level:

- ``rate_limit``  → don't retry; surface to operator (likely needs a token bump
                    or a quieter run).
- ``overload``    → exponential backoff, up to N retries.
- ``context_limit`` → don't retry; the bug body + tool outputs exceeded the
                    model's window. Caller should split the input.
- ``unknown``     → retry once. Catch-all for transient / unfamiliar errors.
"""

from __future__ import annotations

import re
from enum import Enum


class FailureClass(str, Enum):
    RATE_LIMIT = "rate_limit"
    OVERLOAD = "overload"
    CONTEXT_LIMIT = "context_limit"
    UNKNOWN = "unknown"


# Patterns are matched case-insensitively against the exception's stringified form
# (or any extra log text the caller passes). Order matters for ambiguous matches:
# rate_limit is checked first because some providers wrap "rate limit" inside a
# "service unavailable" envelope.
_PATTERNS: list[tuple[FailureClass, re.Pattern[str]]] = [
    (
        FailureClass.RATE_LIMIT,
        re.compile(r"rate.?limit|usage.?limit|quota|plan.?limit|429\b", re.IGNORECASE),
    ),
    (
        FailureClass.OVERLOAD,
        re.compile(
            r"\b(?:overloaded|529|503)\b|service unavailable|temporarily unavailable",
            re.IGNORECASE,
        ),
    ),
    (
        FailureClass.CONTEXT_LIMIT,
        re.compile(
            r"prompt.{0,10}too.{0,5}long|context.{0,10}(?:window|limit)|"
            r"too many tokens|maximum context",
            re.IGNORECASE,
        ),
    ),
]


def classify_error(message: str, *extra: str) -> FailureClass:
    """Classify an error message into a FailureClass.

    Pass any additional context (e.g., recent log lines, response body) as
    positional args — they're concatenated and scanned with the same patterns.
    """
    haystack = "\n".join([message, *extra])
    for cls, pattern in _PATTERNS:
        if pattern.search(haystack):
            return cls
    return FailureClass.UNKNOWN
