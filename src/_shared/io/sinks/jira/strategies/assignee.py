"""Assignee resolvers — display name → tracker-native username.

Three impls:
- :class:`PassthroughAssigneeResolver` — the default; assumes the caller
  already has a tracker-native username.
- :class:`StaticMapStrategy` — drive's pattern; loads a JSON map from disk
  (e.g. ``team_mapping.json``). Composite owners (``"Lior + Aviv"``)
  resolve to the first individual.
- :class:`PickerWithCacheStrategy` — f2j's pattern; checks a YAML cache
  first, falls back to the tracker's user-picker endpoint, persists hits
  back to the cache for next time.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Composite-owner separator: "Lior + Aviv" / "Lior/Aviv" / "Lior, Aviv" / "Lior and Aviv"
_COMPOSITE_RE = re.compile(r"\s*(?:\+|/|&|,| and )\s*")


def _first_owner(raw: str) -> str:
    """Return the first individual from a composite owner string."""
    parts = _COMPOSITE_RE.split(raw)
    return parts[0].strip() if parts else raw.strip()


class PassthroughAssigneeResolver:
    """Default resolver — name is already a tracker-native username."""

    def resolve(self, display_name: str) -> str | None:
        return display_name or None


class StaticMapStrategy:
    """Resolve via a JSON map ``{display_name: username}`` loaded from disk.

    Args:
        map_path: Path to a JSON file mapping display names → usernames.
            Defaults to ``team_mapping.json`` in the CWD (drive's
            convention). If the file is missing, every lookup misses.
        default_username: Optional fallback username when a name is unmapped.
            ``None`` means "leave unassigned on miss".

    Composite owners (``"Lior + Aviv"``) resolve to the first individual.
    Lookup is case-insensitive.
    """

    def __init__(
        self,
        *,
        map_path: Path | str = "team_mapping.json",
        default_username: str | None = None,
    ) -> None:
        self.map_path = Path(map_path)
        self.default_username = default_username
        self._map: dict[str, str] | None = None  # lazy

    def resolve(self, display_name: str) -> str | None:
        if not display_name:
            return None
        first = _first_owner(display_name)
        if not first:
            return None
        m = self._load()
        username = m.get(first.lower())
        if username:
            return username
        return self.default_username

    def _load(self) -> dict[str, str]:
        if self._map is not None:
            return self._map
        if not self.map_path.exists():
            self._map = {}
            return self._map
        try:
            import json
            data = json.loads(self.map_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("failed to load %s: %s", self.map_path, e)
            self._map = {}
            return self._map
        if not isinstance(data, dict):
            log.warning("%s: must contain a JSON object", self.map_path)
            self._map = {}
            return self._map
        self._map = {
            str(k).strip().lower(): str(v).strip()
            for k, v in data.items()
            if k and v
        }
        return self._map


class PickerWithCacheStrategy:
    """Resolve via a YAML cache + the tracker's user-picker endpoint.

    Lookup order:
    1. In-memory + on-disk YAML cache (``configs/user_map.yaml``).
    2. ``client.search_user_picker(query)`` — Jira's lenient name match.
    3. ``unknown_policy`` fallback (``"default"`` → ``default_username``;
       ``"fail"`` → raise; ``"skip"`` → return None).

    On a successful tracker-side hit, the result is cached back to the
    YAML so subsequent runs are deterministic. Call :meth:`save` to flush
    the cache to disk.

    Args:
        client: A JiraClient (must expose ``search_user_picker(query)``).
        user_map_path: Path to the YAML cache. Created on save if missing.
        unknown_policy: ``"default"`` | ``"fail"`` | ``"skip"``.
        default_username: Fallback username when ``unknown_policy="default"``.
    """

    def __init__(
        self,
        *,
        client: Any,
        user_map_path: Path | str,
        unknown_policy: str = "default",
        default_username: str | None = None,
    ) -> None:
        self.client = client
        self.path = Path(user_map_path)
        self.unknown_policy = unknown_policy
        self.default_username = default_username
        self._mapping: dict[str, str] = {}
        self._dirty = False
        self._loaded = False

    def resolve(self, display_name: str) -> str | None:
        if not display_name:
            return self._fallback()
        self._load_if_needed()
        if display_name in self._mapping:
            return self._mapping[display_name]
        # Tracker-side lookup
        try:
            hits = self.client.search_user_picker(display_name) or []
        except Exception as e:  # noqa: BLE001
            log.warning("user picker search failed for %r: %s", display_name, e)
            hits = []
        chosen = self._pick_user(hits, display_name)
        if chosen is not None:
            username = chosen.get("name") or chosen.get("key") or chosen.get("accountId")
            if username:
                self._mapping[display_name] = username
                self._dirty = True
                return username
        return self._fallback()

    def save(self) -> None:
        """Flush new cache entries to disk. No-op if nothing changed."""
        if not self._dirty:
            return
        import yaml
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(
                self._mapping,
                f,
                allow_unicode=True,
                sort_keys=True,
                default_flow_style=False,
            )
        self._dirty = False

    # ---- internals ------------------------------------------------------

    def _load_if_needed(self) -> None:
        if self._loaded:
            return
        if self.path.exists():
            import yaml
            with self.path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                raise ValueError(f"{self.path}: must contain a YAML mapping")
            self._mapping = {str(k): str(v) for k, v in data.items()}
        self._loaded = True

    def _pick_user(self, hits: list[dict[str, Any]], target: str) -> dict[str, Any] | None:
        """Pick the best match from a picker response.

        Prefer exact match on ``displayName`` (case-insensitive), then exact
        match on ``name`` (Jira-Server SSO short name); if no exact match
        and there's exactly one hit, accept it.
        """
        target_lc = target.lower()
        exact = [
            h for h in hits
            if (h.get("displayName") or "").lower() == target_lc
            or (h.get("name") or "").lower() == target_lc
        ]
        if exact:
            return exact[0]
        if len(hits) == 1:
            return hits[0]
        return None

    def _fallback(self) -> str | None:
        if self.unknown_policy == "default" and self.default_username:
            # Translate through the cache so we return a real username
            # rather than a display name (matches f2j's UserResolver).
            return self._mapping.get(self.default_username, self.default_username)
        if self.unknown_policy == "fail":
            raise KeyError("unknown assignee")
        return None
