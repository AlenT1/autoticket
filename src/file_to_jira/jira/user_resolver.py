"""Resolve human names from bug input to Jira usernames.

Workflow:
1. Load `configs/user_map.yaml` (an editable on-disk dictionary).
2. On miss, query `/rest/api/2/user/search` and disambiguate.
3. Cache the result back to the same yaml so repeat runs are deterministic.

The yaml is intentionally simple (`"Display Name": jdoe`) so humans can edit it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .client import JiraClient

log = logging.getLogger(__name__)


@dataclass
class UserResolution:
    name: str
    username: str | None
    confidence: str  # "exact" | "search" | "default" | "skip"


class UserResolver:
    def __init__(
        self,
        client: JiraClient | None,
        user_map_path: Path | str,
        *,
        unknown_policy: str = "default",
        default_assignee: str | None = None,
    ) -> None:
        self.client = client
        self.path = Path(user_map_path)
        self.unknown_policy = unknown_policy
        self.default_assignee = default_assignee
        self._mapping: dict[str, str] = {}
        self._dirty = False
        self._loaded = False

    def _load_if_needed(self) -> None:
        if self._loaded:
            return
        if self.path.exists():
            with self.path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                raise ValueError(f"{self.path}: must contain a YAML mapping")
            self._mapping = {str(k): str(v) for k, v in data.items()}
        self._loaded = True

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(
                self._mapping, f, allow_unicode=True, sort_keys=True, default_flow_style=False
            )
        self._dirty = False

    def resolve(self, name: str | None) -> UserResolution:
        if not name:
            return self._fallback(name or "(empty)")
        self._load_if_needed()
        if name in self._mapping:
            return UserResolution(name=name, username=self._mapping[name], confidence="exact")
        # Search Jira if a client is available.
        if self.client is not None:
            try:
                hits = self.client.search_user(name)
            except Exception as e:  # noqa: BLE001
                log.warning("user search failed for %r: %s", name, e)
                hits = []
            # Match against displayName first (input is typically a human name
            # like "Guy Keinan"), then username (`name` field) as a fallback for
            # callers that pass an SSO short name directly.
            target = name.lower()
            exact = [
                h for h in hits
                if (h.get("displayName") or "").lower() == target
                or (h.get("name") or "").lower() == target
            ]
            chosen = exact[0] if exact else (hits[0] if len(hits) == 1 else None)
            if chosen is not None:
                username = chosen.get("name") or chosen.get("key") or chosen.get("accountId")
                if username:
                    self._mapping[name] = username
                    self._dirty = True
                    return UserResolution(name=name, username=username, confidence="search")
        return self._fallback(name)

    def _fallback(self, name: str) -> UserResolution:
        if self.unknown_policy == "default" and self.default_assignee:
            # Translate the default through the user_map so we return a real
            # Jira username, not a display name. If the default isn't mapped
            # either, return its raw value — Jira will reject and the operator
            # will see they need to add the mapping.
            username = self._mapping.get(self.default_assignee, self.default_assignee)
            return UserResolution(
                name=name, username=username, confidence="default"
            )
        if self.unknown_policy == "fail":
            raise KeyError(f"unknown assignee: {name!r}")
        return UserResolution(name=name, username=None, confidence="skip")
