"""Thin LLM wrapper for the doc-sync pipeline.

Routes through :class:`_shared.llm.OpenAICompatProvider` for the actual SDK
call. This module owns:

- the multi-model fallback chain (try N models in order on transient errors)
- per-task model defaults (classify / extract / summarize), read from
  ``configs/jira_task_agent.yaml``
- JSON-mode plumbing: code-fence stripping + ``json.loads`` of the content
- prompt loading + ``render_prompt`` (variable substitution that ignores
  literal ``{...}`` blocks in JSON examples)

What lives elsewhere:

- SDK construction, base_url + api_key resolution, normalized response
  shape — :class:`_shared.llm.OpenAICompatProvider`.
- Operator-edited values (API key, base URL) — ``.env`` via
  :class:`_shared.config.Settings`.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from _shared.llm import LLMProvider, OpenAICompatProvider

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://inference-api.nvidia.com/v1/"

# Hard-coded fallback chain when the YAML or its specific entry is missing.
# Used in the order shown; first model that succeeds wins.
_HARD_FALLBACK = [
    "openai/openai/gpt-5.2",
    "meta/llama-3.1-70b-instruct",
    "meta/llama-3.1-8b-instruct",
]

# Per-task hard defaults — used only when the YAML config can't be read.
_TASK_HARD_DEFAULT = {
    "classify":  "meta/llama-3.1-70b-instruct",
    "extract":   "openai/openai/gpt-5.2",
    "summarize": "meta/llama-3.1-8b-instruct",
}

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "jira_task_agent.yaml"

_yaml_cache: dict[str, Any] | None = None


def _load_yaml_config() -> dict[str, Any]:
    """Read configs/jira_task_agent.yaml once, cache, return parsed dict."""
    global _yaml_cache
    if _yaml_cache is not None:
        return _yaml_cache
    if not _CONFIG_PATH.exists():
        logger.warning(
            "configs/jira_task_agent.yaml not found at %s; using hard-coded "
            "model defaults.", _CONFIG_PATH
        )
        _yaml_cache = {}
        return _yaml_cache
    with _CONFIG_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"{_CONFIG_PATH} must be a YAML mapping at the root."
        )
    _yaml_cache = data
    return _yaml_cache


def _models_for(task: str) -> list[str]:
    cfg = _load_yaml_config()
    models_section = cfg.get("models") or {}
    primary = models_section.get(task) or _TASK_HARD_DEFAULT[task]
    return [primary] + [m for m in _HARD_FALLBACK if m != primary]


def models_classify() -> list[str]:
    return _models_for("classify")


def models_extract() -> list[str]:
    return _models_for("extract")


def models_summarize() -> list[str]:
    return _models_for("summarize")


_provider_singleton: LLMProvider | None = None


def _get_provider() -> LLMProvider:
    global _provider_singleton
    if _provider_singleton is None:
        from _shared.config import load_settings
        _provider_singleton = OpenAICompatProvider.from_settings(load_settings())
    return _provider_singleton


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)


def strip_code_fences(text: str) -> str:
    text = text.strip()
    m = _FENCE_RE.match(text)
    return m.group(1).strip() if m else text


PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    """Load `prompts/<name>.md`. `name` may contain slashes for subdirs."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def render_prompt(template: str, **vars: object) -> str:
    """Substitute `{name}` placeholders in `template` with `vars[name]`.

    Unlike `str.format`, leaves any unrelated `{...}` literals untouched —
    important because our prompts contain literal JSON examples with braces.
    """
    out = template
    for key, value in vars.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def chat(
    *,
    system: str,
    user: str,
    models: list[str],
    temperature: float = 0.1,
    max_tokens: int | None = None,
    json_mode: bool = False,
) -> tuple[Any, dict]:
    """Run one chat completion with multi-model fallback.

    Returns ``(content, metrics)`` where content is the parsed dict
    (json_mode=True) or the raw string. Raises RuntimeError if every model
    in the chain fails.
    """
    provider = _get_provider()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    response_format = {"type": "json_object"} if json_mode else None

    last_err: Exception | None = None
    for model in models:
        try:
            resp = provider.chat(
                messages=messages,
                model=model,
                response_format=response_format,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("LLM call failed (model=%s): %s", model, e)
            continue

        metrics = {
            "model": model,
            "prompt_tokens": resp.usage.get("prompt_tokens", 0),
            "completion_tokens": resp.usage.get("completion_tokens", 0),
            "total_tokens": resp.usage.get("total_tokens", 0),
        }
        if json_mode:
            try:
                cleaned = strip_code_fences(resp.content)
                return json.loads(cleaned), metrics
            except json.JSONDecodeError as e:
                last_err = e
                logger.warning(
                    "LLM returned non-JSON despite json_mode (model=%s): %s",
                    model,
                    e,
                )
                continue
        return resp.content, metrics

    raise RuntimeError(f"All models failed. Last error: {last_err}")
