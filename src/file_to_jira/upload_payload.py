"""f2j payload builder — assemble the ``{fields: {...}}`` dict that
``POST /rest/api/2/issue`` consumes for one bug record.

Ported from the legacy ``file_to_jira.jira.uploader`` module so the
payload-building logic lives next to the f2j body's ``upload.py``
orchestrator. The orchestrator currently goes through the shared
:class:`JiraSink` for the actual HTTP write; this module is the
f2j-specific shape factory it can call before handing off.

What's portable as-is:
  - ``build_issue_payload`` and its private composers
    (``_compose_description`` / ``_compose_labels`` /
    ``_compose_components`` / ``_resolve_assignee`` / ``_apply_epic_link``
    / ``_apply_custom_fields``).
  - ``markdown_to_jira_wiki`` — the body-shape converter used by both
    the description composer and standalone tooling.

The legacy ``UserResolver`` is still imported from
``file_to_jira.jira.user_resolver`` for now; c8.c (the legacy package
delete) replaces this with the shared :class:`PickerWithCacheStrategy`
once the test migration confirms equivalence.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from _shared.io.sinks.jira import FieldMap

from .config import AppConfig
from .jira.user_resolver import UserResolver
from .models import BugRecord, EnrichedBug

log = logging.getLogger(__name__)


JIRA_DESCRIPTION_LIMIT = 32_500  # Jira DC cap is 32,767; leave safety margin.


# ---------------------------------------------------------------------------
# Markdown → Jira-wiki conversion
# ---------------------------------------------------------------------------

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_MD_BULLET_RE = re.compile(r"^(\s*)[-*]\s+(.+)$")
_MD_NUMBERED_RE = re.compile(r"^(\s*)\d+\.\s+(.+)$")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")


def _md_inline_to_wiki(line: str) -> str:
    """Convert inline markdown elements within a single line to Jira wiki."""
    line = _MD_BOLD_RE.sub(r"*\1*", line)
    line = _MD_INLINE_CODE_RE.sub(r"{{\1}}", line)
    return line


def markdown_to_jira_wiki(md: str) -> str:
    """Convert Markdown to Jira Server/DC wiki markup.

    Handles the subset the enrichment agent actually emits: ATX headings,
    ``-``/``*`` bullets, numbered lists, ``**bold**``, ``inline code``,
    and triple-fence code blocks. Curly braces inside inline code are
    safe because Jira's ``{{...}}`` monospace treats its content
    literally — no further escaping needed for the typical
    ``{type_id}``-in-path case that breaks raw markdown upload.
    """
    out: list[str] = []
    in_fence = False
    for raw_line in md.splitlines():
        stripped = raw_line.lstrip()
        # Code fence open / close. Preserve language hint when present.
        if stripped.startswith("```") or stripped.startswith("~~~"):
            if not in_fence:
                lang = stripped[3:].strip()
                out.append(f"{{code:{lang}}}" if lang else "{code}")
                in_fence = True
            else:
                out.append("{code}")
                in_fence = False
            continue
        if in_fence:
            out.append(raw_line)
            continue
        m = _MD_HEADING_RE.match(raw_line)
        if m:
            level = len(m.group(1))
            out.append(f"h{level}. {_md_inline_to_wiki(m.group(2))}")
            continue
        m = _MD_BULLET_RE.match(raw_line)
        if m:
            indent_spaces = len(m.group(1))
            depth = max(1, indent_spaces // 2 + 1)
            out.append(f"{'*' * depth} {_md_inline_to_wiki(m.group(2))}")
            continue
        m = _MD_NUMBERED_RE.match(raw_line)
        if m:
            indent_spaces = len(m.group(1))
            depth = max(1, indent_spaces // 2 + 1)
            out.append(f"{'#' * depth} {_md_inline_to_wiki(m.group(2))}")
            continue
        out.append(_md_inline_to_wiki(raw_line))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Composers — translate enriched-bug → Jira-payload pieces
# ---------------------------------------------------------------------------


def _module_assignee(cfg: AppConfig, record: BugRecord) -> str | None:
    """Return the module-routed assignee for ``record``, or None if unmapped."""
    alias = record.parsed.inherited_module.repo_alias
    if alias and alias in cfg.jira.module_to_assignee:
        return cfg.jira.module_to_assignee[alias]
    return None


def _resolve_assignee(
    record: BugRecord, cfg: AppConfig, user_resolver: UserResolver,
) -> str | None:
    """Walk the assignee priority chain and return the resolved Jira username.

    Order: ``enriched.assignee_hint`` > ``parsed.hinted_assignee`` >
    module routing > ``default_assignee``. Each candidate is run through
    the ``user_resolver`` so display names get translated to SSO short
    names.
    """
    enriched = record.enriched
    candidates = [
        enriched.assignee_hint if enriched else None,
        record.parsed.hinted_assignee,
        _module_assignee(cfg, record),
        cfg.jira.default_assignee,
    ]
    for candidate in candidates:
        if not candidate:
            continue
        resolution = user_resolver.resolve(candidate)
        if resolution.username:
            return resolution.username
    return None


def _compose_components(
    enriched: EnrichedBug,
    cfg: AppConfig,
    record: BugRecord,
    valid_components: frozenset[str],
) -> list[str]:
    """Merge enriched, module-mapped, and default components (de-duplicated).

    Filters the merged list against the project's actual component set so
    that agent-invented values (e.g. directory paths it mistook for
    components) and stale config entries don't crash the upload. Anything
    dropped is logged.
    """
    components = list(enriched.components or [])
    alias = record.parsed.inherited_module.repo_alias
    if alias and alias in cfg.jira.module_to_component:
        component_name = cfg.jira.module_to_component[alias]
        if component_name not in components:
            components.append(component_name)
    for default_component in cfg.jira.default_components:
        if default_component not in components:
            components.append(default_component)
    kept = [c for c in components if c in valid_components]
    dropped = [c for c in components if c not in valid_components]
    if dropped:
        log.info(
            "dropping unknown components for %s: %s (project has: %s)",
            record.parsed.bug_id, dropped, sorted(valid_components),
        )
    return kept


def _compose_labels(
    enriched: EnrichedBug, cfg: AppConfig, record: BugRecord, label: str,
) -> list[str]:
    """Merge enriched, default, idempotency, and upstream-id labels (de-duplicated)."""
    labels = list(enriched.labels or [])
    for default_label in cfg.jira.default_labels:
        if default_label not in labels:
            labels.append(default_label)
    if label not in labels:
        labels.append(label)
    if record.parsed.external_id:
        ext_label = f"upstream:{record.parsed.external_id}"
        if ext_label not in labels:
            labels.append(ext_label)
    return labels


def _compose_description(enriched: EnrichedBug) -> str:
    description = markdown_to_jira_wiki(enriched.description_md)
    if len(description) > JIRA_DESCRIPTION_LIMIT:
        description = (
            description[:JIRA_DESCRIPTION_LIMIT]
            + "\n\n... (truncated; full text in state.json)"
        )
    return description


# ---------------------------------------------------------------------------
# Epic-link routing (deterministic priority chain)
# ---------------------------------------------------------------------------


def _resolve_epic(record: BugRecord, cfg: AppConfig) -> str | None:
    """Pick the Epic Link key for ``record`` using the configured priority chain.

    Order: ``external_id_prefix_to_epic`` (longest match) → ``module_to_epic`` →
    ``enriched.epic_key`` (if valid) → ``default_epic``. Returns None if
    nothing fits.
    """
    external_id = record.parsed.external_id or ""
    if external_id and cfg.jira.external_id_prefix_to_epic:
        # Longest configured prefix wins — so "CORE-MOBILE-" beats "CORE-".
        for prefix in sorted(cfg.jira.external_id_prefix_to_epic, key=len, reverse=True):
            if external_id.startswith(prefix):
                return cfg.jira.external_id_prefix_to_epic[prefix]
    alias = record.parsed.inherited_module.repo_alias
    if alias and alias in cfg.jira.module_to_epic:
        return cfg.jira.module_to_epic[alias]
    enriched = record.enriched
    if enriched and enriched.epic_key:
        valid_keys = {e.key for e in cfg.jira.available_epics}
        if enriched.epic_key in valid_keys:
            return enriched.epic_key
    return cfg.jira.default_epic


def _apply_epic_link(
    fields: dict[str, Any], record: BugRecord, cfg: AppConfig,
) -> None:
    """Set the Epic Link customfield from the deterministic priority chain."""
    if not cfg.jira.epic_link_field:
        return
    chosen = _resolve_epic(record, cfg)
    if chosen:
        fields[cfg.jira.epic_link_field] = chosen


# ---------------------------------------------------------------------------
# Custom-field projection
# ---------------------------------------------------------------------------


def _read_logical_field(
    enriched: EnrichedBug, record: BugRecord, logical: str,
) -> Any:
    """Read a value out of the enriched bug for a logical custom field name."""
    if logical == "external_id":
        return record.parsed.external_id
    if logical == "expected_behavior":
        return enriched.expected_behavior
    if logical == "actual_behavior":
        return enriched.actual_behavior
    if logical == "affected_versions":
        return enriched.affected_versions
    if logical == "relevant_logs":
        return enriched.relevant_logs
    return None


def _apply_custom_fields(
    fields: dict[str, Any],
    field_map: FieldMap,
    enriched: EnrichedBug,
    record: BugRecord,
    cfg: AppConfig,
) -> None:
    """Project enriched values into the configured custom-field IDs."""
    for logical, fid in field_map.by_logical_name.items():
        if logical in {"summary", "description", "priority"}:
            continue  # already in the core fields block
        value = _read_logical_field(enriched, record, logical)
        if value is None:
            continue
        fields[fid] = value
    if cfg.jira.external_id_field and record.parsed.external_id:
        fields[cfg.jira.external_id_field] = record.parsed.external_id


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_issue_payload(
    record: BugRecord,
    cfg: AppConfig,
    field_map: FieldMap,
    user_resolver: UserResolver,
    *,
    label: str,
    valid_components: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Build the ``{fields: {...}}`` structure for ``POST /rest/api/2/issue``.

    Pure function — no Jira HTTP, no LLM, no state mutation. Tests can
    drive every routing branch by varying ``record`` / ``cfg`` /
    ``field_map`` / ``valid_components``.
    """
    enriched: EnrichedBug = record.enriched  # type: ignore[assignment]

    fields: dict[str, Any] = {
        "project": {"key": cfg.jira.project_key},
        "issuetype": {"name": cfg.jira.issue_type},
        "summary": enriched.summary,
        "description": _compose_description(enriched),
        "priority": {
            "name": cfg.jira.priority_values.get(enriched.priority, enriched.priority),
        },
        "labels": _compose_labels(enriched, cfg, record, label),
    }

    components = _compose_components(enriched, cfg, record, valid_components)
    if components:
        fields["components"] = [{"name": n} for n in components]

    assignee_username = _resolve_assignee(record, cfg, user_resolver)
    if assignee_username:
        fields["assignee"] = {"name": assignee_username}

    _apply_epic_link(fields, record, cfg)
    _apply_custom_fields(fields, field_map, enriched, record, cfg)
    return {"fields": fields}
