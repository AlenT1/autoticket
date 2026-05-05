"""Thin LLM wrapper for the doc-sync pipeline.

Routes through :class:`_shared.llm.OpenAICompatProvider` for the actual SDK
call. This module owns:

- the multi-model fallback chain (try N models in order on transient errors)
- per-task model defaults (classify / extract / summarize), env-overridable
- JSON-mode plumbing: code-fence stripping + ``json.loads`` of the content
- prompt loading + ``render_prompt`` (variable substitution that ignores
  literal ``{...}`` blocks in JSON examples)

What lives elsewhere:

- SDK construction, base_url + api_key resolution, normalized response
  shape — :class:`_shared.llm.OpenAICompatProvider`.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from _shared.llm import LLMProvider, OpenAICompatProvider

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://inference-api.nvidia.com/v1/"

# Fallback chains. First entry wins; later entries are tried only when the
# earlier ones fail. Override per-task via env (LLM_MODEL_CLASSIFY etc.).
_FALLBACK = [
    "openai/openai/gpt-5.2",
    "meta/llama-3.1-70b-instruct",
    "meta/llama-3.1-8b-instruct",
]


def _models_for(env_name: str, primary_default: str) -> list[str]:
    primary = os.getenv(env_name) or primary_default
    chain = [primary] + [m for m in _FALLBACK if m != primary]
    return chain


def models_classify() -> list[str]:
    return _models_for("LLM_MODEL_CLASSIFY", "meta/llama-3.1-70b-instruct")


def models_extract() -> list[str]:
    return _models_for("LLM_MODEL_EXTRACT", "openai/openai/gpt-5.2")


def models_summarize() -> list[str]:
    return _models_for("LLM_MODEL_SUMMARIZE", "meta/llama-3.1-8b-instruct")


_provider_singleton: LLMProvider | None = None


def _get_provider() -> LLMProvider:
    global _provider_singleton
    if _provider_singleton is None:
        api_key = os.getenv("NVIDIA_API_KEY")
        if not api_key:
            raise RuntimeError(
                "NVIDIA_API_KEY is required. Add it to .env. "
                "Get one from https://build.nvidia.com or your internal NVIDIA "
                "Inference console."
            )
        base_url = os.getenv("NVIDIA_BASE_URL") or DEFAULT_BASE_URL
        _provider_singleton = OpenAICompatProvider(api_key=api_key, base_url=base_url)
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
