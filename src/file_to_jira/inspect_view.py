"""state.json inspection: summary view + per-bug detail view.

Named `inspect_view` to avoid colliding with the stdlib `inspect` module.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from .models import BugRecord, IntermediateFile

_ROW_LIMIT = 50


def print_summary(state: IntermediateFile, bugs: list[BugRecord], console: Console) -> None:
    console.print(f"[bold]Source:[/bold]   {state.source_file}")
    console.print(f"[bold]Run ID:[/bold]   {state.run_id}")
    console.print(f"[bold]Updated:[/bold]  {state.updated_at}")
    console.print(f"[bold]Bugs:[/bold]     {len(state.bugs)} total, {len(bugs)} after filter")
    console.print()

    counts: dict[str, int] = {}
    for b in bugs:
        counts[b.stage.value] = counts.get(b.stage.value, 0) + 1
    if counts:
        counts_table = Table(show_header=True, header_style="bold")
        counts_table.add_column("Stage")
        counts_table.add_column("Count", justify="right")
        for stage_name, count in counts.items():
            counts_table.add_row(stage_name, str(count))
        console.print(counts_table)
        console.print()

    list_table = Table(show_header=True, header_style="bold")
    list_table.add_column("bug_id", style="cyan", no_wrap=True)
    list_table.add_column("external_id")
    list_table.add_column("stage")
    list_table.add_column("priority")
    list_table.add_column("repo")
    list_table.add_column("title")
    for b in bugs[:_ROW_LIMIT]:
        list_table.add_row(
            b.parsed.bug_id,
            b.parsed.external_id or "-",
            b.stage.value,
            b.parsed.hinted_priority or "-",
            b.parsed.inherited_module.repo_alias or "-",
            (b.parsed.raw_title[:60] + "...")
            if len(b.parsed.raw_title) > 60
            else b.parsed.raw_title,
        )
    console.print(list_table)
    if len(bugs) > _ROW_LIMIT:
        console.print(
            f"[dim]... and {len(bugs) - _ROW_LIMIT} more "
            f"(use --bug <id> to see details for one)[/dim]"
        )


def print_detail(
    record: BugRecord,
    console: Console,
    *,
    show_stripped: bool = False,
    epic_lookup: dict[str, str] | None = None,
    resolved_epic: str | None = None,
) -> None:
    p = record.parsed
    console.print(f"[bold cyan]bug_id:[/bold cyan]         {p.bug_id}")
    console.print(f"[bold cyan]external_id:[/bold cyan]    {p.external_id or '(none)'}")
    console.print(f"[bold cyan]stage:[/bold cyan]          {record.stage.value}")
    console.print(f"[bold cyan]title:[/bold cyan]          {p.raw_title}")
    console.print(f"[bold cyan]priority:[/bold cyan]       {p.hinted_priority or '(unset)'}")
    console.print(f"[bold cyan]assignee:[/bold cyan]       {p.hinted_assignee or '(unset)'}")
    console.print(f"[bold cyan]closed_section:[/bold cyan] {p.closed_section}")
    console.print(f"[bold cyan]labels:[/bold cyan]         {', '.join(p.labels) or '(none)'}")
    mod = p.inherited_module
    console.print(
        f"[bold cyan]repo:[/bold cyan]           "
        f"{mod.repo_alias or '(none)'} @ {mod.branch or '?'} "
        f"({mod.commit_sha or '?'})"
    )
    console.print(
        f"[bold cyan]hinted_files:[/bold cyan]   "
        f"{', '.join(p.hinted_files) if p.hinted_files else '(none)'}"
    )
    console.print(
        f"[bold cyan]source_lines:[/bold cyan]   {p.source_line_start}-{p.source_line_end}"
    )
    console.print(f"[bold cyan]attempts:[/bold cyan]       {record.attempts}")
    console.print()
    console.rule("[bold]Body[/bold]")
    console.print(p.raw_body)

    if record.enriched is not None:
        console.print()
        console.rule("[bold green]Enriched[/bold green]")
        e = record.enriched
        console.print(f"[bold]Summary:[/bold]    {e.summary}")
        console.print(f"[bold]Priority:[/bold]   {e.priority}")
        epic_label = "(none)"
        if e.epic_key:
            summary = (epic_lookup or {}).get(e.epic_key)
            epic_label = f"{e.epic_key} ({summary})" if summary else e.epic_key
        console.print(f"[bold]Epic (LLM pick):[/bold]   {epic_label}")
        if resolved_epic and resolved_epic != e.epic_key:
            resolved_summary = (epic_lookup or {}).get(resolved_epic)
            resolved_label = (
                f"{resolved_epic} ({resolved_summary})" if resolved_summary else resolved_epic
            )
            console.print(
                f"[bold]Epic (will use):[/bold]  [green]{resolved_label}[/green] "
                f"[dim](deterministic rule overrides LLM pick at upload)[/dim]"
            )
        elif resolved_epic and not e.epic_key:
            resolved_summary = (epic_lookup or {}).get(resolved_epic)
            resolved_label = (
                f"{resolved_epic} ({resolved_summary})" if resolved_summary else resolved_epic
            )
            console.print(f"[bold]Epic (will use):[/bold]  {resolved_label}")
        console.print(f"[bold]Components:[/bold] {', '.join(e.components) or '(none)'}")
        console.print(f"[bold]Refs:[/bold]       {len(e.code_references)} file references")
        console.print()
        console.print(e.description_md)
        console.print()
        meta = e.enrichment_meta
        console.print(
            f"[dim]model={meta.model} tools={meta.tool_calls} "
            f"in={meta.input_tokens} out={meta.output_tokens} "
            f"cache_read={meta.cache_read_tokens}[/dim]"
        )

    if record.upload is not None:
        console.print()
        console.rule("[bold]Jira[/bold]")
        console.print(f"  {record.upload.jira_key}  {record.upload.jira_url}")
        console.print(f"  uploaded_at: {record.upload.uploaded_at}")

    if show_stripped and p.removed_fix_text:
        console.print()
        console.rule("[bold yellow]Removed fix-proposal text[/bold yellow]")
        console.print(p.removed_fix_text)

    if record.last_error is not None:
        console.print()
        console.rule(f"[bold red]Last error ({record.last_error.stage.value})[/bold red]")
        console.print(record.last_error.message)
        if record.last_error.traceback:
            console.print(f"[dim]{record.last_error.traceback}[/dim]")
