"""Unit tests for the LLM matcher.

The actual `chat()` is stubbed — these tests verify the matcher's
parsing + threshold + no-double-mapping logic, NOT the model's quality.
For end-to-end model quality, see tests/test_matcher_integration.py
(opt-in, hits the network).
"""
from __future__ import annotations

import pytest

from jira_task_agent.pipeline import matcher
from jira_task_agent.pipeline.matcher import MatchInput


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _stub_chat(response: dict, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace `matcher.chat` with a stub returning `response`. Records the
    user-message + kwargs of every call into the returned list."""
    calls: list[dict] = []

    def fake_chat(*, system, user, models, temperature=0.0, json_mode=False, **_):
        calls.append(
            {"system": system, "user": user, "models": models,
             "temperature": temperature, "json_mode": json_mode}
        )
        return (response, {"model": "stub"})

    monkeypatch.setattr(matcher, "chat", fake_chat)
    return calls


# ----------------------------------------------------------------------
# tests
# ----------------------------------------------------------------------


def test_empty_items_returns_no_decisions(monkeypatch: pytest.MonkeyPatch):
    calls = _stub_chat({"matches": []}, monkeypatch)
    out = matcher.match(items=[], candidates=[MatchInput(key="X", summary="x")], kind="task")
    assert out == []
    assert calls == [], "should NOT call the LLM when items is empty"


def test_empty_candidates_marks_all_items_new(monkeypatch: pytest.MonkeyPatch):
    calls = _stub_chat({"matches": []}, monkeypatch)
    out = matcher.match(
        items=[MatchInput(summary="a"), MatchInput(summary="b")],
        candidates=[],
        kind="task",
    )
    assert len(out) == 2
    assert all(d.candidate_key is None for d in out)
    assert calls == [], "should NOT call the LLM when candidates is empty"


def test_high_confidence_match_returned(monkeypatch: pytest.MonkeyPatch):
    response = {
        "matches": [
            {
                "item_index": 0,
                "candidate_key": "CENTPM-1",
                "confidence": 0.95,
                "reason": "Both describe JWT secret generation",
            }
        ]
    }
    _stub_chat(response, monkeypatch)
    out = matcher.match(
        items=[MatchInput(summary="Generate JWT secret and store in Vault")],
        candidates=[MatchInput(key="CENTPM-1", summary="JWT secret")],
        kind="task",
    )
    assert len(out) == 1
    assert out[0].candidate_key == "CENTPM-1"
    assert out[0].confidence == 0.95


def test_low_confidence_below_threshold_returns_none(monkeypatch: pytest.MonkeyPatch):
    response = {
        "matches": [
            {
                "item_index": 0,
                "candidate_key": "CENTPM-1",
                "confidence": 0.5,  # below the 0.70 threshold
                "reason": "Only weak signal",
            }
        ]
    }
    _stub_chat(response, monkeypatch)
    out = matcher.match(
        items=[MatchInput(summary="x")],
        candidates=[MatchInput(key="CENTPM-1", summary="y")],
        kind="task",
    )
    assert out[0].candidate_key is None
    # confidence is preserved for audit even when key is dropped
    assert out[0].confidence == 0.5


def test_no_double_mapping(monkeypatch: pytest.MonkeyPatch):
    """If the LLM returns the same candidate_key for two items, the
    second one is dropped to None — wrong-merge prevention."""
    response = {
        "matches": [
            {
                "item_index": 0,
                "candidate_key": "CENTPM-7",
                "confidence": 0.95,
                "reason": "first match",
            },
            {
                "item_index": 1,
                "candidate_key": "CENTPM-7",  # same key again
                "confidence": 0.93,
                "reason": "model collided",
            },
        ]
    }
    _stub_chat(response, monkeypatch)
    out = matcher.match(
        items=[MatchInput(summary="a"), MatchInput(summary="b")],
        candidates=[MatchInput(key="CENTPM-7", summary="thing")],
        kind="task",
    )
    assert out[0].candidate_key == "CENTPM-7"
    assert out[1].candidate_key is None  # collision dropped
    assert "already mapped" in (out[1].reason or "").lower()


def test_missing_match_for_item_marks_it_new(monkeypatch: pytest.MonkeyPatch):
    """If the LLM omits a decision for an item index, the matcher
    should still emit a no-match entry for that index."""
    response = {
        "matches": [
            {"item_index": 0, "candidate_key": "CENTPM-1",
             "confidence": 0.9, "reason": "ok"},
            # item_index 1 omitted
        ]
    }
    _stub_chat(response, monkeypatch)
    out = matcher.match(
        items=[MatchInput(summary="a"), MatchInput(summary="b")],
        candidates=[MatchInput(key="CENTPM-1", summary="x")],
        kind="task",
    )
    assert len(out) == 2
    assert out[0].candidate_key == "CENTPM-1"
    assert out[1].candidate_key is None
    assert out[1].confidence == 0.0


def test_kind_is_passed_to_prompt(monkeypatch: pytest.MonkeyPatch):
    """Sanity: the user message should mention `kind` so the prompt's
    domain hints fire (epic vs task)."""
    calls = _stub_chat({"matches": []}, monkeypatch)
    matcher.match(
        items=[MatchInput(summary="x")],
        candidates=[MatchInput(key="K", summary="y")],
        kind="epic",
    )
    assert len(calls) == 1
    assert "epic" in calls[0]["user"]


def test_json_mode_is_requested(monkeypatch: pytest.MonkeyPatch):
    calls = _stub_chat({"matches": []}, monkeypatch)
    matcher.match(
        items=[MatchInput(summary="x")],
        candidates=[MatchInput(key="K", summary="y")],
        kind="task",
    )
    assert calls[0]["json_mode"] is True
    assert calls[0]["temperature"] == 0.0
