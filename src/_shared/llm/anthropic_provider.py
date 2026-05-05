"""AnthropicProvider — wraps the anthropic SDK behind the LLMProvider protocol.

Currently dormant operationally (Sharon's prod path is OpenAI-compat against
NVIDIA Inference). Kept as a second concrete impl so the LLMProvider ABC is
validated against a genuinely different SDK shape.

Translates between the unified OpenAI-shaped tool definitions/messages and
the Anthropic Messages API. ``cache_control: ephemeral`` annotations on the
last tool (a prompt-cache optimization) are preserved when present.
"""
from __future__ import annotations

import json
import os
from typing import Any

from .base import ChatResponse, ChatWithToolsResponse, ToolCall


class AnthropicProvider:
    """Anthropic Messages API provider."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_env: str | None = None,
        base_url: str | None = None,
        base_url_env: str | None = None,
        client: Any | None = None,
    ) -> None:
        if client is not None:
            self._client = client
            return

        # Lazy import so the OpenAI-only deployment doesn't require anthropic.
        from anthropic import Anthropic

        resolved_key = api_key
        if not resolved_key and api_key_env:
            resolved_key = os.environ.get(api_key_env)
        if not resolved_key:
            resolved_key = os.environ.get("ANTHROPIC_API_KEY")

        resolved_url = base_url
        if base_url_env:
            env_url = os.environ.get(base_url_env)
            if env_url:
                resolved_url = env_url

        kwargs: dict[str, Any] = {}
        if resolved_key:
            kwargs["api_key"] = resolved_key
        if resolved_url:
            kwargs["base_url"] = resolved_url
        self._client = Anthropic(**kwargs)

    # ---- LLMProvider ----------------------------------------------------

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
        system, user_messages = _split_system(messages)
        params: dict[str, Any] = {
            "model": model,
            "messages": user_messages,
            "temperature": temperature,
            "max_tokens": max_tokens or 4096,
        }
        if system:
            params["system"] = system
        # Anthropic doesn't have a JSON-mode flag; if response_format requests
        # JSON, the caller must instruct via the system prompt. We accept the
        # flag silently for protocol compatibility.
        params.update({k: v for k, v in kwargs.items() if k != "response_format"})

        resp = self._client.messages.create(**params)
        content = _join_text_blocks(resp.content)
        return ChatResponse(content=content, usage=_extract_usage(resp), model=model, raw=resp)

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
        system, user_messages = _split_system(messages)
        params: dict[str, Any] = {
            "model": model,
            "messages": _to_anthropic_messages(user_messages),
            "tools": _openai_tools_to_anthropic(tools),
            "temperature": temperature,
            "max_tokens": max_tokens or 4096,
        }
        if system:
            params["system"] = system
        if tool_choice and tool_choice != "auto":
            # Anthropic supports {"type": "any"} or {"type": "tool", "name": "X"}; pass through if explicit.
            params["tool_choice"] = (
                {"type": tool_choice} if isinstance(tool_choice, str) else tool_choice
            )
        params.update(kwargs)

        resp = self._client.messages.create(**params)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", ""))
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=json.dumps(block.input or {}),
                    )
                )

        finish_reason = _normalize_stop_reason(getattr(resp, "stop_reason", None), tool_calls)
        return ChatWithToolsResponse(
            content="".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=_extract_usage(resp),
            model=model,
            raw=resp,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split_system(
    messages: list[dict[str, Any]],
) -> tuple[str | list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """Pull all ``role: system`` messages into a single Anthropic ``system`` value.

    Anthropic Messages API takes ``system`` as a top-level parameter; messages
    must alternate user/assistant.

    Return shape:
    - ``None`` if no system content
    - ``str`` if all system messages are plain text (most callers)
    - ``list[dict]`` of content blocks if any block carries metadata such as
      ``cache_control: ephemeral`` (preserves prompt-cache hints from f2j's
      Anthropic agent)
    """
    blocks: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    has_metadata = False

    for m in messages:
        if m.get("role") != "system":
            rest.append(m)
            continue
        added_metadata = _collect_system_blocks(m.get("content"), blocks)
        has_metadata = has_metadata or added_metadata

    if not blocks:
        return None, rest
    if has_metadata:
        return blocks, rest
    return "\n\n".join(b.get("text", "") for b in blocks if b.get("text")), rest


def _collect_system_blocks(content: Any, blocks: list[dict[str, Any]]) -> bool:
    """Append text blocks for one system message's ``content`` into ``blocks``.

    Returns True if any appended block carried metadata (e.g. ``cache_control``).
    """
    if isinstance(content, str):
        if content:
            blocks.append({"type": "text", "text": content})
        return False
    if isinstance(content, list):
        return any(_append_block(b, blocks) for b in content)
    return False


def _append_block(b: Any, blocks: list[dict[str, Any]]) -> bool:
    """Append one block-or-string to ``blocks``. Returns True if it carried metadata."""
    if isinstance(b, dict):
        blocks.append(b)
        return any(k not in ("type", "text") for k in b)
    if isinstance(b, str) and b:
        blocks.append({"type": "text", "text": b})
    return False


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate OpenAI-shaped messages to Anthropic shape.

    OpenAI:
        - ``{"role": "tool", "tool_call_id": "...", "content": "..."}``
        - assistant tool_calls embedded in ``message.tool_calls``
    Anthropic:
        - tool results live in a user message with content blocks of type ``tool_result``
        - assistant tool calls are content blocks of type ``tool_use``
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            out.append(_tool_message_to_anthropic(m))
        elif role == "assistant" and m.get("tool_calls"):
            out.append(_assistant_with_tool_calls_to_anthropic(m))
        else:
            out.append({"role": role, "content": m.get("content", "")})
    return out


def _tool_message_to_anthropic(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": m.get("content", ""),
            }
        ],
    }


def _assistant_with_tool_calls_to_anthropic(m: dict[str, Any]) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    text = m.get("content")
    if isinstance(text, str) and text:
        blocks.append({"type": "text", "text": text})
    for tc in m["tool_calls"]:
        blocks.append(_tool_call_to_anthropic_block(tc))
    return {"role": "assistant", "content": blocks}


def _tool_call_to_anthropic_block(tc: dict[str, Any]) -> dict[str, Any]:
    fn = tc.get("function", {})
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except (json.JSONDecodeError, TypeError):
        args = {}
    return {
        "type": "tool_use",
        "id": tc.get("id", ""),
        "name": fn.get("name", ""),
        "input": args,
    }


def _openai_tools_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI ``tools`` shape → Anthropic ``tools`` shape.

    OpenAI: ``[{"type": "function", "function": {"name", "description", "parameters"}}]``
    Anthropic: ``[{"name", "description", "input_schema"}]``
    """
    out: list[dict[str, Any]] = []
    for t in tools:
        if t.get("type") == "function" and "function" in t:
            fn = t["function"]
            tool: dict[str, Any] = {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {}),
            }
            # If a cache_control hint was passed through, preserve it.
            if "cache_control" in t:
                tool["cache_control"] = t["cache_control"]
            out.append(tool)
        else:
            # Already Anthropic-shaped; pass through.
            out.append(t)
    return out


def _normalize_stop_reason(stop_reason: str | None, tool_calls: list[ToolCall]) -> str:
    """Map Anthropic ``stop_reason`` to the unified vocabulary.

    Anthropic: ``end_turn`` | ``tool_use`` | ``max_tokens`` | ``stop_sequence``
    Unified:   ``stop``     | ``tool_calls`` | ``length``    | ``stop``
    """
    if stop_reason == "tool_use" or tool_calls:
        return "tool_calls"
    if stop_reason == "max_tokens":
        return "length"
    if stop_reason in ("end_turn", "stop_sequence", None):
        return "stop"
    return stop_reason


def _join_text_blocks(content: Any) -> str:
    if isinstance(content, list):
        return "".join(
            getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text"
        )
    if isinstance(content, str):
        return content
    return ""


def _extract_usage(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    in_t = getattr(u, "input_tokens", 0) or 0
    out_t = getattr(u, "output_tokens", 0) or 0
    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
    cache_create = getattr(u, "cache_creation_input_tokens", 0) or 0
    out = {
        "prompt_tokens": in_t,
        "completion_tokens": out_t,
        "total_tokens": in_t + out_t,
    }
    # Anthropic-only — exposed when present so callers can populate
    # EnrichmentMeta.cache_read_tokens / cache_creation_tokens.
    if cache_read:
        out["cache_read_tokens"] = cache_read
    if cache_create:
        out["cache_creation_tokens"] = cache_create
    return out
