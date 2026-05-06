"""Tests for `_shared.io.sinks.jira.strategies.assignee`.

`PickerWithCacheStrategy` is the canonical replacement for the legacy
`file_to_jira.jira.user_resolver.UserResolver` (deleted in c8.c). These
tests cover the same behavioral contract — YAML cache lookup, Jira
picker fallback, unknown-policy dispatch — adapted to the strategy's
`str | None` return shape.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from _shared.io.sinks.jira.strategies.assignee import (
    PassthroughAssigneeResolver,
    PickerWithCacheStrategy,
    StaticMapStrategy,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


class _FakePickerClient:
    """Stand-in for `JiraClient.search_user_picker`."""

    def __init__(self, hits_by_query: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._hits = hits_by_query or {}
        self.queries: list[str] = []

    def search_user_picker(self, query: str) -> list[dict[str, Any]]:
        self.queries.append(query)
        return self._hits.get(query, [])


# ======================================================================
# PickerWithCacheStrategy — replaces legacy UserResolver
# ======================================================================


def test_picker_with_cache_loads_existing_yaml_cache(tmp_path: Path) -> None:
    """A pre-existing YAML cache resolves names without any tracker call."""
    cache = tmp_path / "u.yaml"
    cache.write_text(
        '"Yair Sadan": ysadan\n"Guy Keinan": gkeinan\n', encoding="utf-8",
    )
    client = _FakePickerClient()
    resolver = PickerWithCacheStrategy(
        client=client, user_map_path=cache,
    )
    assert resolver.resolve("Yair Sadan") == "ysadan"
    assert resolver.resolve("Guy Keinan") == "gkeinan"
    # Cached hits don't query the tracker.
    assert client.queries == []


def test_picker_with_cache_searches_tracker_and_caches(tmp_path: Path) -> None:
    """A name not in the cache hits the tracker's user-picker. A
    successful match is added back to the in-memory cache and (after
    `save()`) persisted to disk."""
    cache = tmp_path / "u.yaml"
    client = _FakePickerClient(
        hits_by_query={
            "Lior Eli": [
                {"name": "lior", "displayName": "Lior Eli"},
            ],
        },
    )
    resolver = PickerWithCacheStrategy(client=client, user_map_path=cache)

    assert resolver.resolve("Lior Eli") == "lior"
    assert client.queries == ["Lior Eli"]

    # Second call should hit the in-memory cache, not re-query.
    assert resolver.resolve("Lior Eli") == "lior"
    assert client.queries == ["Lior Eli"]

    # Save → flush cache to disk for next run.
    resolver.save()
    assert cache.exists()
    text = cache.read_text(encoding="utf-8")
    assert "Lior Eli" in text
    assert "lior" in text


def test_picker_with_cache_unknown_default_policy(tmp_path: Path) -> None:
    """`unknown_policy='default'` returns `default_username` when the
    name isn't in cache and the picker has no hit."""
    resolver = PickerWithCacheStrategy(
        client=_FakePickerClient(),
        user_map_path=tmp_path / "u.yaml",
        unknown_policy="default",
        default_username="triage",
    )
    assert resolver.resolve("Mystery Person") == "triage"


def test_picker_with_cache_unknown_skip_policy(tmp_path: Path) -> None:
    """`unknown_policy='skip'` returns None instead of raising or routing."""
    resolver = PickerWithCacheStrategy(
        client=_FakePickerClient(),
        user_map_path=tmp_path / "u.yaml",
        unknown_policy="skip",
    )
    assert resolver.resolve("Mystery Person") is None


def test_picker_with_cache_unknown_fail_policy(tmp_path: Path) -> None:
    """`unknown_policy='fail'` raises KeyError instead of falling through."""
    resolver = PickerWithCacheStrategy(
        client=_FakePickerClient(),
        user_map_path=tmp_path / "u.yaml",
        unknown_policy="fail",
    )
    with pytest.raises(KeyError):
        resolver.resolve("Mystery Person")


def test_picker_with_cache_picker_returns_no_hit_falls_through_to_policy(
    tmp_path: Path,
) -> None:
    """If the tracker returns hits but none match the target name, fall
    through to the unknown-policy branch (not silent acceptance)."""
    client = _FakePickerClient(
        hits_by_query={
            "Lior": [
                {"name": "different", "displayName": "Different Person"},
                {"name": "alsodiff", "displayName": "Some Other"},
            ],
        },
    )
    resolver = PickerWithCacheStrategy(
        client=client, user_map_path=tmp_path / "u.yaml",
        unknown_policy="default", default_username="triage",
    )
    # Two ambiguous hits, neither matches 'Lior' exactly → policy fallback.
    assert resolver.resolve("Lior") == "triage"


def test_picker_with_cache_handles_picker_exception(tmp_path: Path) -> None:
    """Tracker errors during the picker call are caught — the resolver
    falls through to the unknown-policy branch instead of propagating."""
    class _RaisingClient:
        def search_user_picker(self, query: str):
            raise RuntimeError("403 Forbidden")

    resolver = PickerWithCacheStrategy(
        client=_RaisingClient(), user_map_path=tmp_path / "u.yaml",
        unknown_policy="skip",
    )
    assert resolver.resolve("Anybody") is None


# ======================================================================
# Smoke tests for the other two assignee strategies (PassthroughAssigneeResolver,
# StaticMapStrategy) — these are simple, but they're the f2j ↔ drive
# split and warrant a regression net.
# ======================================================================


def test_passthrough_resolver_returns_input_unchanged() -> None:
    """`PassthroughAssigneeResolver` returns the display_name verbatim
    (assumed to already be a tracker-native username)."""
    r = PassthroughAssigneeResolver()
    assert r.resolve("sriftin") == "sriftin"
    assert r.resolve("") is None  # falsy → None


def test_static_map_strategy_loads_json_and_resolves_first_owner(
    tmp_path: Path,
) -> None:
    """`StaticMapStrategy` reads a JSON map of display→username, with
    composite-owner support (`'A + B'` → first owner) and case-insensitive
    lookup."""
    map_path = tmp_path / "team_mapping.json"
    map_path.write_text(
        '{"Lior Eli": "lior", "Aviv Cohen": "aviv"}', encoding="utf-8",
    )
    r = StaticMapStrategy(map_path=map_path)
    assert r.resolve("Lior Eli") == "lior"
    assert r.resolve("lior eli") == "lior"  # case-insensitive
    # Composite owner uses the first individual.
    assert r.resolve("Lior Eli + Aviv Cohen") == "lior"


def test_static_map_strategy_returns_default_when_unmapped(
    tmp_path: Path,
) -> None:
    """Missing mapping → `default_username` (or None)."""
    r = StaticMapStrategy(
        map_path=tmp_path / "nonexistent.json",
        default_username="fallback",
    )
    assert r.resolve("Anybody") == "fallback"

    r2 = StaticMapStrategy(map_path=tmp_path / "nonexistent.json")
    assert r2.resolve("Anybody") is None
