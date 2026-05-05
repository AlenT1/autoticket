"""Tests for the failure-class taxonomy."""

from __future__ import annotations

import pytest

from file_to_jira.enrich.failure_class import FailureClass, classify_error


@pytest.mark.parametrize(
    "message, expected",
    [
        ("Error: rate_limit hit on this account", FailureClass.RATE_LIMIT),
        ("HTTP 429 too many requests", FailureClass.RATE_LIMIT),
        ("usage limit exceeded for plan", FailureClass.RATE_LIMIT),
        ("API quota exhausted", FailureClass.RATE_LIMIT),
        ("Service is overloaded right now", FailureClass.OVERLOAD),
        ("Got HTTP 529 from upstream", FailureClass.OVERLOAD),
        ("503 Service Unavailable", FailureClass.OVERLOAD),
        ("Temporarily unavailable, try again", FailureClass.OVERLOAD),
        ("prompt is too long for context window", FailureClass.CONTEXT_LIMIT),
        ("context limit exceeded", FailureClass.CONTEXT_LIMIT),
        ("too many tokens in input", FailureClass.CONTEXT_LIMIT),
        ("maximum context exceeded", FailureClass.CONTEXT_LIMIT),
        ("ConnectionError on socket.send", FailureClass.UNKNOWN),
        ("agent ended turn without calling submit_enrichment", FailureClass.UNKNOWN),
        ("", FailureClass.UNKNOWN),
    ],
)
def test_classify_message(message: str, expected: FailureClass) -> None:
    assert classify_error(message) == expected


def test_classify_uses_extra_context() -> None:
    # Empty primary message but extra context contains the signal.
    assert classify_error("", "Server returned 429") == FailureClass.RATE_LIMIT


def test_rate_limit_takes_priority_over_overload() -> None:
    # If both phrases appear, rate_limit wins (it's checked first).
    assert (
        classify_error("503 service unavailable due to rate limit")
        == FailureClass.RATE_LIMIT
    )


def test_failure_class_string_serialization() -> None:
    assert FailureClass.RATE_LIMIT.value == "rate_limit"
    assert FailureClass.OVERLOAD.value == "overload"
    assert FailureClass.CONTEXT_LIMIT.value == "context_limit"
    assert FailureClass.UNKNOWN.value == "unknown"
