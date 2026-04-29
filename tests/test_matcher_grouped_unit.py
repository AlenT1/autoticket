"""Unit tests for the grouped matcher + run_matcher orchestrator.

Stubs `chat()` so no network / LLM is involved. Verifies:
  - grouped output is parsed correctly per group
  - per-group threshold + no-double-mapping
  - cross-group bleed is impossible (data structure enforces it)
  - empty groups are returned with empty decisions
  - `run_matcher` orchestrates Stage-1 (flat epic match) and Stage-2
    (grouped task match) and produces a coherent MatcherResult
"""
from __future__ import annotations

import pytest

from jira_task_agent.pipeline import matcher
from jira_task_agent.pipeline.matcher import (
    GroupInput,
    MatchInput,
)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


class _ChatStub:
    """Patches matcher.chat to return scripted responses based on which
    system prompt was sent. Records every invocation so tests can assert
    on call counts."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self.calls: list[dict] = []
        self._scripts: dict[str, list[dict]] = {"flat": [], "grouped": []}
        monkeypatch.setattr(matcher, "chat", self._fake_chat)

    def script_flat(self, response: dict) -> None:
        self._scripts["flat"].append(response)

    def script_grouped(self, response: dict) -> None:
        self._scripts["grouped"].append(response)

    def _fake_chat(self, *, system, user, models, temperature=0.0, json_mode=False, **_):
        self.calls.append({"system": system, "user": user, "json_mode": json_mode})
        # Branch on which prompt is in the user message.
        if '"groups": [' in user or "GROUPS (input):" in user:
            kind = "grouped"
        else:
            kind = "flat"
        if not self._scripts[kind]:
            return ({}, {"model": "stub"})
        return (self._scripts[kind].pop(0), {"model": "stub"})


# ----------------------------------------------------------------------
# match_grouped tests
# ----------------------------------------------------------------------


def test_match_grouped_empty_returns_empty_list():
    out = matcher.match_grouped([], kind="task")
    assert out == []


def test_match_grouped_per_group_isolation(monkeypatch):
    """Two groups, distinct candidates, distinct items. Verify the
    parser routes each group's matches back correctly."""
    chat = _ChatStub(monkeypatch)
    chat.script_grouped(
        {
            "groups": [
                {
                    "group_id": "G1",
                    "matches": [
                        {
                            "item_index": 0,
                            "candidate_key": "K-A",
                            "confidence": 0.95,
                            "reason": "g1 match",
                        }
                    ],
                },
                {
                    "group_id": "G2",
                    "matches": [
                        {
                            "item_index": 0,
                            "candidate_key": "K-B",
                            "confidence": 0.92,
                            "reason": "g2 match",
                        }
                    ],
                },
            ]
        }
    )

    groups = [
        GroupInput(
            group_id="G1",
            items=[MatchInput(summary="alpha")],
            candidates=[MatchInput(key="K-A", summary="alpha-ish")],
        ),
        GroupInput(
            group_id="G2",
            items=[MatchInput(summary="beta")],
            candidates=[MatchInput(key="K-B", summary="beta-ish")],
        ),
    ]
    out = matcher.match_grouped(groups, kind="task", batch_size=4, max_workers=1)
    assert len(out) == 2
    assert out[0].group_id == "G1"
    assert out[0].decisions[0].candidate_key == "K-A"
    assert out[1].group_id == "G2"
    assert out[1].decisions[0].candidate_key == "K-B"
    # one LLM call (one batch)
    assert len(chat.calls) == 1


def test_match_grouped_threshold_drops_low_confidence(monkeypatch):
    chat = _ChatStub(monkeypatch)
    chat.script_grouped(
        {
            "groups": [
                {
                    "group_id": "G1",
                    "matches": [
                        {
                            "item_index": 0,
                            "candidate_key": "K-A",
                            "confidence": 0.5,
                            "reason": "weak",
                        }
                    ],
                }
            ]
        }
    )
    groups = [
        GroupInput(
            group_id="G1",
            items=[MatchInput(summary="x")],
            candidates=[MatchInput(key="K-A", summary="y")],
        )
    ]
    out = matcher.match_grouped(groups, kind="task", batch_size=4, max_workers=1)
    assert out[0].decisions[0].candidate_key is None
    assert out[0].decisions[0].confidence == 0.5


def test_match_grouped_allows_rollup_within_group(monkeypatch):
    """Within one group, the same candidate cited twice IS preserved —
    the rollup pattern is legitimate at the task level. The reconciler
    later detects this and emits `covered_by_rollup` actions."""
    chat = _ChatStub(monkeypatch)
    chat.script_grouped(
        {
            "groups": [
                {
                    "group_id": "G1",
                    "matches": [
                        {
                            "item_index": 0,
                            "candidate_key": "K-ROLLUP",
                            "confidence": 0.92,
                            "reason": "covered by K-ROLLUP's description bullet 1",
                        },
                        {
                            "item_index": 1,
                            "candidate_key": "K-ROLLUP",
                            "confidence": 0.90,
                            "reason": "covered by K-ROLLUP's description bullet 2",
                        },
                    ],
                }
            ]
        }
    )
    groups = [
        GroupInput(
            group_id="G1",
            items=[MatchInput(summary="step 1"), MatchInput(summary="step 2")],
            candidates=[
                MatchInput(
                    key="K-ROLLUP",
                    summary="Migration",
                    description="bullet 1\nbullet 2",
                )
            ],
        )
    ]
    out = matcher.match_grouped(groups, kind="task", batch_size=4, max_workers=1)
    # Both decisions keep the same key — caller (the reconciler) is the
    # one that detects the rollup pattern.
    assert out[0].decisions[0].candidate_key == "K-ROLLUP"
    assert out[0].decisions[1].candidate_key == "K-ROLLUP"


def test_match_grouped_empty_group_skipped_no_llm_call(monkeypatch):
    """A group with no items contributes no LLM call and gets empty decisions."""
    chat = _ChatStub(monkeypatch)
    groups = [
        GroupInput(
            group_id="G_EMPTY",
            items=[],
            candidates=[MatchInput(key="X", summary="x")],
        )
    ]
    out = matcher.match_grouped(groups, kind="task", batch_size=4, max_workers=1)
    assert out[0].decisions == []
    # Even though the batch was non-empty, only empty groups inside →
    # no LLM call is made.
    assert chat.calls == []


def test_match_grouped_batches_split_correctly(monkeypatch):
    """5 groups, batch_size=2 → 3 batches → 3 LLM calls."""
    chat = _ChatStub(monkeypatch)
    # Script 3 responses, one per batch
    for batch_idx in range(3):
        chat.script_grouped(
            {
                "groups": [
                    {
                        "group_id": f"G{i}",
                        "matches": [
                            {
                                "item_index": 0,
                                "candidate_key": f"K{i}",
                                "confidence": 0.9,
                                "reason": "ok",
                            }
                        ],
                    }
                    for i in range(batch_idx * 2, min(batch_idx * 2 + 2, 5))
                ]
            }
        )

    groups = [
        GroupInput(
            group_id=f"G{i}",
            items=[MatchInput(summary=f"item-{i}")],
            candidates=[MatchInput(key=f"K{i}", summary=f"cand-{i}")],
        )
        for i in range(5)
    ]
    out = matcher.match_grouped(groups, kind="task", batch_size=2, max_workers=1)
    assert len(out) == 5
    assert len(chat.calls) == 3  # 3 batches → 3 LLM calls


# ----------------------------------------------------------------------
# run_matcher orchestrator tests (stubbed)
# ----------------------------------------------------------------------


class _FakeExtractedTask:
    def __init__(self, summary, description=""):
        self.summary = summary
        self.description = description


class _FakeExtractedEpic:
    def __init__(self, summary, description="", assignee_name=None):
        self.summary = summary
        self.description = description
        self.assignee_name = assignee_name


class _FakeSingleExtraction:
    """Duck-types as `pipeline.extractor.ExtractionResult`."""

    def __init__(self, file_id, file_name, epic, tasks):
        self.file_id = file_id
        self.file_name = file_name
        self.epic = epic
        self.tasks = tasks


class _FakeMultiExtraction:
    """Duck-types as `pipeline.extractor.MultiExtractionResult`."""

    def __init__(self, file_id, file_name, epics):
        self.file_id = file_id
        self.file_name = file_name
        self.epics = epics  # list of objects each with .summary, .description, .assignee_name, .tasks


class _FakeMultiEpic:
    def __init__(self, summary, description, assignee_name, tasks):
        self.summary = summary
        self.description = description
        self.assignee_name = assignee_name
        self.tasks = tasks


def test_run_matcher_end_to_end_stubbed(monkeypatch):
    """One single_epic file (matches CENTPM-1) + one multi_epic file with 2
    sub-epics (one matches CENTPM-2, the other has no match) → orchestrator
    should produce 3 FileEpicResult, 2 with matches, 1 without; Stage-2
    should run only on matched ones; orphans flagged.
    """
    chat = _ChatStub(monkeypatch)

    # Stage 1 — flat epic match call
    # 3 items: single's epic, multi's epic[0], multi's epic[1]
    chat.script_flat(
        {
            "matches": [
                {"item_index": 0, "candidate_key": "CENTPM-1",
                 "confidence": 0.95, "reason": "single → 1"},
                {"item_index": 1, "candidate_key": "CENTPM-2",
                 "confidence": 0.92, "reason": "multi[0] → 2"},
                {"item_index": 2, "candidate_key": None,
                 "confidence": 0.0, "reason": "multi[1] no match"},
            ]
        }
    )
    # Stage 2 — grouped task match. 2 matched groups → fits in one batch.
    chat.script_grouped(
        {
            "groups": [
                {
                    "group_id": "F1#0@CENTPM-1",
                    "matches": [
                        {"item_index": 0, "candidate_key": "CENTPM-1.1",
                         "confidence": 0.95, "reason": "task A → 1.1"},
                        {"item_index": 1, "candidate_key": None,
                         "confidence": 0.0, "reason": "task B is new"},
                    ],
                },
                {
                    "group_id": "F2#0@CENTPM-2",
                    "matches": [
                        {"item_index": 0, "candidate_key": "CENTPM-2.1",
                         "confidence": 0.97, "reason": "deploy → 2.1"},
                    ],
                },
            ]
        }
    )

    extractions = [
        (
            None,
            _FakeSingleExtraction(
                file_id="F1",
                file_name="F1.md",
                epic=_FakeExtractedEpic(summary="alpha epic",
                                        description="single",
                                        assignee_name="Saar"),
                tasks=[
                    _FakeExtractedTask("Task A"),
                    _FakeExtractedTask("Task B"),
                ],
            ),
        ),
        (
            None,
            _FakeMultiExtraction(
                file_id="F2",
                file_name="F2.md",
                epics=[
                    _FakeMultiEpic(
                        summary="multi A — deployment",
                        description="multi.0",
                        assignee_name="Yuval",
                        tasks=[_FakeExtractedTask("deploy job")],
                    ),
                    _FakeMultiEpic(
                        summary="multi B — wholly new feature",
                        description="multi.1",
                        assignee_name="Lior",
                        tasks=[_FakeExtractedTask("feature task")],
                    ),
                ],
            ),
        ),
    ]

    project_tree = {
        "epics": [
            {
                "key": "CENTPM-1",
                "summary": "Alpha",
                "description": "alpha description",
                "children": [
                    {"key": "CENTPM-1.1", "summary": "alpha child a",
                     "description": ""},
                    {"key": "CENTPM-1.2", "summary": "alpha child b",
                     "description": ""},  # will be orphaned
                ],
            },
            {
                "key": "CENTPM-2",
                "summary": "Deployment",
                "description": "",
                "children": [
                    {"key": "CENTPM-2.1", "summary": "deploy",
                     "description": ""},
                ],
            },
        ]
    }

    result = matcher.run_matcher(
        extractions, project_tree, batch_size=4, max_workers=1
    )

    assert len(result.file_results) == 3
    fr1, fr2, fr3 = result.file_results

    # single_epic file → matched CENTPM-1
    assert fr1.file_id == "F1"
    assert fr1.section_index == 0
    assert fr1.matched_jira_key == "CENTPM-1"
    assert len(fr1.task_decisions) == 2
    assert fr1.task_decisions[0].candidate_key == "CENTPM-1.1"
    assert fr1.task_decisions[1].candidate_key is None  # task B → new
    # CENTPM-1.2 has no extracted-task counterpart → orphan
    assert fr1.orphan_keys == ["CENTPM-1.2"]

    # multi_epic[0] → matched CENTPM-2
    assert fr2.file_id == "F2"
    assert fr2.section_index == 0
    assert fr2.matched_jira_key == "CENTPM-2"
    assert len(fr2.task_decisions) == 1
    assert fr2.task_decisions[0].candidate_key == "CENTPM-2.1"
    assert fr2.orphan_keys == []

    # multi_epic[1] → no match → no Stage-2 call for this group
    assert fr3.file_id == "F2"
    assert fr3.section_index == 1
    assert fr3.matched_jira_key is None
    assert fr3.task_decisions == []
    assert fr3.orphan_keys == []

    # Two LLM calls total: 1 epic + 1 grouped task call (both matched
    # groups fit in one batch of size 4)
    assert len(chat.calls) == 2
