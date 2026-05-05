"""Epic routers — pick which Jira epic a created ticket links to.

Two impls:
- :class:`NoOpStrategy` — default; trust ``ticket.epic_key`` as set by the
  body. Drive's pattern (epics come from extracted-then-matched flow).
- :class:`DeterministicChainStrategy` — f2j's pattern; priority chain:
  external_id-prefix → module → LLM pick → default. The LLM pick (read
  from ``ticket.epic_key`` or ``ticket.custom_fields["llm_epic_pick"]``)
  is honored only if it's in the curated ``available_epics`` list.

Both read context from ``ticket.custom_fields``:
- ``external_id`` — used by external_id_prefix matching
- ``module`` (or ``repo_alias``) — used by module-based routing
- ``llm_epic_pick`` — alternative to ``ticket.epic_key`` for the LLM-pick layer
"""
from __future__ import annotations

from collections.abc import Mapping

from ...base import Ticket


class NoOpStrategy:
    """Trust ``ticket.epic_key`` as-is. The default."""

    def route(self, ticket: Ticket, *, hint: str | None = None) -> str | None:
        return ticket.epic_key


class DeterministicChainStrategy:
    """f2j's deterministic epic-link priority chain.

    Order of resolution:
    1. **external_id prefix** — longest matching prefix from
       ``prefix_map`` wins (so ``"CORE-MOBILE-"`` beats ``"CORE-"``).
    2. **module** — ``module_map[ticket.custom_fields["module"]]`` if set.
    3. **LLM pick** — ``ticket.epic_key`` (or ``custom_fields["llm_epic_pick"]``)
       if it's in the ``available_epics`` set; otherwise discarded.
    4. **default** — ``default_epic`` if set, else ``None``.

    Args:
        prefix_map: ``{external_id_prefix: epic_key}`` (e.g.
            ``{"X-OBS-": "CENTPM-1179", "CORE-MOBILE-": "CENTPM-1235"}``).
        module_map: ``{module_alias: epic_key}`` (e.g.
            ``{"_core": "CENTPM-1184", "vibe_coding/centarb": "CENTPM-1197"}``).
        available_epics: Set of valid epic keys. The LLM pick layer (#3)
            requires the candidate to be in this set; without it, agent
            hallucinations would slip through.
        default_epic: Final fallback epic key (e.g. ``"CENTPM-1184"``).
    """

    def __init__(
        self,
        *,
        prefix_map: Mapping[str, str] | None = None,
        module_map: Mapping[str, str] | None = None,
        available_epics: set[str] | None = None,
        default_epic: str | None = None,
    ) -> None:
        self.prefix_map = dict(prefix_map or {})
        self.module_map = dict(module_map or {})
        self.available_epics = set(available_epics or ())
        self.default_epic = default_epic

    def route(self, ticket: Ticket, *, hint: str | None = None) -> str | None:
        # 1. External-id prefix (longest match wins)
        external_id = (
            hint
            or ticket.custom_fields.get("external_id")
            or ""
        )
        if external_id and self.prefix_map:
            for prefix in sorted(self.prefix_map, key=len, reverse=True):
                if external_id.startswith(prefix):
                    return self.prefix_map[prefix]

        # 2. Module / repo alias
        module = (
            ticket.custom_fields.get("module")
            or ticket.custom_fields.get("repo_alias")
        )
        if module and module in self.module_map:
            return self.module_map[module]

        # 3. LLM pick — gated by available_epics
        llm_pick = (
            ticket.epic_key
            or ticket.custom_fields.get("llm_epic_pick")
        )
        if llm_pick and (not self.available_epics or llm_pick in self.available_epics):
            return llm_pick

        # 4. Default
        return self.default_epic
