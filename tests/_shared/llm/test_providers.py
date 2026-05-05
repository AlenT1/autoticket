"""Contract tests for _shared.llm providers using fake SDK clients."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from _shared.llm import (
    AnthropicProvider,
    ChatResponse,
    ChatWithToolsResponse,
    OpenAICompatProvider,
    ToolCall,
    get_provider,
)


# ===========================================================================
# Fake OpenAI client (matches the openai SDK response shape)
# ===========================================================================


@dataclass
class FakeOAIFunction:
    name: str
    arguments: str  # JSON-encoded


@dataclass
class FakeOAIToolCall:
    id: str
    function: FakeOAIFunction
    type: str = "function"


@dataclass
class FakeOAIMessage:
    content: str | None = None
    tool_calls: list[FakeOAIToolCall] | None = None


@dataclass
class FakeOAIChoice:
    message: FakeOAIMessage
    finish_reason: str
    index: int = 0


@dataclass
class FakeOAIUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class FakeOAICompletion:
    choices: list[FakeOAIChoice]
    usage: FakeOAIUsage = field(default_factory=FakeOAIUsage)


class _FakeOAICompletions:
    def __init__(self, parent: "FakeOAI") -> None:
        self.parent = parent

    def create(self, **kwargs: Any) -> FakeOAICompletion:
        self.parent.calls.append(kwargs)
        return self.parent.responses.pop(0)


class _FakeOAIChat:
    def __init__(self, parent: "FakeOAI") -> None:
        self.completions = _FakeOAICompletions(parent)


class FakeOAI:
    def __init__(self, responses: list[FakeOAICompletion]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []
        self.chat = _FakeOAIChat(self)


# ===========================================================================
# Fake Anthropic client (matches the anthropic SDK Messages-API response shape)
# ===========================================================================


@dataclass
class FakeAnthBlock:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeAnthUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class FakeAnthMessage:
    content: list[FakeAnthBlock]
    stop_reason: str
    usage: FakeAnthUsage = field(default_factory=FakeAnthUsage)


class _FakeAnthMessages:
    def __init__(self, parent: "FakeAnth") -> None:
        self.parent = parent

    def create(self, **kwargs: Any) -> FakeAnthMessage:
        self.parent.calls.append(kwargs)
        return self.parent.responses.pop(0)


class FakeAnth:
    def __init__(self, responses: list[FakeAnthMessage]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []
        self.messages = _FakeAnthMessages(self)


# ===========================================================================
# OpenAICompatProvider.chat — JSON-mode single shot
# ===========================================================================


def test_openai_compat_chat_json_mode():
    fake = FakeOAI(
        [
            FakeOAICompletion(
                choices=[
                    FakeOAIChoice(
                        message=FakeOAIMessage(content='{"role": "single_epic"}'),
                        finish_reason="stop",
                    )
                ],
                usage=FakeOAIUsage(prompt_tokens=42, completion_tokens=11, total_tokens=53),
            )
        ]
    )
    provider = OpenAICompatProvider(client=fake)

    resp = provider.chat(
        messages=[
            {"role": "system", "content": "You are a classifier."},
            {"role": "user", "content": "Doc text..."},
        ],
        model="meta/llama-3.1-70b-instruct",
        response_format={"type": "json_object"},
        temperature=0.1,
    )

    assert isinstance(resp, ChatResponse)
    assert resp.content == '{"role": "single_epic"}'
    assert resp.usage == {"prompt_tokens": 42, "completion_tokens": 11, "total_tokens": 53}
    assert resp.model == "meta/llama-3.1-70b-instruct"

    # Verify the SDK was called with the right shape
    call = fake.calls[0]
    assert call["model"] == "meta/llama-3.1-70b-instruct"
    assert call["response_format"] == {"type": "json_object"}
    assert call["temperature"] == 0.1
    assert call["messages"][0]["role"] == "system"


# ===========================================================================
# OpenAICompatProvider.chat_with_tools — single tool-use turn
# ===========================================================================


def test_openai_compat_chat_with_tools_normalizes_tool_calls():
    fake = FakeOAI(
        [
            FakeOAICompletion(
                choices=[
                    FakeOAIChoice(
                        message=FakeOAIMessage(
                            content=None,
                            tool_calls=[
                                FakeOAIToolCall(
                                    id="call_abc",
                                    function=FakeOAIFunction(
                                        name="search_code",
                                        arguments='{"query": "foo"}',
                                    ),
                                )
                            ],
                        ),
                        finish_reason="tool_calls",
                    )
                ],
                usage=FakeOAIUsage(prompt_tokens=100, completion_tokens=50),
            )
        ]
    )
    provider = OpenAICompatProvider(client=fake)

    resp = provider.chat_with_tools(
        messages=[{"role": "user", "content": "find foo"}],
        model="gpt-4o",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "search_code",
                    "description": "ripgrep",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )

    assert isinstance(resp, ChatWithToolsResponse)
    assert resp.finish_reason == "tool_calls"
    assert resp.content is None
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert isinstance(tc, ToolCall)
    assert tc.id == "call_abc"
    assert tc.name == "search_code"
    assert tc.arguments == '{"query": "foo"}'  # string, not dict


def test_openai_compat_stop_finish_no_tool_calls():
    fake = FakeOAI(
        [
            FakeOAICompletion(
                choices=[
                    FakeOAIChoice(
                        message=FakeOAIMessage(content="all done"),
                        finish_reason="stop",
                    )
                ],
            )
        ]
    )
    provider = OpenAICompatProvider(client=fake)
    resp = provider.chat_with_tools(
        messages=[{"role": "user", "content": "hi"}], model="x", tools=[]
    )
    assert resp.finish_reason == "stop"
    assert resp.tool_calls == []
    assert resp.content == "all done"


# ===========================================================================
# AnthropicProvider — translation to/from Anthropic SDK shape
# ===========================================================================


def test_anthropic_chat_with_tools_translates_tool_use_block():
    fake = FakeAnth(
        [
            FakeAnthMessage(
                content=[
                    FakeAnthBlock(type="text", text="Let me look that up."),
                    FakeAnthBlock(
                        type="tool_use",
                        id="toolu_01",
                        name="search_code",
                        input={"query": "foo"},
                    ),
                ],
                stop_reason="tool_use",
                usage=FakeAnthUsage(input_tokens=100, output_tokens=20),
            )
        ]
    )
    provider = AnthropicProvider(client=fake)

    resp = provider.chat_with_tools(
        messages=[
            {"role": "system", "content": "You are an investigator."},
            {"role": "user", "content": "find foo"},
        ],
        model="claude-sonnet-4-6",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "search_code",
                    "description": "ripgrep",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )

    assert resp.finish_reason == "tool_calls"
    assert resp.content == "Let me look that up."
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "toolu_01"
    assert tc.name == "search_code"
    # Anthropic returns input as dict; provider normalizes to JSON string.
    assert json.loads(tc.arguments) == {"query": "foo"}

    # Verify SDK was called with Anthropic-shaped tools and split system
    call = fake.calls[0]
    assert call["system"] == "You are an investigator."
    # System message stripped from messages list
    assert call["messages"] == [{"role": "user", "content": "find foo"}]
    # Tools translated to Anthropic shape
    anth_tools = call["tools"]
    assert len(anth_tools) == 1
    assert anth_tools[0]["name"] == "search_code"
    assert anth_tools[0]["input_schema"] == {"type": "object"}


def test_anthropic_translates_tool_result_message():
    """Caller sends OpenAI-shaped tool result; provider translates to Anthropic shape."""
    fake = FakeAnth(
        [
            FakeAnthMessage(
                content=[FakeAnthBlock(type="text", text="ok")],
                stop_reason="end_turn",
                usage=FakeAnthUsage(input_tokens=50, output_tokens=5),
            )
        ]
    )
    provider = AnthropicProvider(client=fake)
    provider.chat_with_tools(
        messages=[
            {"role": "user", "content": "find foo"},
            {
                "role": "assistant",
                "content": "Looking now.",
                "tool_calls": [
                    {
                        "id": "toolu_01",
                        "type": "function",
                        "function": {
                            "name": "search_code",
                            "arguments": '{"query": "foo"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_01",
                "content": '{"matches": []}',
            },
        ],
        model="claude-sonnet-4-6",
        tools=[],
    )

    sent_messages = fake.calls[0]["messages"]
    # First: user (passthrough)
    assert sent_messages[0]["role"] == "user"
    assert sent_messages[0]["content"] == "find foo"
    # Second: assistant with text + tool_use blocks
    assert sent_messages[1]["role"] == "assistant"
    assert sent_messages[1]["content"][0] == {"type": "text", "text": "Looking now."}
    assert sent_messages[1]["content"][1]["type"] == "tool_use"
    assert sent_messages[1]["content"][1]["name"] == "search_code"
    assert sent_messages[1]["content"][1]["input"] == {"query": "foo"}
    # Third: user message with tool_result block
    assert sent_messages[2]["role"] == "user"
    assert sent_messages[2]["content"][0]["type"] == "tool_result"
    assert sent_messages[2]["content"][0]["tool_use_id"] == "toolu_01"


def test_anthropic_normalizes_stop_reasons():
    cases = [
        ("end_turn", [], "stop"),
        ("max_tokens", [], "length"),
        ("stop_sequence", [], "stop"),
        # Even with stop_reason=None, presence of tool calls forces "tool_calls"
        (None, [FakeAnthBlock(type="tool_use", id="t1", name="x", input={})], "tool_calls"),
    ]
    for stop_reason, content, expected in cases:
        fake = FakeAnth(
            [FakeAnthMessage(content=content, stop_reason=stop_reason)]  # type: ignore[arg-type]
        )
        provider = AnthropicProvider(client=fake)
        resp = provider.chat_with_tools(
            messages=[{"role": "user", "content": "x"}], model="m", tools=[]
        )
        assert resp.finish_reason == expected, f"{stop_reason} → {expected}"


# ===========================================================================
# Registry
# ===========================================================================


def test_get_provider_routes_to_openai_compat():
    p = get_provider("openai_compatible", api_key="x")
    assert isinstance(p, OpenAICompatProvider)


def test_get_provider_routes_to_anthropic():
    # Use a fake client to avoid SDK auth at construction
    fake = FakeAnth([])
    p = get_provider("anthropic", client=fake)
    assert isinstance(p, AnthropicProvider)


def test_get_provider_aliases():
    assert isinstance(get_provider("openai", api_key="x"), OpenAICompatProvider)
    assert isinstance(get_provider("nvidia", api_key="x"), OpenAICompatProvider)


def test_get_provider_unknown_name_raises():
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        get_provider("bedrock")
