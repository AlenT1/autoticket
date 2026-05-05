"""OpenAI-compatible tool-use agent loop, one session per bug.

Drives one bug end-to-end: clones repo, browses code, blames, submits
the structured ``EnrichedBug`` via ``submit_enrichment``. The actual SDK
call is delegated to :class:`_shared.llm.LLMProvider` so this module
owns only loop semantics — when to stop, how to interpret submit results,
how to account tokens — not provider/auth plumbing.

The Anthropic agent and this one share:
- the system prompt (versioned in ``prompts/enrichment_system.md``)
- the toolkit (``Toolkit`` methods become tools)
- the ``submit_enrichment`` schema validator
- the ``EnrichmentMeta`` accounting

Backward compat: tests that pass a raw fake-OpenAI client via ``client=``
are auto-wrapped into an :class:`OpenAICompatProvider`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from _shared.llm import (
    ChatWithToolsResponse,
    LLMProvider,
    OpenAICompatProvider,
    ToolCall,
)

from ..models import EnrichedBug, EnrichmentMeta, ParsedBug
from .agent import (
    EnrichmentError,
    EnrichmentTruncated,
    build_openai_tool_registry,
    format_initial_prompt,
    load_system_prompt,
    system_prompt_hash,
)
from .tools import ToolError, Toolkit

# Re-export for tests that import build_openai_tool_registry from here.
__all__ = [
    "OpenAIEnrichmentAgent",
    "build_openai_tool_registry",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_MAX_TOKENS_PER_TURN",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_MODEL",
]

DEFAULT_MAX_TURNS = 20
DEFAULT_MAX_TOKENS_PER_TURN = 4096
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MODEL = "gpt-4o"

log = logging.getLogger(__name__)


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
        provider: LLMProvider | None = None,
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
        self._provider = _resolve_openai_provider(
            provider=provider,
            client=client,
            base_url=base_url,
            base_url_env=base_url_env,
            api_key_env=api_key_env,
        )
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
            resp = self._provider.chat_with_tools(
                messages=messages,
                model=self.model,
                tools=self.tools,
                tool_choice="auto",
                temperature=self.temperature,
                max_tokens=self.max_tokens_per_turn,
            )
            self._account_usage(usage, resp.usage)
            tool_calls = self._extract_tool_calls(resp, turn, messages)

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
        self,
        resp: ChatWithToolsResponse,
        turn: int,
        messages: list[dict[str, Any]],
    ) -> list[ToolCall]:
        """Validate the response and return its tool_calls. Raises on bad shape."""
        finish_reason = resp.finish_reason

        if finish_reason == "stop":
            raise EnrichmentTruncated(
                f"agent ended turn without calling submit_enrichment "
                f"after {turn + 1} turn(s)"
            )
        if finish_reason not in {"tool_calls", "length"}:
            raise EnrichmentError(
                f"unexpected finish_reason {finish_reason!r} on turn {turn + 1}"
            )

        if not resp.tool_calls:
            raise EnrichmentError(
                f"finish_reason={finish_reason} but no tool_calls present"
            )

        messages.append(_assistant_message_with_tool_calls(resp))
        return resp.tool_calls

    def _run_tool_calls(
        self,
        tool_calls: list[ToolCall],
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
                continue
            if tc.name == "submit_enrichment":
                submitted = self._handle_submit(tc, args, messages, submitted)
            else:
                self._handle_tool_call(tc, args, messages, repos_touched)
        return submitted

    # ----- helpers --------------------------------------------------------

    def _handle_submit(
        self,
        tc: ToolCall,
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
        tc: ToolCall,
        args: dict[str, Any],
        messages: list[dict[str, Any]],
        repos_touched: set[str],
    ) -> None:
        result, repo_alias = self._dispatch_tool(tc.name, args)
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

    def _account_usage(self, usage: _Usage, u: dict[str, int]) -> None:
        if not u:
            return
        usage.input_tokens += u.get("prompt_tokens", 0) or 0
        usage.output_tokens += u.get("completion_tokens", 0) or 0

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

def _resolve_openai_provider(
    *,
    provider: LLMProvider | None,
    client: Any | None,
    base_url: str | None,
    base_url_env: str | None,
    api_key_env: str | None,
) -> LLMProvider:
    """Pick a provider: explicit > raw client (back-compat) > env-derived default."""
    if provider is not None:
        return provider
    if client is not None:
        return OpenAICompatProvider(client=client)
    return OpenAICompatProvider(
        base_url=base_url,
        base_url_env=base_url_env,
        api_key_env=api_key_env,
    )


def _assistant_message_with_tool_calls(resp: ChatWithToolsResponse) -> dict[str, Any]:
    """OpenAI-shape assistant message preserving tool_calls for next turn."""
    return {
        "role": "assistant",
        "content": resp.content,
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in resp.tool_calls
        ],
    }


def _parse_tool_arguments(
    tc: ToolCall, messages: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Parse the tool-call argument JSON; on failure, append a tool_result error."""
    try:
        return json.loads(tc.arguments) if tc.arguments else {}
    except json.JSONDecodeError as e:
        messages.append(_tool_result(tc.id, {"error": f"invalid JSON args: {e}"}))
        return None


def _tool_result(tool_call_id: str, result: Any) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(result, default=str),
    }
