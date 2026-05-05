"""End-to-end upload: enriched state → Jira tickets, idempotent and resumable."""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from ..config import AppConfig
from ..models import (
    BugError,
    BugRecord,
    BugStage,
    EnrichedBug,
    UploadResult,
)
from ..state import StateStore
from .client import JiraClient, JiraError
from .field_map import FieldMap, build_field_map, discover_create_meta
from .user_resolver import UserResolver

log = logging.getLogger(__name__)

JIRA_DESCRIPTION_LIMIT = 32_500  # Jira DC cap is 32,767; leave safety margin.


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _validate_and_build_client(
    cfg: AppConfig,
    *,
    dry_run: bool,
    err_console: Console,
    client: JiraClient | None,
) -> JiraClient | None:
    """Pre-flight checks + JiraClient construction. Returns None on misconfig."""
    if not cfg.jira.url:
        err_console.print("[red]jira.url is not configured.[/red]")
        return None
    if not cfg.jira.project_key:
        err_console.print("[red]jira.project_key is not configured.[/red]")
        return None
    if client is not None:
        return client
    pat = os.environ.get("JIRA_PAT")
    if not pat and not dry_run:
        err_console.print("[red]JIRA_PAT env var is not set.[/red]")
        return None
    return JiraClient(
        cfg.jira.url,
        pat=pat,
        auth_mode=cfg.jira.auth_mode,
        user_email=cfg.jira.user_email,
        ca_bundle=cfg.jira.ca_bundle,
    )


def upload_state(
    *,
    state_file: Path,
    cfg: AppConfig,
    only: set[str] | None = None,
    retry_failed: bool = False,
    dry_run: bool = False,
    concurrency: int = 1,
    console: Console | None = None,
    err_console: Console | None = None,
    client: JiraClient | None = None,
) -> None:
    console = console or Console()
    err_console = err_console or Console(stderr=True)
    only = only or set()

    client = _validate_and_build_client(
        cfg, dry_run=dry_run, err_console=err_console, client=client
    )
    if client is None:
        return

    store = StateStore(state_file)
    state = store.load()
    targets = _select_targets(state.bugs, only=only, retry_failed=retry_failed)
    if not targets:
        console.print("[yellow]No bugs to upload.[/yellow]")
        return

    field_map = _resolve_field_map(client, cfg, err_console)
    if field_map is None:
        return  # error already printed

    user_resolver = UserResolver(
        client,
        cfg.jira.user_map_path,
        unknown_policy=cfg.jira.unknown_assignee_policy,
        default_assignee=cfg.jira.default_assignee,
    )

    valid_components = frozenset(client.list_project_components(cfg.jira.project_key))
    if not valid_components:
        log.info(
            "project %s has no Jira components; component field will be omitted",
            cfg.jira.project_key,
        )

    counts = {"ok": 0, "skipped": 0, "fail": 0}
    console.rule(
        f"[bold]upload[/bold] ({len(targets)} bugs, "
        f"project={cfg.jira.project_key}, dry_run={dry_run}, concurrency={concurrency})"
    )

    if concurrency <= 1:
        for record in targets:
            _process_one(
                record, cfg, client, field_map, user_resolver,
                dry_run=dry_run, counts=counts, console=console,
                valid_components=valid_components,
            )
            store.save(state)
    else:
        _run_upload_parallel(
            targets, cfg, client, field_map, user_resolver,
            dry_run=dry_run, concurrency=concurrency,
            state=state, store=store, counts=counts, console=console,
            valid_components=valid_components,
        )

    user_resolver.save()
    _print_summary(counts, console)


def _run_upload_parallel(
    targets: list[BugRecord],
    cfg: AppConfig,
    client: JiraClient,
    field_map: FieldMap,
    user_resolver: UserResolver,
    *,
    dry_run: bool,
    concurrency: int,
    state,
    store,
    counts: dict[str, int],
    console: Console,
    valid_components: frozenset[str],
) -> None:
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures: dict[Future, BugRecord] = {
            pool.submit(
                _process_one_threaded,
                rec, cfg, client, field_map, user_resolver, dry_run,
                valid_components,
            ): rec
            for rec in targets
        }
        for fut in as_completed(futures):
            outcome = fut.result()
            _commit_outcome(state, outcome, counts, console)
            store.save(state)


# ---------------------------------------------------------------------------
# Selection / outcomes
# ---------------------------------------------------------------------------

@dataclass
class _UploadOutcome:
    bug_id: str
    upload: UploadResult | None
    error: BugError | None
    skipped: bool = False
    skip_reason: str | None = None
    dry_run: bool = False


def _matches_only_upload(rec: BugRecord, only: set[str]) -> bool:
    return rec.parsed.bug_id in only or (
        rec.parsed.external_id is not None and rec.parsed.external_id in only
    )


def _eligible_to_upload(rec: BugRecord, retry_failed: bool) -> bool:
    if rec.stage == BugStage.UPLOADED:
        return False
    if rec.stage == BugStage.ENRICHED:
        return True
    if rec.stage == BugStage.FAILED and retry_failed:
        return rec.enriched is not None
    return False


def _select_targets(
    bugs: list[BugRecord], *, only: set[str], retry_failed: bool
) -> list[BugRecord]:
    if only:
        return [r for r in bugs if _matches_only_upload(r, only)]
    return [r for r in bugs if _eligible_to_upload(r, retry_failed)]


# ---------------------------------------------------------------------------
# Field-map resolution
# ---------------------------------------------------------------------------

_STANDARD_JIRA_FIELDS = frozenset(
    {
        "summary", "description", "priority", "issuetype", "project",
        "assignee", "reporter", "labels", "components", "fixVersions",
        "versions", "duedate", "environment", "parent",
    }
)


def _is_only_standard_fields(user_map_for_type: dict[str, str]) -> bool:
    """True iff every value in the user's field_map is a standard Jira field id.

    When that's the case, createmeta validation is unnecessary — every Jira
    project has these fields by definition. Skipping the check works around
    Jira instances where /rest/api/2/issue/createmeta is restricted to admins
    or returns misleading errors (NVIDIA jirasw is one — it returns "Issue Does
    Not Exist" instead of 403 when the caller lacks createmeta permission).
    """
    return all(fid in _STANDARD_JIRA_FIELDS for fid in user_map_for_type.values())


def _resolve_field_map(
    client: JiraClient,
    cfg: AppConfig,
    err_console: Console,
) -> FieldMap | None:
    user_map_for_type = cfg.jira.field_map.get(
        cfg.jira.issue_type.lower()
    ) or cfg.jira.field_map.get("bug", {})
    if not user_map_for_type:
        err_console.print(
            f"[red]No field_map configured for issue type "
            f"{cfg.jira.issue_type!r}.[/red] "
            "Run `f2j jira fields --project <KEY>` to discover IDs."
        )
        return None
    try:
        available = discover_create_meta(
            client, cfg.jira.project_key, cfg.jira.issue_type
        )
    except Exception as e:  # noqa: BLE001
        # createmeta is gated by the "Create Issues" permission on some Jira
        # instances (NVIDIA jirasw included). If the user's field_map only
        # references standard Jira fields, we don't actually need createmeta
        # to validate anything — proceed with a synthesized empty meta.
        if _is_only_standard_fields(user_map_for_type):
            err_console.print(
                f"[yellow]createmeta lookup failed[/yellow] ({e}); "
                "proceeding without validation since field_map only uses "
                "standard Jira fields."
            )
            return build_field_map(
                cfg.jira.project_key, cfg.jira.issue_type, user_map_for_type, {}
            )
        err_console.print(
            f"[red]createmeta lookup failed:[/red] {e}\n"
            "Your field_map references custom fields, which need validation. "
            "Try `f2j jira fields --from-issue <existing-ticket-key>` to "
            "discover IDs from an existing issue you can read."
        )
        return None
    fm = build_field_map(
        cfg.jira.project_key, cfg.jira.issue_type, user_map_for_type, available
    )
    if fm.missing:
        err_console.print(
            f"[yellow]warning:[/yellow] field_map references unknown fields: "
            f"{', '.join(fm.missing)}"
        )
    return fm


# ---------------------------------------------------------------------------
# Per-bug processing
# ---------------------------------------------------------------------------

def _process_one(
    record: BugRecord,
    cfg: AppConfig,
    client: JiraClient,
    field_map: FieldMap,
    user_resolver: UserResolver,
    *,
    dry_run: bool,
    counts: dict[str, int],
    console: Console,
    valid_components: frozenset[str],
) -> None:
    outcome = _process_one_threaded(
        record, cfg, client, field_map, user_resolver, dry_run,
        valid_components,
    )
    if outcome.dry_run:
        counts["ok"] += 1
        console.print(
            f"[green]ok[/green]    {outcome.bug_id}  -> DRY-RUN "
            f"[dim](payload built; not persisted, not uploaded)[/dim]"
        )
    elif outcome.upload is not None:
        record.upload = outcome.upload
        record.stage = BugStage.UPLOADED
        record.last_error = None
        counts["ok"] += 1
        console.print(
            f"[green]ok[/green]    {outcome.bug_id}  -> {outcome.upload.jira_key}"
        )
    elif outcome.skipped:
        counts["skipped"] += 1
        console.print(
            f"[dim]skip[/dim]  {outcome.bug_id}  ({outcome.skip_reason})"
        )
    else:
        record.stage = BugStage.FAILED
        record.last_error = outcome.error
        counts["fail"] += 1
        console.print(
            f"[red]fail[/red]  {outcome.bug_id}  "
            f"{outcome.error.message[:120] if outcome.error else ''}"
        )


def _process_one_threaded(
    record: BugRecord,
    cfg: AppConfig,
    client: JiraClient,
    field_map: FieldMap,
    user_resolver: UserResolver,
    dry_run: bool,
    valid_components: frozenset[str],
) -> _UploadOutcome:
    bug_id = record.parsed.bug_id

    # Already uploaded? Short-circuit.
    if record.upload and record.upload.jira_key:
        return _UploadOutcome(
            bug_id=bug_id, upload=None, error=None,
            skipped=True, skip_reason=f"already uploaded as {record.upload.jira_key}",
        )

    # Idempotency: JQL search for our label before creating. Prefer the
    # human-readable `upstream:<external_id>` label so the ticket isn't
    # cluttered with a hex-hash f2j-id label. Falls back to f2j-id:<bug_id>
    # only when the parsed bug has no external_id.
    label = (
        f"upstream:{record.parsed.external_id}"
        if record.parsed.external_id
        else f"{cfg.jira.external_id_label_prefix}:{bug_id}"
    )
    if not dry_run:
        try:
            existing = client.search_by_jql(
                f'labels = "{label}" AND project = "{cfg.jira.project_key}"',
                fields=["summary"],
                limit=1,
            )
            issues = existing.get("issues", [])
            if issues:
                key = issues[0]["key"]
                return _UploadOutcome(
                    bug_id=bug_id,
                    upload=UploadResult(
                        jira_key=key,
                        jira_url=client.issue_browse_url(key),
                        uploaded_at=_now_iso(),
                    ),
                    error=None,
                )
        except Exception as e:  # noqa: BLE001
            log.warning("idempotency search failed for %s: %s", bug_id, e)

    if record.enriched is None:
        return _UploadOutcome(
            bug_id=bug_id,
            upload=None,
            error=BugError(
                stage=BugStage.UPLOADING,
                message="bug is not enriched; run `f2j enrich` first",
                occurred_at=_now_iso(),
            ),
        )

    payload = build_issue_payload(
        record, cfg, field_map, user_resolver,
        label=label, valid_components=valid_components,
    )

    if dry_run:
        # Don't persist anything to state — dry-run must be read-only. Otherwise
        # the synthesized "DRY-RUN" key would trip the idempotency short-circuit
        # on the next live upload.
        return _UploadOutcome(
            bug_id=bug_id, upload=None, error=None, dry_run=True,
        )

    try:
        result = client.create_issue(payload["fields"])
    except Exception as e:  # noqa: BLE001
        return _UploadOutcome(
            bug_id=bug_id,
            upload=None,
            error=BugError(
                stage=BugStage.UPLOADING,
                message=f"create_issue failed: {e}",
                occurred_at=_now_iso(),
            ),
        )
    key = result.get("key")
    if not key:
        return _UploadOutcome(
            bug_id=bug_id,
            upload=None,
            error=BugError(
                stage=BugStage.UPLOADING,
                message=f"create_issue returned no key: {result}",
                occurred_at=_now_iso(),
            ),
        )
    parent_assignee = payload["fields"].get("assignee", {}).get("name")
    _create_subtasks(client, cfg, key, parent_assignee)
    return _UploadOutcome(
        bug_id=bug_id,
        upload=UploadResult(
            jira_key=key,
            jira_url=client.issue_browse_url(key),
            uploaded_at=_now_iso(),
        ),
        error=None,
    )


def _commit_outcome(
    state, outcome: _UploadOutcome, counts: dict[str, int], console: Console
) -> None:
    record = next((r for r in state.bugs if r.parsed.bug_id == outcome.bug_id), None)
    if record is None:
        return
    if outcome.dry_run:
        counts["ok"] += 1
        console.print(
            f"[green]ok[/green]    {outcome.bug_id}  -> DRY-RUN "
            f"[dim](payload built; not persisted, not uploaded)[/dim]"
        )
        return
    if outcome.upload is not None:
        record.upload = outcome.upload
        record.stage = BugStage.UPLOADED
        record.last_error = None
        counts["ok"] += 1
        console.print(
            f"[green]ok[/green]    {outcome.bug_id}  -> {outcome.upload.jira_key}"
        )
    elif outcome.skipped:
        counts["skipped"] += 1
        console.print(
            f"[dim]skip[/dim]  {outcome.bug_id}  ({outcome.skip_reason})"
        )
    else:
        record.stage = BugStage.FAILED
        record.last_error = outcome.error
        counts["fail"] += 1
        console.print(
            f"[red]fail[/red]  {outcome.bug_id}  "
            f"{outcome.error.message[:120] if outcome.error else ''}"
        )
    state.touch()


# ---------------------------------------------------------------------------
# Issue payload builder
# ---------------------------------------------------------------------------

def _module_assignee(cfg: AppConfig, record: BugRecord) -> str | None:
    """Return the module-routed assignee for ``record``, or None if unmapped."""
    alias = record.parsed.inherited_module.repo_alias
    if alias and alias in cfg.jira.module_to_assignee:
        return cfg.jira.module_to_assignee[alias]
    return None


def _resolve_assignee(
    record: BugRecord, cfg: AppConfig, user_resolver: UserResolver
) -> str | None:
    """Walk the assignee priority chain and return the resolved Jira username.

    Order: enriched.assignee_hint > parsed.hinted_assignee > module routing >
    default_assignee. Each candidate is run through the user_resolver so that
    display names get translated to SSO short names.
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

    Filters the merged list against the project's actual component set so that
    agent-invented values (e.g. directory paths it mistook for components) and
    stale config entries don't crash the upload. Anything dropped is logged.
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
    enriched: EnrichedBug, cfg: AppConfig, record: BugRecord, label: str
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
    `-`/`*` bullets, numbered lists, **bold**, ``inline code``, and triple-fence
    code blocks. Curly braces inside inline code are safe because Jira's
    `{{...}}` monospace treats its content literally — no further escaping
    needed for the typical `{type_id}`-in-path case that breaks raw markdown
    upload.
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


def _compose_description(enriched: EnrichedBug) -> str:
    description = markdown_to_jira_wiki(enriched.description_md)
    if len(description) > JIRA_DESCRIPTION_LIMIT:
        description = (
            description[:JIRA_DESCRIPTION_LIMIT]
            + "\n\n... (truncated; full text in state.json)"
        )
    return description


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


def build_issue_payload(
    record: BugRecord,
    cfg: AppConfig,
    field_map: FieldMap,
    user_resolver: UserResolver,
    *,
    label: str,
    valid_components: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Build the {fields: {...}} structure for POST /rest/api/2/issue."""
    enriched: EnrichedBug = record.enriched  # type: ignore[assignment]

    fields: dict[str, Any] = {
        "project": {"key": cfg.jira.project_key},
        "issuetype": {"name": cfg.jira.issue_type},
        "summary": enriched.summary,
        "description": _compose_description(enriched),
        "priority": {
            "name": cfg.jira.priority_values.get(enriched.priority, enriched.priority)
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


def _create_subtasks(
    client: JiraClient, cfg: AppConfig, parent_key: str, parent_assignee: str | None
) -> None:
    """Create configured subtasks under the just-created parent ticket.

    Failures are logged but don't roll back the parent — once the parent
    exists, a missing subtask is a recoverable nuisance, not a data loss.
    Subtasks with ``inherit_assignee=true`` reuse ``parent_assignee``; if
    the parent has no assignee, the subtask's assignee is left unset.
    """
    if not cfg.jira.subtasks:
        return
    for template in cfg.jira.subtasks:
        fields: dict[str, Any] = {
            "project": {"key": cfg.jira.project_key},
            "parent": {"key": parent_key},
            "issuetype": {"name": template.issue_type},
            "summary": template.title,
        }
        if template.description:
            fields["description"] = template.description
        assignee = parent_assignee if template.inherit_assignee else template.assignee
        if assignee:
            fields["assignee"] = {"name": assignee}
        try:
            client.create_issue(fields)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "subtask creation failed for parent %s (title=%r): %s",
                parent_key, template.title, e,
            )


def _resolve_epic(record: BugRecord, cfg: AppConfig) -> str | None:
    """Pick the Epic Link key for ``record`` using the configured priority chain.

    Order: external_id_prefix_to_epic (longest match) → module_to_epic →
    enriched.epic_key (if valid) → default_epic. Returns None if nothing fits.
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
    fields: dict[str, Any], record: BugRecord, cfg: AppConfig
) -> None:
    """Set the Epic Link customfield from the deterministic priority chain."""
    if not cfg.jira.epic_link_field:
        return
    chosen = _resolve_epic(record, cfg)
    if chosen:
        fields[cfg.jira.epic_link_field] = chosen


def _read_logical_field(enriched: EnrichedBug, record: BugRecord, logical: str) -> Any:
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


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _print_summary(counts: dict[str, int], console: Console) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("metric")
    table.add_column("count", justify="right")
    table.add_row("uploaded", str(counts["ok"]))
    table.add_row("skipped (already up)", str(counts["skipped"]))
    table.add_row("failed", str(counts["fail"]))
    console.rule("[bold]upload summary[/bold]")
    console.print(table)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
