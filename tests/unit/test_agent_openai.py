"""Tests for the OpenAI-compatible enrichment agent.

Mirrors test_agent.py with a fake OpenAI client. Verifies that:
- happy path: agent submits, returns EnrichedBug
- schema retry: invalid submit_enrichment payload returns errors and the agent
  re-submits with a corrected payload
- truncation when stop reason is "stop"
- truncation when max_turns is exhausted
- non-submit tool dispatch routes to the toolkit
- unknown tool returns an error to the model
- argument-JSON-decode errors surface as a tool result, not a crash
- token counting from response.usage works
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from file_to_jira.config import RepoAlias
from file_to_jira.enrich.agent_openai import (
    OpenAIEnrichmentAgent,
    build_openai_tool_registry,
)
from file_to_jira.enrich.agent import EnrichmentTruncated
from file_to_jira.enrich.tools import Toolkit, build_submit_tool
from file_to_jira.models import ModuleContext, ParsedBug
from file_to_jira.repocache import RepoCacheManager
from tests.fixtures.git_repo import make_sample_repo


# ---------------------------------------------------------------------------
# Fake OpenAI client mirroring the SDK's response shape
# ---------------------------------------------------------------------------


@dataclass
class FakeFunction:
    name: str
    arguments: str  # JSON-encoded string per OpenAI spec


@dataclass
class FakeToolCall:
    id: str
    function: FakeFunction
    type: str = "function"


@dataclass
class FakeMessage:
    content: str | None = None
    tool_calls: list[FakeToolCall] | None = None


@dataclass
class FakeChoice:
    message: FakeMessage
    finish_reason: str
    index: int = 0


@dataclass
class FakeUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class FakeChatCompletion:
    choices: list[FakeChoice]
    usage: FakeUsage = field(default_factory=FakeUsage)


class FakeChatCompletions:
    def __init__(self, parent: "FakeOpenAI") -> None:
        self.parent = parent

    def create(self, **kwargs: Any) -> FakeChatCompletion:
        self.parent.calls.append(kwargs)
        if not self.parent.responses:
            raise RuntimeError("FakeOpenAI ran out of scripted responses")
        return self.parent.responses.pop(0)


class FakeChat:
    def __init__(self, parent: "FakeOpenAI") -> None:
        self.completions = FakeChatCompletions(parent)


class FakeOpenAI:
    def __init__(self, responses: list[FakeChatCompletion]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []
        self.chat = FakeChat(self)


# ---------------------------------------------------------------------------
# Helpers to script tool_calls turns
# ---------------------------------------------------------------------------


def tool_call_turn(*calls: tuple[str, str, dict | str]) -> FakeChatCompletion:
    """Build a tool_calls response. Each entry is (id, name, args_dict_or_string)."""
    tool_calls = [
        FakeToolCall(
            id=tc_id,
            function=FakeFunction(
                name=name,
                arguments=args if isinstance(args, str) else json.dumps(args),
            ),
        )
        for tc_id, name, args in calls
    ]
    return FakeChatCompletion(
        choices=[
            FakeChoice(
                message=FakeMessage(content=None, tool_calls=tool_calls),
                finish_reason="tool_calls",
            )
        ],
        usage=FakeUsage(prompt_tokens=100, completion_tokens=50),
    )


def stop_turn() -> FakeChatCompletion:
    return FakeChatCompletion(
        choices=[
            FakeChoice(
                message=FakeMessage(content="(no tool call)", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=FakeUsage(prompt_tokens=10, completion_tokens=10),
    )


# ---------------------------------------------------------------------------
# Fixtures
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
            repo_alias="sample", branch="main", commit_sha="abc"
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
# Module-level helpers
# ---------------------------------------------------------------------------


def test_tool_registry_translates_to_openai_shape() -> None:
    """build_openai_tool_registry wraps each tool in {type:function, function:{...}}."""
    reg = build_openai_tool_registry()
    assert reg, "registry must not be empty"
    for entry in reg:
        assert entry["type"] == "function"
        assert "function" in entry
        fn = entry["function"]
        assert {"name", "description", "parameters"}.issubset(fn.keys())
        # cache_control is Anthropic-only and must be stripped.
        assert "cache_control" not in entry
        assert "cache_control" not in fn


def test_tool_registry_includes_submit_enrichment() -> None:
    reg = build_openai_tool_registry()
    names = {t["function"]["name"] for t in reg}
    assert "submit_enrichment" in names
    assert "clone_repo" in names
    assert "search_code" in names


# ---------------------------------------------------------------------------
# End-to-end agent loop
# ---------------------------------------------------------------------------


def test_happy_path_two_turns(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    submit_tool = build_submit_tool({"sample": upstream_repo})
    fake = FakeOpenAI(
        [
            tool_call_turn(("t1", "clone_repo", {"repo_alias": "sample"})),
            tool_call_turn(("t2", "submit_enrichment", _enriched_payload())),
        ]
    )
    agent = OpenAIEnrichmentAgent(
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


def test_schema_validation_failure_then_retry(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    submit_tool = build_submit_tool({"sample": upstream_repo})
    bad = _enriched_payload()
    bad["code_references"] = [
        {"repo_alias": "sample", "file_path": "src/does_not_exist.py"}
    ]
    fake = FakeOpenAI(
        [
            tool_call_turn(("t1", "submit_enrichment", bad)),
            tool_call_turn(("t2", "submit_enrichment", _enriched_payload())),
        ]
    )
    agent = OpenAIEnrichmentAgent(
        toolkit=toolkit,
        submit_tool=submit_tool,
        client=fake,
        system_prompt="(test prompt)",
        max_turns=5,
    )
    enriched = agent.enrich(parsed_bug)
    assert enriched is not None
    assert enriched.enrichment_meta.tool_calls == 2


def test_truncates_on_stop_finish_reason(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    submit_tool = build_submit_tool({"sample": upstream_repo})
    fake = FakeOpenAI([stop_turn()])
    agent = OpenAIEnrichmentAgent(
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
    fake = FakeOpenAI(
        [
            tool_call_turn(("t1", "list_dir", {"repo_alias": "sample"})),
            tool_call_turn(("t2", "list_dir", {"repo_alias": "sample"})),
            tool_call_turn(("t3", "list_dir", {"repo_alias": "sample"})),
        ]
    )
    agent = OpenAIEnrichmentAgent(
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
    submit_tool = build_submit_tool({"sample": upstream_repo})
    fake = FakeOpenAI(
        [
            tool_call_turn(("t1", "list_dir", {"repo_alias": "sample"})),
            tool_call_turn(("t2", "submit_enrichment", _enriched_payload())),
        ]
    )
    agent = OpenAIEnrichmentAgent(
        toolkit=toolkit,
        submit_tool=submit_tool,
        client=fake,
        system_prompt="(test prompt)",
        max_turns=5,
    )
    agent.enrich(parsed_bug)
    # Inspect the second create() call's messages — list_dir's tool result
    # must be there as a `role: tool` message.
    second = fake.calls[1]
    tool_msgs = [m for m in second["messages"] if m.get("role") == "tool"]
    assert tool_msgs, "expected at least one tool result message"
    payload = json.loads(tool_msgs[0]["content"])
    assert "entries" in payload
    assert any(e["name"] == "src" for e in payload["entries"])


def test_unknown_tool_returns_error(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    submit_tool = build_submit_tool({"sample": upstream_repo})
    fake = FakeOpenAI(
        [
            tool_call_turn(("t1", "ghost_tool", {"x": 1})),
            tool_call_turn(("t2", "submit_enrichment", _enriched_payload())),
        ]
    )
    agent = OpenAIEnrichmentAgent(
        toolkit=toolkit,
        submit_tool=submit_tool,
        client=fake,
        system_prompt="(test prompt)",
        max_turns=5,
    )
    agent.enrich(parsed_bug)
    second = fake.calls[1]
    tool_msgs = [m for m in second["messages"] if m.get("role") == "tool"]
    payload = json.loads(tool_msgs[0]["content"])
    assert "unknown tool" in payload["error"]


def test_invalid_arguments_json_does_not_crash(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    """If the model returns malformed JSON in tool_calls.arguments, the agent
    must surface a tool_result error rather than raising."""
    submit_tool = build_submit_tool({"sample": upstream_repo})
    fake = FakeOpenAI(
        [
            tool_call_turn(("t1", "list_dir", "{not json")),  # raw bad string
            tool_call_turn(("t2", "submit_enrichment", _enriched_payload())),
        ]
    )
    agent = OpenAIEnrichmentAgent(
        toolkit=toolkit,
        submit_tool=submit_tool,
        client=fake,
        system_prompt="(test prompt)",
        max_turns=5,
    )
    enriched = agent.enrich(parsed_bug)
    assert enriched is not None
    second = fake.calls[1]
    tool_msgs = [m for m in second["messages"] if m.get("role") == "tool"]
    error_msgs = [
        json.loads(m["content"]) for m in tool_msgs if "error" in m["content"]
    ]
    assert any("invalid JSON args" in e["error"] for e in error_msgs)


def test_token_accounting_from_usage(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    """meta.input_tokens / output_tokens accumulate from response.usage."""
    submit_tool = build_submit_tool({"sample": upstream_repo})
    fake = FakeOpenAI(
        [tool_call_turn(("t1", "submit_enrichment", _enriched_payload()))]
    )
    agent = OpenAIEnrichmentAgent(
        toolkit=toolkit,
        submit_tool=submit_tool,
        client=fake,
        system_prompt="(test prompt)",
        max_turns=2,
    )
    enriched = agent.enrich(parsed_bug)
    assert enriched.enrichment_meta.input_tokens == 100
    assert enriched.enrichment_meta.output_tokens == 50


def test_system_message_is_first(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    """OpenAI expects the system prompt as a `role: system` message, not a kwarg."""
    submit_tool = build_submit_tool({"sample": upstream_repo})
    fake = FakeOpenAI(
        [tool_call_turn(("t1", "submit_enrichment", _enriched_payload()))]
    )
    agent = OpenAIEnrichmentAgent(
        toolkit=toolkit,
        submit_tool=submit_tool,
        client=fake,
        system_prompt="THIS IS THE TEST SYSTEM PROMPT",
        max_turns=2,
    )
    agent.enrich(parsed_bug)
    first_call = fake.calls[0]
    first_msg = first_call["messages"][0]
    assert first_msg["role"] == "system"
    assert first_msg["content"] == "THIS IS THE TEST SYSTEM PROMPT"


def test_passes_temperature_and_model(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    submit_tool = build_submit_tool({"sample": upstream_repo})
    fake = FakeOpenAI(
        [tool_call_turn(("t1", "submit_enrichment", _enriched_payload()))]
    )
    agent = OpenAIEnrichmentAgent(
        toolkit=toolkit,
        submit_tool=submit_tool,
        client=fake,
        system_prompt="(test prompt)",
        model="openai/openai/gpt-5.4-mini",
        temperature=0.7,
        max_turns=2,
    )
    agent.enrich(parsed_bug)
    first = fake.calls[0]
    assert first["model"] == "openai/openai/gpt-5.4-mini"
    assert first["temperature"] == pytest.approx(0.7)
    assert first["tool_choice"] == "auto"


def test_base_url_env_overrides_literal(monkeypatch) -> None:
    """When base_url_env names an env var that is set, its value beats the literal."""
    from file_to_jira.enrich.agent_openai import build_openai_client

    monkeypatch.setenv("NVIDIA_BASE_URL", "https://from-env.example.com/v1/")
    monkeypatch.setenv("NVIDIA_LLM_API_KEY", "sk-fake")
    client = build_openai_client(
        base_url="https://from-yaml.example.com/v1",
        base_url_env="NVIDIA_BASE_URL",
        api_key_env="NVIDIA_LLM_API_KEY",
    )
    # OpenAI SDK exposes the resolved base URL on `_base_url` (private but stable).
    assert "from-env.example.com" in str(client.base_url)


def test_base_url_falls_back_to_literal_when_env_unset(monkeypatch) -> None:
    """If the env-var named by base_url_env is empty/unset, the literal wins."""
    from file_to_jira.enrich.agent_openai import build_openai_client

    monkeypatch.delenv("NVIDIA_BASE_URL", raising=False)
    monkeypatch.setenv("NVIDIA_LLM_API_KEY", "sk-fake")
    client = build_openai_client(
        base_url="https://from-yaml.example.com/v1",
        base_url_env="NVIDIA_BASE_URL",
        api_key_env="NVIDIA_LLM_API_KEY",
    )
    assert "from-yaml.example.com" in str(client.base_url)


def test_tools_array_uses_openai_shape(
    toolkit: Toolkit, parsed_bug: ParsedBug, upstream_repo: Path
) -> None:
    submit_tool = build_submit_tool({"sample": upstream_repo})
    fake = FakeOpenAI(
        [tool_call_turn(("t1", "submit_enrichment", _enriched_payload()))]
    )
    agent = OpenAIEnrichmentAgent(
        toolkit=toolkit,
        submit_tool=submit_tool,
        client=fake,
        system_prompt="(test prompt)",
        max_turns=2,
    )
    agent.enrich(parsed_bug)
    first = fake.calls[0]
    assert "tools" in first
    assert all(t["type"] == "function" for t in first["tools"])
    assert all("function" in t for t in first["tools"])
