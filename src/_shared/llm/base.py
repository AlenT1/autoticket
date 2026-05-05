"""LLMProvider protocol + normalized response types."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    """One tool call requested by the assistant.

    ``arguments`` is always a JSON string (not a parsed dict). Anthropic
    natively returns a dict; the AnthropicProvider serializes it on the way
    out so f2j's existing ``json.loads(tc.function.arguments)`` continues to
    work unchanged.
    """

    id: str
    name: str
    arguments: str


@dataclass
class ChatResponse:
    """Normalized response from a JSON-mode single-shot ``chat`` call."""

    content: str
    """Raw text the assistant returned. JSON-mode callers should ``json.loads`` it."""

    usage: dict[str, int] = field(default_factory=dict)
    """At minimum: ``prompt_tokens``, ``completion_tokens``, ``total_tokens``."""

    model: str = ""
    """The model id that actually served this response."""

    raw: Any = None
    """Provider-native response object, for debugging / advanced inspection."""


@dataclass
class ChatWithToolsResponse:
    """Normalized response from one turn of a tool-use loop."""

    content: str | None
    """Assistant's text content, if any. May be None when only tool calls were emitted."""

    tool_calls: list[ToolCall]
    """Tool calls the assistant wants the caller to execute."""

    finish_reason: str
    """Normalized vocabulary:

    - ``"stop"``      : assistant completed without calling tools (terminal)
    - ``"tool_calls"``: assistant emitted tool calls (caller must dispatch)
    - ``"length"``    : ran out of tokens mid-response
    - other strings (``"content_filter"``, etc.) pass through as-is
    """

    usage: dict[str, int] = field(default_factory=dict)
    """At minimum: ``prompt_tokens``, ``completion_tokens``."""

    model: str = ""

    raw: Any = None


class LLMProvider(Protocol):
    """Two-method protocol covering JSON-mode and tool-use call shapes.

    Implementations must be thread-safe enough to be reused across
    concurrent requests at the level the bodies use them — ``chat()`` is
    called from drive's ThreadPoolExecutor, ``chat_with_tools()`` is called
    from f2j's per-bug agent sessions (one provider, multiple bugs).
    """

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """One-shot JSON-mode-friendly chat completion.

        ``response_format={"type": "json_object"}`` enables strict JSON mode
        (caller is still responsible for stripping any code fences and
        ``json.loads``-ing the content — drive's existing helpers do this).

        ``messages`` follows the OpenAI message shape:
            ``[{"role": "system", "content": "..."}, {"role": "user", "content": "..."}, ...]``

        AnthropicProvider translates ``role: system`` into the SDK's
        ``system=`` parameter at call time.
        """
        ...

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]],
        tool_choice: str = "auto",
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> ChatWithToolsResponse:
        """One turn of a tool-use loop. Caller drives the loop.

        ``tools`` follows OpenAI's shape:
            ``[{"type": "function", "function": {"name", "description", "parameters"}}]``

        AnthropicProvider translates this to the Messages-API
        ``[{"name", "description", "input_schema"}]`` shape internally.

        After receiving the response:
        - if ``finish_reason == "tool_calls"``: the caller dispatches each
          ``ToolCall`` and appends an assistant + tool-result message pair
          for the next turn.
        - if ``finish_reason == "stop"``: assistant decided no tool was
          needed; caller decides whether that's a terminal success or
          truncation.
        """
        ...
