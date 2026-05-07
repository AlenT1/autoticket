"""OpenAICompatProvider — wraps the openai SDK against any OpenAI-protocol endpoint.

Used today by both bodies against NVIDIA Inference Hub.
"""
from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI

from .base import ChatResponse, ChatWithToolsResponse, ToolCall


class OpenAICompatProvider:
    """OpenAI-compatible chat provider.

    Resolution order for credentials when none are passed explicitly:
    1. ``api_key`` constructor arg (used by tests)
    2. The env var named by ``api_key_env``
    3. ``OPENAI_API_KEY`` (the SDK's default)

    Resolution order for the base URL:
    1. ``base_url`` constructor arg
    2. The env var named by ``base_url_env`` (overrides #1 if set)
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        base_url_env: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        client: Any | None = None,
    ) -> None:
        if client is not None:
            self._client = client
            return

        resolved_url = base_url
        if base_url_env:
            env_url = os.environ.get(base_url_env)
            if env_url:
                resolved_url = env_url

        resolved_key = api_key
        if not resolved_key and api_key_env:
            resolved_key = os.environ.get(api_key_env)
        if not resolved_key:
            resolved_key = os.environ.get("OPENAI_API_KEY")

        kwargs: dict[str, Any] = {}
        if resolved_url:
            kwargs["base_url"] = resolved_url
        if resolved_key:
            kwargs["api_key"] = resolved_key
        self._client = OpenAI(**kwargs)

    @classmethod
    def from_settings(cls, settings: Any) -> "OpenAICompatProvider":
        """Build a provider from a unified ``Settings`` object.

        Reads ``nvidia_api_key`` + ``nvidia_base_url`` (env: ``NVIDIA_API_KEY``
        / ``NVIDIA_BASE_URL``). One canonical name per field; legacy names
        like ``OPENAI_API_KEY`` are not consulted by Settings.
        """
        if not settings.nvidia_api_key:
            raise RuntimeError(
                "No LLM API key resolvable from settings. Set NVIDIA_API_KEY "
                "in .env."
            )
        return cls(
            api_key=settings.nvidia_api_key,
            base_url=settings.nvidia_base_url,
        )

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
        params: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        if response_format is not None:
            params["response_format"] = response_format
        params.update(kwargs)

        resp = self._client.chat.completions.create(**params)
        content = resp.choices[0].message.content or ""
        usage = _extract_usage(resp)
        return ChatResponse(content=content, usage=usage, model=model, raw=resp)

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
        params: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "temperature": temperature,
        }
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        params.update(kwargs)

        resp = self._client.chat.completions.create(**params)
        choice = resp.choices[0]
        msg = choice.message
        finish_reason = choice.finish_reason or ""

        tool_calls: list[ToolCall] = []
        for tc in (msg.tool_calls or []):
            tool_calls.append(
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=tc.function.arguments,
                )
            )

        return ChatWithToolsResponse(
            content=getattr(msg, "content", None),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=_extract_usage(resp),
            model=model,
            raw=resp,
        )


def _extract_usage(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    return {
        "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
        "total_tokens": getattr(u, "total_tokens", 0) or 0,
    }
