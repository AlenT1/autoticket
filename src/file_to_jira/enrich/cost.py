"""Token → USD cost estimation, mirroring the sibling autodev project's table.

Anthropic's pricing varies by model; this table is the canonical mapping at
2026-05. If a model isn't in the table we fall back to Sonnet rates (and log a
warning at orchestration time).

Pricing units are USD per 1K tokens. Source: Anthropic public pricing page.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import EnrichmentMeta


@dataclass(frozen=True)
class _Rates:
    input_per_1k: float
    output_per_1k: float
    cache_read_per_1k: float
    cache_write_per_1k: float


# Keys are normalized to lowercase. Lookup tries exact match, then prefix match
# (so `claude-sonnet-4-6-20260101` falls through to `claude-sonnet-4-6`).
_RATE_TABLE: dict[str, _Rates] = {
    "claude-opus-4-7":   _Rates(0.015,   0.075,  0.0015,   0.01875),
    "claude-sonnet-4-6": _Rates(0.003,   0.015,  0.0003,   0.00375),
    "claude-haiku-4-5":  _Rates(0.0008,  0.004,  0.00008,  0.001),
}

_DEFAULT_RATES = _RATE_TABLE["claude-sonnet-4-6"]


def _lookup_rates(model: str) -> _Rates:
    m = model.lower().strip()
    if m in _RATE_TABLE:
        return _RATE_TABLE[m]
    for prefix, rates in _RATE_TABLE.items():
        if m.startswith(prefix):
            return rates
    return _DEFAULT_RATES


def estimate_cost_usd(meta: EnrichmentMeta) -> float:
    """Estimate the USD cost of one enrichment session from its EnrichmentMeta."""
    rates = _lookup_rates(meta.model)
    return round(
        (
            meta.input_tokens         * rates.input_per_1k
            + meta.output_tokens      * rates.output_per_1k
            + meta.cache_read_tokens  * rates.cache_read_per_1k
            + meta.cache_creation_tokens * rates.cache_write_per_1k
        )
        / 1000.0,
        4,
    )
