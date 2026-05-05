"""Shared LLM provider layer.

Two methods cover both bodies' needs:

- ``chat(messages, response_format=…)`` — JSON-mode single shot (used by
  jira_task_agent's pipeline calls; classify, extract, match, summarize).

- ``chat_with_tools(messages, tools, tool_choice=…)`` — one turn of an
  agentic loop (used by file_to_jira's enrichment agent). The agent loop
  itself stays in f2j's body; the provider only abstracts the SDK call.

Concrete impls:
- :class:`OpenAICompatProvider` — wraps the ``openai`` SDK against any
  OpenAI-protocol endpoint (NVIDIA Inference Hub, Azure OpenAI, vLLM, ...).
- :class:`AnthropicProvider` — wraps the ``anthropic`` SDK. Currently dormant
  operationally (Sharon's prod path is OpenAI-compat against NVIDIA), kept
  as a second impl that validates the ABC works with a different SDK.

Lookup by name via :func:`get_provider`.
"""
from .base import (
    ChatResponse,
    ChatWithToolsResponse,
    LLMProvider,
    ToolCall,
)
from .openai_compat import OpenAICompatProvider
from .anthropic_provider import AnthropicProvider
from .registry import get_provider

__all__ = [
    "LLMProvider",
    "ChatResponse",
    "ChatWithToolsResponse",
    "ToolCall",
    "OpenAICompatProvider",
    "AnthropicProvider",
    "get_provider",
]
