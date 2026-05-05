"""Provider lookup by name.

Used so config-driven selection (``enrichment.provider: openai_compatible``)
can dispatch to the right impl without bodies hard-coding the class.
"""
from __future__ import annotations

from typing import Any

from .anthropic_provider import AnthropicProvider
from .base import LLMProvider
from .openai_compat import OpenAICompatProvider


def get_provider(name: str, **kwargs: Any) -> LLMProvider:
    """Return a provider instance by canonical name.

    Recognized names:

    - ``"openai_compatible"`` / ``"openai"`` — :class:`OpenAICompatProvider`
    - ``"anthropic"``                       — :class:`AnthropicProvider`

    All keyword arguments pass through to the provider constructor.
    """
    key = (name or "").lower()
    if key in ("openai_compatible", "openai", "nvidia"):
        return OpenAICompatProvider(**kwargs)
    if key == "anthropic":
        return AnthropicProvider(**kwargs)
    raise ValueError(
        f"Unknown LLM provider: {name!r}. "
        "Recognized: 'openai_compatible' / 'openai' / 'nvidia' / 'anthropic'."
    )
