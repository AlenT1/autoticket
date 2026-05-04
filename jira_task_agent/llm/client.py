"""Thin LLM client over NVIDIA Inference (OpenAI-compatible).

Pattern adapted from apps/CentArb/.../llm/client.py:
- One or more OpenAI clients, tried in order.
- One or more model IDs, tried in order per client.
- Strict JSON mode via response_format={"type": "json_object"} + json.loads,
  with code-fence stripping for models that wrap output in ```json ...```.
- Multi-model fallback handles transient 429 / 5xx / parse errors without
  per-call retry plumbing.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from openai import OpenAI

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


_client_singleton: OpenAI | None = None


def get_client() -> OpenAI:
    global _client_singleton
    if _client_singleton is None:
        api_key = os.getenv("NVIDIA_API_KEY")
        if not api_key:
            raise RuntimeError(
                "NVIDIA_API_KEY is required. Add it to .env. "
                "Get one from https://build.nvidia.com or your internal NVIDIA "
                "Inference console."
            )
        base_url = os.getenv("NVIDIA_BASE_URL") or DEFAULT_BASE_URL
        _client_singleton = OpenAI(api_key=api_key, base_url=base_url)
    return _client_singleton


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)


def strip_code_fences(text: str) -> str:
    text = text.strip()
    m = _FENCE_RE.match(text)
    return m.group(1).strip() if m else text


PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    """Load `prompts/<name>.md`."""
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
) -> tuple[str | dict, dict]:
    """Run one chat completion with multi-model fallback.

    Returns (content, metrics) where content is the parsed dict (json_mode=True)
    or the raw string. Raises RuntimeError if every model in the chain fails.
    """
    client = get_client()
    last_err: Exception | None = None
    for model in models:
        try:
            kwargs: dict = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
            }
            if max_tokens:
                kwargs["max_tokens"] = max_tokens
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            resp = client.chat.completions.create(**kwargs)
            raw = resp.choices[0].message.content or ""
            metrics = {
                "model": model,
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
                "total_tokens": getattr(resp.usage, "total_tokens", 0),
            }
            if json_mode:
                cleaned = strip_code_fences(raw)
                return json.loads(cleaned), metrics
            return raw, metrics
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("LLM call failed (model=%s): %s", model, e)
            continue
    raise RuntimeError(f"All models failed. Last error: {last_err}")
