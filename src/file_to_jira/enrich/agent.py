"""Anthropic tool-use agent loop, one session per bug.

Routes through :class:`_shared.llm.LLMProvider` (specifically
:class:`AnthropicProvider`) for the SDK call. Operationally **dormant** —
Sharon's prod path is OpenAI-compat against NVIDIA — but kept as a genuine
alternate impl that exercises the LLMProvider ABC against a different SDK
shape. This validates that the abstraction generalizes.

What stays the same as before the LLMProvider refactor:
- ``submit_enrichment`` validation/retry loop fully under our control
- prompt-caching boundaries explicit (``cache_control: ephemeral`` on the
  system block + last tool); preserved through the provider
- token accounting tied to ``EnrichmentMeta`` per bug, including
  ``cache_read_tokens`` / ``cache_creation_tokens`` from Anthropic usage
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from _shared.llm import (
    AnthropicProvider,
    ChatWithToolsResponse,
    LLMProvider,
    ToolCall,
)

from ..models import EnrichedBug, EnrichmentMeta, ParsedBug
from .tools import ToolError, Toolkit

DEFAULT_MAX_TURNS = 20
DEFAULT_MAX_TOKENS_PER_TURN = 4096
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MODEL = "claude-sonnet-4-6"

log = logging.getLogger(__name__)


class EnrichmentError(Exception):
    """Base class for enrichment failures."""


class EnrichmentTruncated(EnrichmentError):
    """Agent ran out of turns before successfully calling submit_enrichment."""


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

# A permissive schema for submit_enrichment: the *real* validation happens in
# the build_submit_tool callable. Models occasionally wander outside narrow
# JSON Schemas, and we want to surface useful errors via the Pydantic path.
_SUBMIT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bug_id": {"type": "string"},
        "summary": {"type": "string", "maxLength": 255},
        "description_md": {"type": "string"},
        "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
        "assignee_hint": {"type": ["string", "null"]},
        "components": {"type": "array", "items": {"type": "string"}},
        "labels": {"type": "array", "items": {"type": "string"}},
        "epic_key": {
            "type": ["string", "null"],
            "description": "Jira key of the most relevant epic from the available_epics list provided in the user prompt. Null if none fits.",
        },
        "expected_behavior": {"type": ["string", "null"]},
        "actual_behavior": {"type": ["string", "null"]},
        "relevant_logs": {"type": ["string", "null"]},
        "reproduction_steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "order": {"type": "integer", "minimum": 1},
                    "text": {"type": "string"},
                },
                "required": ["order", "text"],
            },
        },
        "code_references": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "repo_alias": {"type": "string"},
                    "file_path": {"type": "string"},
                    "line_start": {"type": ["integer", "null"]},
                    "line_end": {"type": ["integer", "null"]},
                    "snippet": {"type": ["string", "null"]},
                    "blame_author": {"type": ["string", "null"]},
                    "blame_date": {"type": ["string", "null"]},
                    "commit_sha": {"type": ["string", "null"]},
                },
                "required": ["repo_alias", "file_path"],
            },
        },
    },
    "required": ["bug_id", "summary", "description_md", "priority"],
}


def build_tool_registry() -> list[dict[str, Any]]:
    """Tool definitions in Anthropic's tool-use schema. The last tool gets cache_control."""
    tools: list[dict[str, Any]] = [
        {
            "name": "clone_repo",
            "description": "Ensure a shallow clone of the named repo exists; return its info.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo_alias": {"type": "string"},
                    "ref": {"type": ["string", "null"]},
                },
                "required": ["repo_alias"],
            },
        },
        {
            "name": "search_code",
            "description": "Search the cached repo. Default mode is fixed-string; pass is_regex=true for regex.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo_alias": {"type": "string"},
                    "pattern": {"type": "string"},
                    "is_regex": {"type": "boolean"},
                    "file_glob": {"type": ["string", "null"]},
                    "case_insensitive": {"type": "boolean"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                    "context_lines": {"type": "integer", "minimum": 0, "maximum": 10},
                },
                "required": ["repo_alias", "pattern"],
            },
        },
        {
            "name": "read_file",
            "description": "Read a file (or line range) from the cached repo. POSIX-style relative path.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo_alias": {"type": "string"},
                    "file_path": {"type": "string"},
                    "line_start": {"type": ["integer", "null"], "minimum": 1},
                    "line_end": {"type": ["integer", "null"], "minimum": 1},
                    "max_bytes": {"type": "integer", "minimum": 1000, "maximum": 65000},
                },
                "required": ["repo_alias", "file_path"],
            },
        },
        {
            "name": "list_dir",
            "description": "List directory contents in the cached repo.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo_alias": {"type": "string"},
                    "dir_path": {"type": "string"},
                    "max_entries": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "required": ["repo_alias"],
            },
        },
        {
            "name": "git_blame",
            "description": "Show git blame for a line range. Use to identify recent changes to suspicious code.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo_alias": {"type": "string"},
                    "file_path": {"type": "string"},
                    "line_start": {"type": "integer", "minimum": 1},
                    "line_end": {"type": "integer", "minimum": 1},
                },
                "required": ["repo_alias", "file_path", "line_start", "line_end"],
            },
        },
        {
            "name": "git_log_for_path",
            "description": "Show recent commits that touched a single file.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo_alias": {"type": "string"},
                    "file_path": {"type": "string"},
                    "max_entries": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["repo_alias", "file_path"],
            },
        },
        {
            "name": "submit_enrichment",
            "description": (
                "FINAL ACTION: submit your structured bug report. The tool returns "
                "{ok: true} on success or {ok: false, errors: [...]} for self-correction. "
                "Call exactly once on success. Do not produce text after a successful call."
            ),
            "input_schema": _SUBMIT_INPUT_SCHEMA,
        },
    ]
    # Cache everything from the system prompt through the tool list.
    tools[-1]["cache_control"] = {"type": "ephemeral"}
    return tools


def build_openai_tool_registry() -> list[dict[str, Any]]:
    """OpenAI-shape tool registry, used uniformly via _shared.llm.LLMProvider.

    Translates the Anthropic-shaped registry returned by :func:`build_tool_registry`
    into OpenAI's ``tools=[{"type":"function","function":{...}}]`` shape.

    The ``cache_control: ephemeral`` annotation on the last tool is preserved
    at the outer dict level — :class:`AnthropicProvider` reads it back when
    converting to Anthropic shape on the wire, so prompt caching still works
    on the dormant Anthropic path.
    """
    out: list[dict[str, Any]] = []
    for tool in build_tool_registry():
        entry: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        if "cache_control" in tool:
            entry["cache_control"] = tool["cache_control"]
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Initial-prompt formatting
# ---------------------------------------------------------------------------

_INITIAL_TEMPLATE = """\
You are now enriching this bug. Read it, browse the repo as needed, then submit
your structured report via `submit_enrichment`.

## Bug record

- bug_id: {bug_id}
- external_id: {external_id}
- priority (hint): {priority}
- inherited repo: {repo_alias}
- inherited branch: {branch}
- inherited commit: {commit_sha}
- labels: {labels}
- hinted file paths: {hinted_files}
- hinted assignee: {assignee}

## Title
{title}

## Body (verbatim from source markdown)
```
{body}
```

## Available epics

Pick the single most appropriate epic from this list and include its key as
`epic_key` in your `submit_enrichment` payload. If none fit, set `epic_key`
to null.

{epic_list}

Begin by calling `clone_repo` for `{repo_alias}` if it has not been cloned yet,
then locate the code via `search_code`. Do not propose fixes.\
"""


def _format_epic_list(available_epics: list[Any] | None) -> str:
    """Render the available epics as a bulleted list for the prompt."""
    if not available_epics:
        return "(none configured — leave epic_key null)"
    lines: list[str] = []
    for entry in available_epics:
        key = getattr(entry, "key", None) or (entry.get("key") if isinstance(entry, dict) else None)
        summary = getattr(entry, "summary", None) or (
            entry.get("summary") if isinstance(entry, dict) else None
        )
        if key:
            lines.append(f"- `{key}` — {summary or '(no summary)'}")
    return "\n".join(lines) if lines else "(none configured — leave epic_key null)"


def format_initial_prompt(
    bug: ParsedBug, available_epics: list[Any] | None = None
) -> str:
    return _INITIAL_TEMPLATE.format(
        bug_id=bug.bug_id,
        external_id=bug.external_id or "(none)",
        priority=bug.hinted_priority or "(none)",
        repo_alias=bug.inherited_module.repo_alias or "(none)",
        branch=bug.inherited_module.branch or "(none)",
        commit_sha=bug.inherited_module.commit_sha or "(none)",
        labels=", ".join(bug.labels) or "(none)",
        hinted_files=", ".join(bug.hinted_files) or "(none)",
        assignee=bug.hinted_assignee or "(none)",
        title=bug.raw_title,
        body=bug.raw_body,
        epic_list=_format_epic_list(available_epics),
    )


# ---------------------------------------------------------------------------
# System-prompt loader
# ---------------------------------------------------------------------------

def load_system_prompt() -> str:
    """Read the versioned system prompt from `prompts/enrichment_system.md`."""
    here = Path(__file__).resolve().parent.parent.parent.parent
    path = here / "prompts" / "file_to_jira" / "enrichment_system.md"
    if not path.exists():
        # Back-compat with the pre-merge layout.
        path = here / "prompts" / "enrichment_system.md"
    return path.read_text(encoding="utf-8")


def system_prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    tool_calls: int = 0


class EnrichmentAgent:
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
        enable_prompt_caching: bool = True,
        base_url: str | None = None,
        auth_token_env: str | None = None,
        available_epics: list[Any] | None = None,
    ) -> None:
        self.toolkit = toolkit
        self.submit_tool = submit_tool
        self._provider = _resolve_anthropic_provider(
            provider=provider,
            client=client,
            base_url=base_url,
            auth_token_env=auth_token_env,
        )
        self.model = model
        self.max_turns = max_turns
        self.max_tokens_per_turn = max_tokens_per_turn
        self.temperature = temperature
        self.system_prompt = system_prompt or load_system_prompt()
        self.system_prompt_hash = system_prompt_hash(self.system_prompt)
        self.enable_prompt_caching = enable_prompt_caching
        self.available_epics = available_epics
        self.tools = build_openai_tool_registry()

    def enrich(self, bug: ParsedBug) -> EnrichedBug:
        """Run one bug through the agent loop. Raises EnrichmentError on failure."""
        usage = _Usage()
        started = datetime.now(timezone.utc)
        repos_touched: set[str] = set()

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_blocks()},
            {"role": "user", "content": format_initial_prompt(bug, self.available_epics)},
        ]

        for turn in range(self.max_turns):
            resp = self._provider.chat_with_tools(
                messages=messages,
                model=self.model,
                tools=self.tools,
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

    # ----- per-turn helpers ----------------------------------------------

    def _extract_tool_calls(
        self,
        resp: ChatWithToolsResponse,
        turn: int,
        messages: list[dict[str, Any]],
    ) -> list[ToolCall]:
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

    # ----- helpers --------------------------------------------------------

    def _system_blocks(self) -> list[dict[str, Any]]:
        """System content as a list of blocks so cache_control can ride along."""
        block: dict[str, Any] = {"type": "text", "text": self.system_prompt}
        if self.enable_prompt_caching:
            block["cache_control"] = {"type": "ephemeral"}
        return [block]

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
        usage.cache_read_tokens += u.get("cache_read_tokens", 0) or 0
        usage.cache_creation_tokens += u.get("cache_creation_tokens", 0) or 0

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
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            repos_touched=sorted(repos_touched),
            truncated=truncated,
            prompt_hash=self.system_prompt_hash,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _resolve_anthropic_provider(
    *,
    provider: LLMProvider | None,
    client: Any | None,
    base_url: str | None,
    auth_token_env: str | None,
) -> LLMProvider:
    """Pick a provider: explicit > raw client (back-compat) > env-derived default.

    Legacy ``auth_token_env`` is mapped onto :class:`AnthropicProvider`'s
    ``api_key_env`` — the Anthropic SDK accepts the same value via either
    parameter for proxy/MaaS deployments.
    """
    if provider is not None:
        return provider
    if client is not None:
        return AnthropicProvider(client=client)
    return AnthropicProvider(api_key_env=auth_token_env, base_url=base_url)


def _assistant_message_with_tool_calls(resp: ChatWithToolsResponse) -> dict[str, Any]:
    """OpenAI-shape assistant message; AnthropicProvider re-translates on the wire."""
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
    try:
        return json.loads(tc.arguments) if tc.arguments else {}
    except json.JSONDecodeError as e:
        messages.append(_tool_result(tc.id, {"error": f"invalid JSON args: {e}"}))
        return None


def _tool_result(tool_call_id: str, result: Any) -> dict[str, Any]:
    """OpenAI-shape tool result message; AnthropicProvider re-wraps as tool_result block."""
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(result, default=str),
    }


# ---------------------------------------------------------------------------
# Module-level "what time is it" helper for tests
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
