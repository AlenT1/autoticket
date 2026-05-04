"""OpenAI-compatible tool-use agent loop, one session per bug.

Mirrors the Anthropic agent but speaks OpenAI Chat Completions protocol so
this tool can run against any compatible endpoint — NVIDIA Inference Hub,
Azure OpenAI, vLLM, LocalAI, etc. — without code change beyond config.

The Anthropic agent and this one share:
- the same system prompt (versioned in `prompts/enrichment_system.md`)
- the same toolkit (`Toolkit` methods become tools)
- the same `submit_enrichment` schema validator
- the same `EnrichmentMeta` accounting

What differs:
- request/response shape (Chat Completions vs Messages)
- tool format (`tools=[{"type":"function","function":{...}}]` vs `tools=[{"name":...}]`)
- finish-reason vocabulary (`stop`/`tool_calls`/`length` vs `end_turn`/`tool_use`)
- no native prompt caching (some endpoints support it via headers; out of scope here)
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from openai import OpenAI
from pydantic import ValidationError

from ..models import EnrichedBug, EnrichmentMeta, ParsedBug
from .agent import (
    EnrichmentError,
    EnrichmentTruncated,
    build_tool_registry as _anthropic_tool_registry,
    format_initial_prompt,
    load_system_prompt,
    system_prompt_hash,
)
from .tools import ToolError, Toolkit

DEFAULT_MAX_TURNS = 20
DEFAULT_MAX_TOKENS_PER_TURN = 4096
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MODEL = "gpt-4o"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool registry: convert Anthropic shape → OpenAI Chat Completions shape
# ---------------------------------------------------------------------------

def build_openai_tool_registry() -> list[dict[str, Any]]:
    """Translate the Anthropic tool list into OpenAI's `tools` array.

    Anthropic shape:  ``{"name": "...", "description": "...", "input_schema": {...}}``
    OpenAI shape:     ``{"type": "function", "function": {"name", "description", "parameters"}}``

    The Anthropic-only ``cache_control`` field on the last tool is dropped —
    OpenAI-compatible endpoints don't speak ephemeral cache control.
    """
    out: list[dict[str, Any]] = []
    for tool in _anthropic_tool_registry():
        out.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                },
            }
        )
    return out


# ---------------------------------------------------------------------------
# Client builder
# ---------------------------------------------------------------------------

def build_openai_client(
    *,
    base_url: str,
    api_key_env: str,
    base_url_env: str | None = None,
    api_key: str | None = None,
) -> OpenAI:
    """Construct an OpenAI client.

    Resolution order for the **base URL**:
    1. Env var named by ``base_url_env`` (if set and non-empty).
    2. The ``base_url`` literal arg.

    Resolution order for the **bearer token**:
    1. Explicit ``api_key`` arg (used by tests).
    2. The env var named by ``api_key_env`` (e.g. ``NVIDIA_LLM_API_KEY``).
    3. The default ``OPENAI_API_KEY`` env var (the SDK falls back to this).

    Raises a clear ``EnrichmentError`` if no token is resolvable.
    """
    resolved_url = base_url
    if base_url_env:
        env_url = os.environ.get(base_url_env)
        if env_url:
            resolved_url = env_url

    resolved_key = api_key or os.environ.get(api_key_env)
    if not resolved_key and not os.environ.get("OPENAI_API_KEY"):
        raise EnrichmentError(
            f"OpenAI-compatible auth: env var {api_key_env!r} not set "
            "(and no fallback OPENAI_API_KEY found)."
        )
    return OpenAI(
        base_url=resolved_url,
        api_key=resolved_key or os.environ.get("OPENAI_API_KEY"),
    )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0


class OpenAIEnrichmentAgent:
    def __init__(
        self,
        toolkit: Toolkit,
        submit_tool: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        client: Any | None = None,
        model: str = DEFAULT_MODEL,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_tokens_per_turn: int = DEFAULT_MAX_TOKENS_PER_TURN,
        temperature: float = DEFAULT_TEMPERATURE,
        system_prompt: str | None = None,
        base_url: str | None = None,
        base_url_env: str | None = None,
        api_key_env: str | None = None,
        available_epics: list[Any] | None = None,
    ) -> None:
        self.toolkit = toolkit
        self.submit_tool = submit_tool
        if client is not None:
            self._client = client
        elif base_url and api_key_env:
            self._client = build_openai_client(
                base_url=base_url,
                base_url_env=base_url_env,
                api_key_env=api_key_env,
            )
        else:
            self._client = OpenAI()  # SDK reads OPENAI_API_KEY/OPENAI_BASE_URL

        self.model = model
        self.max_turns = max_turns
        self.max_tokens_per_turn = max_tokens_per_turn
        self.temperature = temperature
        self.system_prompt = system_prompt or load_system_prompt()
        self.system_prompt_hash = system_prompt_hash(self.system_prompt)
        self.available_epics = available_epics
        self.tools = build_openai_tool_registry()

    def enrich(self, bug: ParsedBug) -> EnrichedBug:
        """Run one bug through the OpenAI tool-use loop. Raises on failure."""
        usage = _Usage()
        started = datetime.now(timezone.utc)
        repos_touched: set[str] = set()

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": format_initial_prompt(bug, self.available_epics)},
        ]

        for turn in range(self.max_turns):
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.tools,
                tool_choice="auto",
                temperature=self.temperature,
                max_tokens=self.max_tokens_per_turn,
            )
            self._account_usage(usage, response)
            tool_calls = self._extract_tool_calls(response, turn, messages)

            submitted = self._run_tool_calls(
                tool_calls, messages, usage, repos_touched
            )
            if submitted is not None:
                submitted.enrichment_meta = self._build_meta(
                    started, usage, repos_touched, truncated=False
                )
                return submitted

        raise EnrichmentTruncated(
            f"hit max_turns ({self.max_turns}) without successful submit_enrichment"
        )

    def _extract_tool_calls(
        self, response: Any, turn: int, messages: list[dict[str, Any]]
    ) -> list[Any]:
        """Validate the response and return its tool_calls. Raises on bad shape."""
        choice = response.choices[0]
        msg = choice.message
        finish_reason = choice.finish_reason

        if finish_reason == "stop":
            raise EnrichmentTruncated(
                f"agent ended turn without calling submit_enrichment "
                f"after {turn + 1} turn(s)"
            )
        if finish_reason not in {"tool_calls", "length"}:
            raise EnrichmentError(
                f"unexpected finish_reason {finish_reason!r} on turn {turn + 1}"
            )

        tool_calls = list(msg.tool_calls or [])
        if not tool_calls:
            raise EnrichmentError(
                f"finish_reason={finish_reason} but no tool_calls present"
            )

        messages.append(_assistant_message_with_tool_calls(msg, tool_calls))
        return tool_calls

    def _run_tool_calls(
        self,
        tool_calls: list[Any],
        messages: list[dict[str, Any]],
        usage: _Usage,
        repos_touched: set[str],
    ) -> EnrichedBug | None:
        """Dispatch each tool call. Returns the EnrichedBug iff submit_enrichment succeeded."""
        submitted: EnrichedBug | None = None
        for tc in tool_calls:
            usage.tool_calls += 1
            args = _parse_tool_arguments(tc, messages)
            if args is None:
                continue  # invalid JSON; tool result already appended
            if tc.function.name == "submit_enrichment":
                submitted = self._handle_submit(tc, args, messages, submitted)
            else:
                self._handle_tool_call(tc, args, messages, repos_touched)
        return submitted

    # ----- helpers --------------------------------------------------------

    def _handle_submit(
        self,
        tc: Any,
        args: dict[str, Any],
        messages: list[dict[str, Any]],
        submitted: EnrichedBug | None,
    ) -> EnrichedBug | None:
        result = self.submit_tool(args)
        messages.append(_tool_result(tc.id, result))
        if not result.get("ok"):
            return submitted
        try:
            return EnrichedBug.model_validate(result["enriched"])
        except ValidationError as e:
            raise EnrichmentError(
                f"submit_tool returned ok but EnrichedBug failed: {e}"
            ) from e

    def _handle_tool_call(
        self,
        tc: Any,
        args: dict[str, Any],
        messages: list[dict[str, Any]],
        repos_touched: set[str],
    ) -> None:
        result, repo_alias = self._dispatch_tool(tc.function.name, args)
        if repo_alias:
            repos_touched.add(repo_alias)
        messages.append(_tool_result(tc.id, result))

    def _dispatch_tool(self, name: str, args: dict[str, Any]) -> tuple[Any, str | None]:
        method = getattr(self.toolkit, name, None)
        if method is None:
            return {"error": f"unknown tool {name!r}"}, None
        repo_alias = args.get("repo_alias") if isinstance(args, dict) else None
        try:
            return method(**args), repo_alias
        except ToolError as e:
            return {"error": str(e)}, repo_alias
        except TypeError as e:
            return {"error": f"bad arguments for {name}: {e}"}, repo_alias

    def _account_usage(self, usage: _Usage, response: Any) -> None:
        u = getattr(response, "usage", None)
        if u is None:
            return
        usage.input_tokens += getattr(u, "prompt_tokens", 0) or 0
        usage.output_tokens += getattr(u, "completion_tokens", 0) or 0

    def _build_meta(
        self,
        started: datetime,
        usage: _Usage,
        repos_touched: set[str],
        *,
        truncated: bool,
    ) -> EnrichmentMeta:
        return EnrichmentMeta(
            model=self.model,
            started_at=started.isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            tool_calls=usage.tool_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            repos_touched=sorted(repos_touched),
            truncated=truncated,
            prompt_hash=self.system_prompt_hash,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _assistant_message_with_tool_calls(
    msg: Any, tool_calls: list[Any]
) -> dict[str, Any]:
    """Turn the SDK's Message object into a JSON-serializable dict for the next turn."""
    return {
        "role": "assistant",
        "content": getattr(msg, "content", None),
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in tool_calls
        ],
    }


def _parse_tool_arguments(tc: Any, messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Parse the tool-call argument JSON; on failure, append a tool_result error."""
    try:
        return json.loads(tc.function.arguments)
    except json.JSONDecodeError as e:
        messages.append(_tool_result(tc.id, {"error": f"invalid JSON args: {e}"}))
        return None


def _tool_result(tool_call_id: str, result: Any) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(result, default=str),
    }
