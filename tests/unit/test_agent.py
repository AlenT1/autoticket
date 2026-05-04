"""Tests for the EnrichmentAgent loop, using a scripted fake Anthropic client."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from file_to_jira.config import RepoAlias
from file_to_jira.enrich.agent import (
    EnrichmentAgent,
    EnrichmentTruncated,
    format_initial_prompt,
)
from file_to_jira.enrich.tools import Toolkit, build_submit_tool
from file_to_jira.models import ModuleContext, ParsedBug
from file_to_jira.repocache import RepoCacheManager
from tests.fixtures.git_repo import make_sample_repo


# ---------------------------------------------------------------------------
# Fake Anthropic client: scripts a sequence of responses
# ---------------------------------------------------------------------------


@dataclass
class FakeContentBlock:
    type: str
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    text: str = ""


@dataclass
class FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class FakeMessage:
    content: list[FakeContentBlock]
    stop_reason: str
    usage: FakeUsage = field(default_factory=FakeUsage)


class FakeMessages:
    def __init__(self, parent: "FakeAnthropic") -> None:
        self.parent = parent

    def create(self, **kwargs: Any) -> FakeMessage:
        self.parent.calls.append(kwargs)
        if not self.parent.responses:
            raise RuntimeError("FakeAnthropic ran out of scripted responses")
        return self.parent.responses.pop(0)


class FakeAnthropic:
    def __init__(self, responses: list[FakeMessage]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []
        self.messages = FakeMessages(self)


# ---------------------------------------------------------------------------
# Helpers to script tool_use turns
# ---------------------------------------------------------------------------


def tool_use_turn(*calls: tuple[str, str, dict]) -> FakeMessage:
    """Build a tool_use response containing N tool calls.

    Each entry is (id, tool_name, tool_input).
    """
    blocks = [
        FakeContentBlock(type="tool_use", id=tu_id, name=name, input=args)
        for tu_id, name, args in calls
    ]
    return FakeMessage(
        content=blocks,
        stop_reason="tool_use",
        usage=FakeUsage(input_tokens=100, output_tokens=50),
    )


def end_turn() -> FakeMessage:
    return FakeMessage(
        content=[FakeContentBlock(type="text", text="(no tool call)")],
        stop_reason="end_turn",
        usage=FakeUsage(input_tokens=10, output_tokens=10),
    )


# ---------------------------------------------------------------------------
# Real fixture setup
# ---------------------------------------------------------------------------


@pytest.fixture
def upstream_repo(tmp_path: Path) -> Path:
    return make_sample_repo(tmp_path / "upstream")


@pytest.fixture
def toolkit(tmp_path: Path, upstream_repo: Path) -> Toolkit:
    cache = RepoCacheManager(
        cache_dir=tmp_path / "cache",
        aliases={
            "sample": RepoAlias(
                url=upstream_repo.resolve().as_uri(),
                auth="ssh-default",
                default_branch="main",
            ),
        },
    )
    return Toolkit(cache)


@pytest.fixture
def parsed_bug() -> ParsedBug:
    return ParsedBug(
        bug_id="abc1234567890def",
        external_id="SAMPLE-001",
        source_line_start=1,
        source_line_end=10,
        raw_title="multiply function regression",
        raw_body="**What's broken:** the multiply function fails on negatives.",
        hinted_priority="P1",
        inherited_module=ModuleContext(
            repo_alias="sample",
            branch="main",
            commit_sha="abc",
        ),
    )


def _enriched_payload(bug_id: str = "abc1234567890def") -> dict:
    return {
        "bug_id": bug_id,
        "summary": "multiply fails on negative inputs",
        "description_md": "Symptom: multiply(-1, 2) returns -2; expected -2.\n",
        "priority": "P1",
        "code_references": [
            {"repo_alias": "sample", "file_path": "src/main.py", "line_start": 6}
        ],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_initial_prompt_contains_bug_metadata(parsed_bug: ParsedBug) -> None:
    text = format_initial_prompt(parsed_bug)
    assert parsed_bug.external_id in text
    assert parsed_bug.raw_title in text
    assert parsed_bug.inherited_module.repo_alias in text
    assert "P1" in text


def test_happy_path_two_turns(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    """Turn 1: agent calls clone_repo. Turn 2: agent calls submit_enrichment."""
    submit_tool = build_submit_tool({"sample": upstream_repo})

    fake = FakeAnthropic(
        [
            tool_use_turn(("t1", "clone_repo", {"repo_alias": "sample"})),
            tool_use_turn(("t2", "submit_enrichment", _enriched_payload())),
        ]
    )

    agent = EnrichmentAgent(
        toolkit=toolkit,
        submit_tool=submit_tool,
        client=fake,
        system_prompt="(test prompt)",
        max_turns=5,
    )
    enriched = agent.enrich(parsed_bug)
    assert enriched.summary == "multiply fails on negative inputs"
    assert enriched.priority == "P1"
    assert enriched.enrichment_meta.tool_calls == 2
    assert "sample" in enriched.enrichment_meta.repos_touched


def test_submit_validation_failure_then_retry(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    """Agent submits with a hallucinated file path, gets validation error,
    retries with a valid path, and succeeds."""
    submit_tool = build_submit_tool({"sample": upstream_repo})

    bad_payload = _enriched_payload()
    bad_payload["code_references"] = [
        {"repo_alias": "sample", "file_path": "src/does_not_exist.py"}
    ]
    good_payload = _enriched_payload()

    fake = FakeAnthropic(
        [
            tool_use_turn(("t1", "submit_enrichment", bad_payload)),
            tool_use_turn(("t2", "submit_enrichment", good_payload)),
        ]
    )
    agent = EnrichmentAgent(
        toolkit=toolkit,
        submit_tool=submit_tool,
        client=fake,
        system_prompt="(test prompt)",
        max_turns=5,
    )
    enriched = agent.enrich(parsed_bug)
    assert enriched is not None
    # Two attempts means two tool calls.
    assert enriched.enrichment_meta.tool_calls == 2


def test_truncates_when_agent_ends_turn_without_submitting(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    submit_tool = build_submit_tool({"sample": upstream_repo})
    fake = FakeAnthropic([end_turn()])
    agent = EnrichmentAgent(
        toolkit=toolkit,
        submit_tool=submit_tool,
        client=fake,
        system_prompt="(test prompt)",
        max_turns=3,
    )
    with pytest.raises(EnrichmentTruncated):
        agent.enrich(parsed_bug)


def test_truncates_when_max_turns_exhausted(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    submit_tool = build_submit_tool({"sample": upstream_repo})
    # Three turns of tool_use, none of them submit_enrichment.
    fake = FakeAnthropic(
        [
            tool_use_turn(("t1", "clone_repo", {"repo_alias": "sample"})),
            tool_use_turn(("t2", "list_dir", {"repo_alias": "sample"})),
            tool_use_turn(("t3", "list_dir", {"repo_alias": "sample"})),
        ]
    )
    agent = EnrichmentAgent(
        toolkit=toolkit,
        submit_tool=submit_tool,
        client=fake,
        system_prompt="(test prompt)",
        max_turns=3,
    )
    with pytest.raises(EnrichmentTruncated):
        agent.enrich(parsed_bug)


def test_tool_dispatch_routes_to_toolkit(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    """Verify a non-submit tool call actually invokes the toolkit method."""
    submit_tool = build_submit_tool({"sample": upstream_repo})
    fake = FakeAnthropic(
        [
            tool_use_turn(("t1", "list_dir", {"repo_alias": "sample"})),
            tool_use_turn(("t2", "submit_enrichment", _enriched_payload())),
        ]
    )
    agent = EnrichmentAgent(
        toolkit=toolkit,
        submit_tool=submit_tool,
        client=fake,
        system_prompt="(test prompt)",
        max_turns=5,
    )
    enriched = agent.enrich(parsed_bug)
    # The tool_result for list_dir should be in the second create() call's messages.
    second_call = fake.calls[1]
    # The tool-result message is the LAST user message (initial prompt is the first).
    user_msg = next(
        m for m in reversed(second_call["messages"])
        if m["role"] == "user" and isinstance(m["content"], list)
    )
    assert isinstance(user_msg["content"], list)
    tool_result_text = json.loads(user_msg["content"][0]["content"])
    assert "entries" in tool_result_text
    assert any(e["name"] == "src" for e in tool_result_text["entries"])
    assert enriched is not None


def test_unknown_tool_returns_error_to_agent(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    submit_tool = build_submit_tool({"sample": upstream_repo})
    fake = FakeAnthropic(
        [
            tool_use_turn(("t1", "ghost_tool", {"x": 1})),
            tool_use_turn(("t2", "submit_enrichment", _enriched_payload())),
        ]
    )
    agent = EnrichmentAgent(
        toolkit=toolkit,
        submit_tool=submit_tool,
        client=fake,
        system_prompt="(test prompt)",
        max_turns=5,
    )
    enriched = agent.enrich(parsed_bug)
    second_call = fake.calls[1]
    # The tool-result message is the LAST user message (initial prompt is the first).
    user_msg = next(
        m for m in reversed(second_call["messages"])
        if m["role"] == "user" and isinstance(m["content"], list)
    )
    payload = json.loads(user_msg["content"][0]["content"])
    assert "unknown tool" in payload["error"]
    assert enriched is not None


def test_prompt_caching_is_set_on_system_block(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    submit_tool = build_submit_tool({"sample": upstream_repo})
    fake = FakeAnthropic(
        [tool_use_turn(("t1", "submit_enrichment", _enriched_payload()))]
    )
    agent = EnrichmentAgent(
        toolkit=toolkit,
        submit_tool=submit_tool,
        client=fake,
        system_prompt="(test prompt)",
        max_turns=2,
        enable_prompt_caching=True,
    )
    agent.enrich(parsed_bug)
    first_call = fake.calls[0]
    assert first_call["system"][0]["cache_control"] == {"type": "ephemeral"}
    # The last tool in the registry also gets cache_control.
    assert first_call["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_caching_can_be_disabled(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    submit_tool = build_submit_tool({"sample": upstream_repo})
    fake = FakeAnthropic(
        [tool_use_turn(("t1", "submit_enrichment", _enriched_payload()))]
    )
    agent = EnrichmentAgent(
        toolkit=toolkit,
        submit_tool=submit_tool,
        client=fake,
        system_prompt="(test prompt)",
        max_turns=2,
        enable_prompt_caching=False,
    )
    agent.enrich(parsed_bug)
    first_call = fake.calls[0]
    assert "cache_control" not in first_call["system"][0]
