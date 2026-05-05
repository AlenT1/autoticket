"""Upload f2j enriched bugs to Jira via the shared :class:`JiraSink`.

Replaces the legacy :mod:`file_to_jira.jira.uploader` path with a thin
orchestrator on top of ``_shared/io/sinks/jira``. f2j-specific concerns are
expressed as plug-in strategies, not hard-coded behaviors:

- **Idempotency**: :class:`LabelSearchStrategy` searches by
  ``upstream:<external_id>`` (or ``f2j-id:<bug_id>`` fallback).
- **Assignee resolution**: :class:`PickerWithCacheStrategy` checks
  ``configs/user_map.yaml`` then falls back to ``/user/picker``, caches
  hits back to the YAML.
- **Epic routing**: :class:`DeterministicChainStrategy` walks
  external_id-prefix → module → LLM pick → default.
- **Component filtering**: handled by :class:`JiraSink` against the
  project's live component list.
"""
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

from _shared.io.sinks import Ticket
from _shared.io.sinks.jira import JiraSink
from _shared.io.sinks.jira.strategies import (
    DeterministicChainStrategy,
    LabelSearchStrategy,
    PickerWithCacheStrategy,
)
from jira_task_agent.jira.client import (
    JiraClient,
    _build_auth_header,
    _normalize_host,
)

from .config import AppConfig
from .models import (
    BugError,
    BugRecord,
    BugStage,
    EnrichedBug,
    UploadResult,
)
from .state import StateStore

log = logging.getLogger(__name__)

JIRA_DESCRIPTION_LIMIT = 32_500


# ---------------------------------------------------------------------------
# Markdown → Jira wiki — preserved verbatim from the legacy uploader.
# JiraClient also ships a converter, but f2j's emits a slightly different
# subset (numbered lists, bullet depth from indent count). Keeping f2j's so
# pre-merge byte-equivalence holds.
# ---------------------------------------------------------------------------

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_MD_BULLET_RE = re.compile(r"^(\s*)[-*]\s+(.+)$")
_MD_NUMBERED_RE = re.compile(r"^(\s*)\d+\.\s+(.+)$")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")


def _md_inline_to_wiki(line: str) -> str:
    line = _MD_BOLD_RE.sub(r"*\1*", line)
    line = _MD_INLINE_CODE_RE.sub(r"{{\1}}", line)
    return line


def markdown_to_jira_wiki(md: str) -> str:
    """Convert Markdown to Jira Server/DC wiki markup (f2j flavor)."""
    out: list[str] = []
    in_fence = False
    for raw_line in md.splitlines():
        stripped = raw_line.lstrip()
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
            depth = max(1, len(m.group(1)) // 2 + 1)
            out.append(f"{'*' * depth} {_md_inline_to_wiki(m.group(2))}")
            continue
        m = _MD_NUMBERED_RE.match(raw_line)
        if m:
            depth = max(1, len(m.group(1)) // 2 + 1)
            out.append(f"{'#' * depth} {_md_inline_to_wiki(m.group(2))}")
            continue
        out.append(_md_inline_to_wiki(raw_line))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# JiraClient construction (bridges f2j config → drive's env-driven from_env)
# ---------------------------------------------------------------------------

def _build_jira_client(cfg: AppConfig) -> JiraClient:
    """Build a JiraClient honoring f2j's config without disturbing the env."""
    host = _normalize_host(cfg.jira.url or "")
    if not host:
        raise RuntimeError("cfg.jira.url is not configured")
    token = os.environ.get("JIRA_PAT") or os.environ.get("JIRA_TOKEN")
    if not token:
        # Fall back to the autodev token chain inside JiraClient by setting
        # JIRA_PROJECT_KEY temporarily — but only consult the file system,
        # don't mutate the parent env permanently. Easier: import the
        # token loader directly.
        from jira_task_agent.jira.client import _load_token
        prior = os.environ.get("JIRA_PROJECT_KEY")
        os.environ["JIRA_PROJECT_KEY"] = cfg.jira.project_key or "unknown"
        try:
            token = _load_token()
        finally:
            if prior is None:
                os.environ.pop("JIRA_PROJECT_KEY", None)
            else:
                os.environ["JIRA_PROJECT_KEY"] = prior
    auth_mode = cfg.jira.auth_mode or "bearer"
    # _build_auth_header reads JIRA_AUTH_MODE / JIRA_USER_EMAIL from env;
    # bridge cfg into env temporarily so it works for "basic" Cloud auth.
    prior_mode = os.environ.get("JIRA_AUTH_MODE")
    prior_email = os.environ.get("JIRA_USER_EMAIL")
    os.environ["JIRA_AUTH_MODE"] = auth_mode
    if cfg.jira.user_email:
        os.environ["JIRA_USER_EMAIL"] = cfg.jira.user_email
    try:
        auth_header = _build_auth_header(token)
    finally:
        if prior_mode is None:
            os.environ.pop("JIRA_AUTH_MODE", None)
        else:
            os.environ["JIRA_AUTH_MODE"] = prior_mode
        if cfg.jira.user_email and prior_email is None:
            os.environ.pop("JIRA_USER_EMAIL", None)
    return JiraClient(host=host, auth_header=auth_header, auth_mode=auth_mode)


def _issue_browse_url(client: JiraClient, key: str) -> str:
    return f"https://{client.host}/browse/{key}"


# ---------------------------------------------------------------------------
# Sink construction (composes f2j strategies)
# ---------------------------------------------------------------------------

def _build_sink(cfg: AppConfig, client: JiraClient) -> tuple[JiraSink, PickerWithCacheStrategy]:
    """Build a JiraSink wired to f2j's strategies.

    Returns ``(sink, picker_strategy)`` so the caller can ``picker.save()``
    after the run to persist newly-cached usernames.
    """
    available_epics = {e.key for e in cfg.jira.available_epics} if cfg.jira.available_epics else set()
    epic_router = DeterministicChainStrategy(
        prefix_map=cfg.jira.external_id_prefix_to_epic,
        module_map=cfg.jira.module_to_epic,
        available_epics=available_epics,
        default_epic=cfg.jira.default_epic,
    )
    picker = PickerWithCacheStrategy(
        client=client,
        user_map_path=cfg.jira.user_map_path,
        unknown_policy=cfg.jira.unknown_assignee_policy,
        default_username=cfg.jira.default_assignee,
    )
    sink = JiraSink(
        client=client,
        project_key=cfg.jira.project_key or "",
        assignee_resolver=picker,
        epic_router=epic_router,
        # f2j convention: Bug tickets don't carry the `ai-generated` label
        # (the upstream:<id> label is the marker that matters).
        add_ai_generated_label=False,
    )
    return sink, picker


# ---------------------------------------------------------------------------
# Ticket construction
# ---------------------------------------------------------------------------

def _resolve_assignee_display_name(
    record: BugRecord, cfg: AppConfig
) -> str | None:
    """Walk f2j's assignee priority chain — return the FIRST candidate.

    Order: enriched.assignee_hint > parsed.hinted_assignee > module routing >
    default. The :class:`PickerWithCacheStrategy` resolves whatever name we
    pick to a Jira username; we let it own the resolution outcome (including
    the unknown-policy fallback to ``default_assignee``).
    """
    enriched = record.enriched
    candidates = [
        enriched.assignee_hint if enriched else None,
        record.parsed.hinted_assignee,
        _module_assignee(cfg, record),
    ]
    for c in candidates:
        if c:
            return c
    # Falling through — the picker's `unknown_policy="default"` will route
    # to default_assignee on miss, so we don't need to set it here.
    return None


def _module_assignee(cfg: AppConfig, record: BugRecord) -> str | None:
    alias = record.parsed.inherited_module.repo_alias
    if alias and alias in cfg.jira.module_to_assignee:
        return cfg.jira.module_to_assignee[alias]
    return None


def _compose_labels(
    enriched: EnrichedBug, cfg: AppConfig, record: BugRecord, label: str
) -> list[str]:
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


def _compose_components(enriched: EnrichedBug, cfg: AppConfig, record: BugRecord) -> list[str]:
    components = list(enriched.components or [])
    alias = record.parsed.inherited_module.repo_alias
    if alias and alias in cfg.jira.module_to_component:
        cn = cfg.jira.module_to_component[alias]
        if cn not in components:
            components.append(cn)
    for default_component in cfg.jira.default_components:
        if default_component not in components:
            components.append(default_component)
    return components


def _ticket_for(record: BugRecord, cfg: AppConfig, label: str) -> Ticket:
    """Build a Ticket from an enriched bug. JiraSink handles routing/filtering."""
    enriched: EnrichedBug = record.enriched  # type: ignore[assignment]
    description = markdown_to_jira_wiki(enriched.description_md)
    if len(description) > JIRA_DESCRIPTION_LIMIT:
        description = (
            description[:JIRA_DESCRIPTION_LIMIT]
            + "\n\n... (truncated; full text in state.json)"
        )

    custom_fields: dict[str, Any] = {}
    # Wire context so DeterministicChainStrategy can route epic-link.
    if record.parsed.external_id:
        custom_fields["external_id"] = record.parsed.external_id
    if record.parsed.inherited_module.repo_alias:
        custom_fields["module"] = record.parsed.inherited_module.repo_alias
    if enriched.epic_key:
        custom_fields["llm_epic_pick"] = enriched.epic_key
    # External-id customfield (cfg-driven).
    if cfg.jira.external_id_field and record.parsed.external_id:
        custom_fields[cfg.jira.external_id_field] = record.parsed.external_id
    # User-configured field_map → values from the enriched record.
    field_map_for_type = cfg.jira.field_map.get(cfg.jira.issue_type.lower(), {})
    for logical, fid in field_map_for_type.items():
        if logical in {"summary", "description", "priority"}:
            continue
        value = _read_logical_field(enriched, record, logical)
        if value is not None:
            custom_fields[fid] = value

    return Ticket(
        summary=enriched.summary,
        description=description,
        type=cfg.jira.issue_type,
        assignee=_resolve_assignee_display_name(record, cfg),
        labels=_compose_labels(enriched, cfg, record, label),
        components=_compose_components(enriched, cfg, record),
        priority=cfg.jira.priority_values.get(enriched.priority, enriched.priority),
        custom_fields=custom_fields,
    )


def _read_logical_field(enriched: EnrichedBug, record: BugRecord, logical: str) -> Any:
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
# Entry point
# ---------------------------------------------------------------------------

@dataclass
class _UploadOutcome:
    bug_id: str
    upload: UploadResult | None
    error: BugError | None
    skipped: bool = False
    skip_reason: str | None = None
    dry_run: bool = False


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
    """Upload f2j enriched bugs to Jira, idempotent and resumable."""
    console = console or Console()
    err_console = err_console or Console(stderr=True)
    only = only or set()

    if not cfg.jira.url:
        err_console.print("[red]jira.url is not configured.[/red]")
        return
    if not cfg.jira.project_key:
        err_console.print("[red]jira.project_key is not configured.[/red]")
        return

    if client is None:
        try:
            client = _build_jira_client(cfg)
        except Exception as e:  # noqa: BLE001
            if not dry_run:
                err_console.print(f"[red]could not build JiraClient:[/red] {e}")
                return
            # In dry-run we don't actually need a client; signal that downstream.
            client = None  # type: ignore[assignment]

    store = StateStore(state_file)
    state = store.load()
    targets = _select_targets(state.bugs, only=only, retry_failed=retry_failed)
    if not targets:
        console.print("[yellow]No bugs to upload.[/yellow]")
        return

    sink: JiraSink | None = None
    picker: PickerWithCacheStrategy | None = None
    if client is not None:
        sink, picker = _build_sink(cfg, client)

    counts = {"ok": 0, "skipped": 0, "fail": 0}
    console.rule(
        f"[bold]upload[/bold] ({len(targets)} bugs, "
        f"project={cfg.jira.project_key}, dry_run={dry_run}, concurrency={concurrency})"
    )

    if concurrency <= 1:
        for record in targets:
            outcome = _process_one(record, cfg, sink, dry_run=dry_run)
            _commit_outcome(state, outcome, counts, console)
            store.save(state)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures: dict[Future, BugRecord] = {
                pool.submit(_process_one, rec, cfg, sink, dry_run=dry_run): rec
                for rec in targets
            }
            for fut in as_completed(futures):
                outcome = fut.result()
                _commit_outcome(state, outcome, counts, console)
                store.save(state)

    if picker is not None:
        picker.save()
    _print_summary(counts, console)


# ---------------------------------------------------------------------------
# Selection / outcomes
# ---------------------------------------------------------------------------

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


def _process_one(
    record: BugRecord,
    cfg: AppConfig,
    sink: JiraSink | None,
    *,
    dry_run: bool,
) -> _UploadOutcome:
    bug_id = record.parsed.bug_id

    if record.upload and record.upload.jira_key:
        return _UploadOutcome(
            bug_id=bug_id, upload=None, error=None,
            skipped=True,
            skip_reason=f"already uploaded as {record.upload.jira_key}",
        )

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

    label = (
        f"upstream:{record.parsed.external_id}"
        if record.parsed.external_id
        else f"{cfg.jira.external_id_label_prefix}:{bug_id}"
    )

    if dry_run or sink is None:
        # Build the ticket so the operator can inspect what would be sent
        # (tests + downstream tooling can call _ticket_for / build_issue_payload-like
        # helpers if they need to assert on the payload structure).
        _ticket_for(record, cfg, label)
        return _UploadOutcome(
            bug_id=bug_id, upload=None, error=None, dry_run=True,
        )

    # Idempotency check via the Jira sink.
    strategy = LabelSearchStrategy(
        label_template=label, project_key=cfg.jira.project_key
    )
    ticket = _ticket_for(record, cfg, label)
    try:
        existing = sink.find_existing(ticket, strategy)
    except Exception as e:  # noqa: BLE001
        log.warning("idempotency search failed for %s: %s", bug_id, e)
        existing = None
    if existing:
        return _UploadOutcome(
            bug_id=bug_id,
            upload=UploadResult(
                jira_key=existing,
                jira_url=_issue_browse_url(sink.client, existing),
                uploaded_at=_now_iso(),
            ),
            error=None,
        )

    try:
        key = sink.create(ticket)
    except Exception as e:  # noqa: BLE001
        return _UploadOutcome(
            bug_id=bug_id,
            upload=None,
            error=BugError(
                stage=BugStage.UPLOADING,
                message=f"create failed: {e}",
                occurred_at=_now_iso(),
            ),
        )

    # Parent assignee for subtasks that inherit it.
    parent_username = (
        sink.assignee_resolver.resolve(ticket.assignee)
        if ticket.assignee
        else None
    )
    _create_subtasks(sink, cfg, key, parent_username)
    return _UploadOutcome(
        bug_id=bug_id,
        upload=UploadResult(
            jira_key=key,
            jira_url=_issue_browse_url(sink.client, key),
            uploaded_at=_now_iso(),
        ),
        error=None,
    )


def _create_subtasks(
    sink: JiraSink, cfg: AppConfig, parent_key: str, parent_assignee: str | None
) -> None:
    """Create configured subtasks under the just-created parent ticket."""
    if not cfg.jira.subtasks:
        return
    for template in cfg.jira.subtasks:
        # Subtasks bypass the parent's assignee_resolver because the
        # template's assignee is already a Jira username (per docstring).
        # Pre-resolved by setting ticket.assignee = the literal username
        # and using a passthrough resolver. Easier: build the subtask via
        # JiraSink's lower-level interaction.
        if template.inherit_assignee:
            sub_assignee_username = parent_assignee
        else:
            sub_assignee_username = template.assignee
        try:
            sink.client.create_issue(
                project_key=cfg.jira.project_key or "",
                summary=template.title,
                description=template.description or "",
                issue_type=template.issue_type,
                parent_key=parent_key,
                add_ai_generated_label=False,
                extra_fields=(
                    {"assignee": {"name": sub_assignee_username}}
                    if sub_assignee_username
                    else None
                ),
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "subtask creation failed for parent %s (title=%r): %s",
                parent_key, template.title, e,
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


# ---------------------------------------------------------------------------
# Inspect helper — used by `f2j inspect --bug <id>` to preview the routed
# epic without going to Jira. Mirrors the strategy chain at upload time so
# the operator sees what would actually be sent.
# ---------------------------------------------------------------------------

def _resolve_epic_for_inspect(record: BugRecord, cfg: AppConfig) -> str | None:
    """Apply the same epic-routing chain as the live upload, without I/O."""
    available_epics = (
        {e.key for e in cfg.jira.available_epics}
        if cfg.jira.available_epics
        else set()
    )
    router = DeterministicChainStrategy(
        prefix_map=cfg.jira.external_id_prefix_to_epic,
        module_map=cfg.jira.module_to_epic,
        available_epics=available_epics,
        default_epic=cfg.jira.default_epic,
    )
    label = f"upstream:{record.parsed.external_id or record.parsed.bug_id}"
    ticket = _ticket_for(record, cfg, label)
    return router.route(ticket)
