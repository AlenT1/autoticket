"""Per-bug agent dispatch with bounded concurrency.

For Phase 1, parallelism is implemented via threads (the agent SDK is sync, the
toolkit is sync; running N agent loops concurrently in threads is simpler than
plumbing async through every subprocess call). The Anthropic client itself is
thread-safe.
"""

from __future__ import annotations

import logging
import traceback
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
    IntermediateFile,
)
from ..repocache import RepoCacheManager, default_cache_dir
from ..state import StateStore
from .agent import EnrichmentAgent, EnrichmentError, EnrichmentTruncated
from .cost import estimate_cost_usd
from .failure_class import FailureClass, classify_error
from .linter import LintResult, lint_description
from .tools import Toolkit, build_submit_tool

log = logging.getLogger(__name__)


@dataclass
class _BugOutcome:
    bug_id: str
    enriched: EnrichedBug | None
    error: BugError | None
    lint: LintResult | None


def run_enrich(
    *,
    state_file: Path,
    cfg: AppConfig,
    only: set[str] | None = None,
    retry_failed: bool = False,
    concurrency: int = 1,
    max_turns: int = 20,
    model: str | None = None,
    fix_proposals: str = "strip",
    console: Console | None = None,
    err_console: Console | None = None,
) -> None:
    """Enrich bugs in `state_file`. Saves after each bug for resumability."""
    console = console or Console()
    err_console = err_console or Console(stderr=True)
    only = only or set()

    store = StateStore(state_file)
    state = store.load()

    targets = _select_targets(state, only=only, retry_failed=retry_failed)
    if not targets:
        console.print("[yellow]No bugs to enrich.[/yellow]")
        return

    cache = RepoCacheManager(
        cache_dir=Path(cfg.repo_cache.dir) if cfg.repo_cache.dir else default_cache_dir(),
        aliases=cfg.repo_aliases,
        git_auth=cfg.git_auth,
        clone_depth=cfg.repo_cache.clone_depth,
    )
    toolkit = Toolkit(cache)

    if model:
        chosen_model = model
    elif cfg.enrichment.provider == "openai_compatible":
        chosen_model = cfg.openai_compatible.model
    else:
        chosen_model = cfg.anthropic.model
    counts: dict[str, int] = {"ok": 0, "fail": 0, "skipped_budget": 0}
    budget = cfg.enrichment.max_budget_usd  # None = no cap

    console.rule(
        f"[bold]enrich[/bold] ({len(targets)} bugs, model={chosen_model}, "
        f"concurrency={concurrency}"
        + (f", budget=${budget}" if budget is not None else "")
        + ")"
    )

    enrich_kwargs = {
        "toolkit": toolkit,
        "cfg": cfg,
        "chosen_model": chosen_model,
        "max_turns": max_turns,
        "fix_proposals": fix_proposals,
    }

    if concurrency <= 1:
        cumulative = _run_serial(
            targets, store, state, counts, console, budget, enrich_kwargs
        )
    else:
        cumulative = _run_parallel(
            targets, store, state, counts, console, concurrency, budget, enrich_kwargs
        )

    _print_summary(state, counts, console, cumulative)


def _budget_exceeded(budget: float | None, cumulative: float) -> bool:
    return budget is not None and cumulative >= budget


def _accumulate_cost(prior: float, outcome: "_BugOutcome") -> float:
    """Add this outcome's enrichment cost to the running total."""
    if outcome.enriched is None:
        return prior
    return round(prior + estimate_cost_usd(outcome.enriched.enrichment_meta), 4)


def _run_serial(
    targets: list[BugRecord],
    store: StateStore,
    state: IntermediateFile,
    counts: dict[str, int],
    console: Console,
    budget: float | None,
    enrich_kwargs: dict,
) -> float:
    cumulative = 0.0
    for record in targets:
        if _budget_exceeded(budget, cumulative):
            counts["skipped_budget"] += 1
            continue
        outcome = _enrich_one(record, state=state, **enrich_kwargs)
        _commit_outcome(state, outcome, counts, console)
        cumulative = _accumulate_cost(cumulative, outcome)
        if _budget_exceeded(budget, cumulative):
            console.print(
                f"[yellow]budget cap reached:[/yellow] cumulative "
                f"${cumulative:.4f} >= cap ${budget:.2f}; stopping further bugs."
            )
        store.save(state)
    return cumulative


def _run_parallel(
    targets: list[BugRecord],
    store: StateStore,
    state: IntermediateFile,
    counts: dict[str, int],
    console: Console,
    concurrency: int,
    budget: float | None,
    enrich_kwargs: dict,
) -> float:
    cumulative = 0.0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures: dict[Future, BugRecord] = {
            pool.submit(_enrich_one, rec, state=state, **enrich_kwargs): rec
            for rec in targets
        }
        for rec in targets:
            rec.stage = BugStage.ENRICHING
        store.save(state)
        for fut in as_completed(futures):
            outcome = fut.result()
            _commit_outcome(state, outcome, counts, console)
            cumulative = _accumulate_cost(cumulative, outcome)
            store.save(state)
            if _budget_exceeded(budget, cumulative):
                for f in futures:
                    if not f.done():
                        f.cancel()
                        counts["skipped_budget"] += 1
                console.print(
                    f"[yellow]budget cap reached:[/yellow] cumulative "
                    f"${cumulative:.4f} >= cap ${budget:.2f}; cancelling pending bugs."
                )
                break
    return cumulative


def _matches_only_filter(rec: BugRecord, only: set[str]) -> bool:
    return rec.parsed.bug_id in only or (
        rec.parsed.external_id is not None and rec.parsed.external_id in only
    )


def _eligible_by_stage(rec: BugRecord, retry_failed: bool) -> bool:
    if rec.stage == BugStage.PARSED:
        return True
    return rec.stage == BugStage.FAILED and retry_failed


def _select_targets(
    state: IntermediateFile, *, only: set[str], retry_failed: bool
) -> list[BugRecord]:
    if only:
        return [rec for rec in state.bugs if _matches_only_filter(rec, only)]
    return [rec for rec in state.bugs if _eligible_by_stage(rec, retry_failed)]


def _enrich_one(
    record: BugRecord,
    *,
    state: IntermediateFile,
    toolkit: Toolkit,
    cfg: AppConfig,
    chosen_model: str,
    max_turns: int,
    fix_proposals: str,
) -> _BugOutcome:
    """Run one bug through the agent + linter, returning an outcome."""
    bug_id = record.parsed.bug_id
    submit_tool = build_submit_tool(_resolve_repo_paths(toolkit, record))
    agent = _build_agent(cfg, toolkit, submit_tool, chosen_model, max_turns)
    try:
        enriched = agent.enrich(record.parsed)
    except EnrichmentTruncated as e:
        return _BugOutcome(
            bug_id=bug_id,
            enriched=None,
            error=BugError(
                stage=BugStage.ENRICHING,
                message=f"truncated: {e}",
                failure_class=FailureClass.UNKNOWN.value,
                occurred_at=_now_iso(),
            ),
            lint=None,
        )
    except EnrichmentError as e:
        cls = classify_error(str(e))
        return _BugOutcome(
            bug_id=bug_id,
            enriched=None,
            error=BugError(
                stage=BugStage.ENRICHING,
                message=str(e),
                failure_class=cls.value,
                occurred_at=_now_iso(),
            ),
            lint=None,
        )
    except Exception as e:  # noqa: BLE001 — surface anything to state
        cls = classify_error(f"{type(e).__name__}: {e}")
        return _BugOutcome(
            bug_id=bug_id,
            enriched=None,
            error=BugError(
                stage=BugStage.ENRICHING,
                message=f"{type(e).__name__}: {e}",
                traceback=traceback.format_exc(),
                failure_class=cls.value,
                occurred_at=_now_iso(),
            ),
            lint=None,
        )

    # Linter post-pass.
    lint = lint_description(enriched.description_md, mode=fix_proposals)
    if fix_proposals == "strip" and lint.stripped_lines:
        enriched.description_md = lint.cleaned

    return _BugOutcome(bug_id=bug_id, enriched=enriched, error=None, lint=lint)


def _commit_outcome(
    state: IntermediateFile,
    outcome: _BugOutcome,
    counts: dict[str, int],
    console: Console,
) -> None:
    record = next((r for r in state.bugs if r.parsed.bug_id == outcome.bug_id), None)
    if record is None:
        return
    record.attempts += 1
    if outcome.enriched is not None:
        record.enriched = outcome.enriched
        record.stage = BugStage.ENRICHED
        record.last_error = None
        counts["ok"] += 1
        msg = f"[green]ok[/green]    {outcome.bug_id}  {record.parsed.external_id or ''}"
        if outcome.lint and outcome.lint.stripped_lines:
            msg += f"  [yellow]({len(outcome.lint.stripped_lines)} fix-proposal line(s) stripped)[/yellow]"
        console.print(msg)
    else:
        record.stage = BugStage.FAILED
        record.last_error = outcome.error
        counts["fail"] += 1
        console.print(
            f"[red]fail[/red]  {outcome.bug_id}  {record.parsed.external_id or ''}  "
            f"{outcome.error.message[:120] if outcome.error else ''}"
        )
    state.touch()


def _resolve_repo_paths(toolkit: Toolkit, record: BugRecord) -> dict[str, Path]:
    """Best-effort: clone the inherited module's repo so submit can validate paths."""
    paths: dict[str, Path] = {}
    alias = record.parsed.inherited_module.repo_alias
    if alias and alias in toolkit.cache.aliases:
        try:
            info = toolkit.cache.ensure_clone(alias)
            paths[alias] = info.local_path
        except Exception as e:  # noqa: BLE001
            log.warning("could not pre-clone %s: %s", alias, e)
    return paths


def _print_summary(
    state: IntermediateFile,
    counts: dict[str, int],
    console: Console,
    cumulative_cost_usd: float = 0.0,
) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("metric")
    table.add_column("count", justify="right")
    table.add_row("enriched ok", str(counts["ok"]))
    table.add_row("failed", str(counts["fail"]))
    if counts.get("skipped_budget", 0) > 0:
        table.add_row("skipped (budget cap)", str(counts["skipped_budget"]))
    table.add_row("total bugs in state", str(len(state.bugs)))
    table.add_row("estimated cost (USD)", f"${cumulative_cost_usd:.4f}")
    console.rule("[bold]summary[/bold]")
    console.print(table)


def _build_agent(
    cfg: AppConfig,
    toolkit: Toolkit,
    submit_tool,
    chosen_model: str,
    max_turns: int,
):
    """Pick the agent backend by ``cfg.enrichment.provider`` and construct it.

    The :class:`_shared.llm.LLMProvider` is constructed via the registry and
    injected into the agent — so the agent itself owns only loop semantics,
    not SDK/auth plumbing.
    """
    from _shared.llm import get_provider

    provider_name = cfg.enrichment.provider
    available_epics = list(cfg.jira.available_epics) if cfg.jira.available_epics else None

    if provider_name == "openai_compatible":
        from .agent_openai import OpenAIEnrichmentAgent

        oc = cfg.openai_compatible
        provider = get_provider(
            "openai_compatible",
            base_url=oc.base_url,
            base_url_env=oc.base_url_env,
            api_key_env=oc.api_key_env,
        )
        return OpenAIEnrichmentAgent(
            toolkit=toolkit,
            submit_tool=submit_tool,
            provider=provider,
            model=chosen_model,
            max_turns=max_turns,
            max_tokens_per_turn=oc.max_tokens_per_turn,
            temperature=oc.temperature,
            available_epics=available_epics,
        )
    if provider_name == "anthropic":
        provider = get_provider(
            "anthropic",
            base_url=cfg.anthropic.base_url,
            api_key_env=cfg.anthropic.auth_token_env,
        )
        return EnrichmentAgent(
            toolkit=toolkit,
            submit_tool=submit_tool,
            provider=provider,
            model=chosen_model,
            max_turns=max_turns,
            enable_prompt_caching=cfg.anthropic.enable_prompt_caching,
            available_epics=available_epics,
        )
    raise ValueError(
        f"unknown enrichment.provider {provider_name!r} "
        "(expected 'anthropic' or 'openai_compatible')"
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
