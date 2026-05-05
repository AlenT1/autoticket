"""Tests for the token-to-USD cost estimator."""

from __future__ import annotations

import pytest

from file_to_jira.enrich.cost import estimate_cost_usd
from file_to_jira.models import EnrichmentMeta


def _meta(model: str, **kwargs) -> EnrichmentMeta:
    return EnrichmentMeta(
        model=model,
        started_at="2026-05-03T12:00:00+00:00",
        finished_at="2026-05-03T12:00:30+00:00",
        **kwargs,
    )


def test_zero_tokens_zero_cost() -> None:
    assert estimate_cost_usd(_meta("claude-sonnet-4-6")) == 0.0


def test_sonnet_input_only() -> None:
    # 1000 input tokens at $0.003 / 1k = $0.003
    cost = estimate_cost_usd(_meta("claude-sonnet-4-6", input_tokens=1000))
    assert cost == 0.003


def test_sonnet_output_only() -> None:
    # 1000 output tokens at $0.015 / 1k = $0.015
    cost = estimate_cost_usd(_meta("claude-sonnet-4-6", output_tokens=1000))
    assert cost == 0.015


def test_opus_costs_5x_sonnet_input() -> None:
    sonnet = estimate_cost_usd(_meta("claude-sonnet-4-6", input_tokens=1000))
    opus = estimate_cost_usd(_meta("claude-opus-4-7", input_tokens=1000))
    assert opus == pytest.approx(sonnet * 5, rel=0.01)


def test_haiku_cheaper_than_sonnet() -> None:
    sonnet = estimate_cost_usd(_meta("claude-sonnet-4-6", input_tokens=10000))
    haiku = estimate_cost_usd(_meta("claude-haiku-4-5", input_tokens=10000))
    assert haiku < sonnet


def test_cache_read_is_cheap() -> None:
    # Cache reads should be ~10% of input cost.
    cache = estimate_cost_usd(
        _meta("claude-sonnet-4-6", cache_read_tokens=1000)
    )
    plain = estimate_cost_usd(_meta("claude-sonnet-4-6", input_tokens=1000))
    assert cache < plain / 5


def test_unknown_model_falls_back_to_sonnet_rates() -> None:
    sonnet = estimate_cost_usd(_meta("claude-sonnet-4-6", input_tokens=1000))
    fallback = estimate_cost_usd(_meta("claude-bizarre-future-model", input_tokens=1000))
    assert fallback == sonnet


def test_versioned_model_resolves_via_prefix() -> None:
    # `claude-sonnet-4-6-20260101` should resolve to `claude-sonnet-4-6`.
    base = estimate_cost_usd(_meta("claude-sonnet-4-6", input_tokens=1000))
    versioned = estimate_cost_usd(_meta("claude-sonnet-4-6-20260101", input_tokens=1000))
    assert versioned == base


def test_realistic_enrichment_session() -> None:
    """A typical bug enrichment: ~30k input, ~2k output, ~10k cache reads."""
    cost = estimate_cost_usd(
        _meta(
            "claude-sonnet-4-6",
            input_tokens=30_000,
            output_tokens=2_000,
            cache_read_tokens=10_000,
            cache_creation_tokens=3_000,
        )
    )
    # Hand-computed: 30k*0.003 + 2k*0.015 + 10k*0.0003 + 3k*0.00375 = 90+30+3+11.25 = 134.25 cents per thousand
    # Divided by 1000: $0.13425
    assert cost == pytest.approx(0.13425, abs=0.0001)
