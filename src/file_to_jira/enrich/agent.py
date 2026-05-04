"""Claude tool-use agent loop, one session per bug.

We drive the Anthropic SDK directly (rather than depending on a higher-level
agent framework) so that:
- The submit_enrichment validation/retry loop is fully under our control.
- Prompt caching boundaries are explicit (`cache_control: ephemeral` on the
  system prompt + tool registry).
- Token accounting is tied to our EnrichmentMeta on a per-bug basis.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from pydantic import ValidationError

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
# Tool registry (Anthropic Tool Use schema)
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
    """Tool definitions sent to the Anthropic API. The last tool gets cache_control."""
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


def _build_anthropic_client(
    base_url: str | None = None,
    auth_token_env: str | None = None,
) -> Any:
    """Construct an Anthropic client honoring the per-operator overrides.

    With both args None this is equivalent to ``Anthropic()`` — i.e., the SDK
    picks up ``ANTHROPIC_API_KEY`` and ``ANTHROPIC_BASE_URL`` from env.
    Explicit values take precedence over env so a project-local config can
    pin the routing.
    """
    import os

    kwargs: dict[str, Any] = {}
    if base_url:
        kwargs["base_url"] = base_url
    if auth_token_env:
        token = os.environ.get(auth_token_env)
        if token:
            kwargs["auth_token"] = token
    return Anthropic(**kwargs)


class EnrichmentAgent:
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
        enable_prompt_caching: bool = True,
        base_url: str | None = None,
        auth_token_env: str | None = None,
        available_epics: list[Any] | None = None,
    ) -> None:
        self.toolkit = toolkit
        self.submit_tool = submit_tool
        self._client = (
            client
            if client is not None
            else _build_anthropic_client(base_url, auth_token_env)
        )
        self.model = model
        self.max_turns = max_turns
        self.max_tokens_per_turn = max_tokens_per_turn
        self.temperature = temperature
        self.system_prompt = system_prompt or load_system_prompt()
        self.system_prompt_hash = system_prompt_hash(self.system_prompt)
        self.enable_prompt_caching = enable_prompt_caching
        self.available_epics = available_epics
        self.tools = build_tool_registry()

    def enrich(self, bug: ParsedBug) -> EnrichedBug:
        """Run one bug through the agent loop. Raises EnrichmentError on failure."""
        usage = _Usage()
        started = datetime.now(timezone.utc)
        repos_touched: set[str] = set()

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": format_initial_prompt(bug, self.available_epics)}
        ]
        system = self._system_blocks()

        for turn in range(self.max_turns):
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens_per_turn,
                temperature=self.temperature,
                system=system,
                tools=self.tools,
                messages=messages,
            )
            self._account_usage(usage, response)

            if response.stop_reason == "end_turn":
                # Agent ended without submitting. Treat as a failure.
                raise EnrichmentTruncated(
                    f"agent ended turn without calling submit_enrichment "
                    f"after {turn + 1} turn(s)"
                )

            if response.stop_reason != "tool_use":
                raise EnrichmentError(
                    f"unexpected stop_reason {response.stop_reason!r} on turn {turn + 1}"
                )

            # Append assistant content verbatim.
            messages.append({"role": "assistant", "content": response.content})

            # Dispatch each tool_use block. submit_enrichment is special: a
            # successful call ends the session.
            tool_results: list[dict[str, Any]] = []
            submitted: EnrichedBug | None = None
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                usage.tool_calls += 1
                tu_id = block.id
                tu_name = block.name
                tu_input = block.input

                if tu_name == "submit_enrichment":
                    result = self.submit_tool(tu_input)
                    tool_results.append(_tool_result(tu_id, result))
                    if result.get("ok"):
                        try:
                            submitted = EnrichedBug.model_validate(result["enriched"])
                        except ValidationError as e:
                            # Should not happen — submit_tool already validated.
                            raise EnrichmentError(
                                f"submit_tool returned ok but EnrichedBug failed: {e}"
                            ) from e
                else:
                    result, repo_alias = self._dispatch_tool(tu_name, tu_input)
                    if repo_alias:
                        repos_touched.add(repo_alias)
                    tool_results.append(_tool_result(tu_id, result))

            if submitted is not None:
                # Successful submission ends the session.
                submitted.enrichment_meta = self._build_meta(
                    started, usage, repos_touched, truncated=False
                )
                return submitted

            messages.append({"role": "user", "content": tool_results})

        raise EnrichmentTruncated(
            f"hit max_turns ({self.max_turns}) without successful submit_enrichment"
        )

    # ---- helpers --------------------------------------------------------

    def _system_blocks(self) -> list[dict[str, Any]]:
        block: dict[str, Any] = {"type": "text", "text": self.system_prompt}
        if self.enable_prompt_caching:
            block["cache_control"] = {"type": "ephemeral"}
        return [block]

    def _dispatch_tool(self, name: str, args: dict[str, Any]) -> tuple[Any, str | None]:
        """Run a non-submit tool. Returns (result_dict_or_error, repo_alias_if_known)."""
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
        usage.input_tokens += getattr(u, "input_tokens", 0) or 0
        usage.output_tokens += getattr(u, "output_tokens", 0) or 0
        usage.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
        usage.cache_creation_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0

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


def _tool_result(tool_use_id: str, result: Any) -> dict[str, Any]:
    """Format a tool result block for the next turn's user message."""
    is_error = isinstance(result, dict) and (
        "error" in result or result.get("ok") is False
    )
    text = json.dumps(result, default=str)
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": text,
        "is_error": is_error,
    }


# ---------------------------------------------------------------------------
# Module-level "what time is it" helper for tests
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
