"""file-to-jira-tickets CLI (Typer)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Windows console is CP-1252 by default; reconfigure to UTF-8 so Rich glyphs don't
# crash with UnicodeEncodeError. No-op on already-UTF-8 streams.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import (
    SECRET_ENV_VARS,
    AppConfig,
    config_paths,
    get_secret,
    load_config,
    redact_secret,
)
from .logging import configure_logging, get_logger

app = typer.Typer(
    name="f2j",
    help="Parse markdown bug lists and upload them as Jira tickets, enriched by AI agents.",
    no_args_is_help=True,
    add_completion=False,
)
jira_app = typer.Typer(name="jira", help="Jira diagnostics.", no_args_is_help=True)
repo_cache_app = typer.Typer(
    name="repo-cache", help="Repo cache management.", no_args_is_help=True
)
app.add_typer(jira_app, name="jira")
app.add_typer(repo_cache_app, name="repo-cache")

console = Console()
err_console = Console(stderr=True)


def _stub(step: int, what: str) -> None:
    err_console.print(f"[yellow]'{what}' is not yet implemented (Step {step}).[/yellow]")
    raise typer.Exit(code=2)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"f2j {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    # Body is intentionally empty: this callback only exists so Typer registers
    # `--version` at the root. The eager callback handles exit before we reach here.
    return


@app.command()
def parse(
    input_file: Optional[Path] = typer.Argument(
        None,
        help="Path to a local markdown file (when --source file, the default).",
    ),
    out: Path = typer.Option(Path("state.json"), "--out", "-o"),
    config: Optional[Path] = typer.Option(None, "--config"),
    force: bool = typer.Option(False, "--force"),
    include_resolved: bool = typer.Option(False, "--include-resolved"),
    source: str = typer.Option(
        "file",
        "--source",
        help="Where to read the bug list from: 'file' (positional path), "
        "'gdrive' (uses $FOLDER_ID + Google OAuth, requires --only), or "
        "'local' (uses --local-dir, requires --only).",
    ),
    only: Optional[str] = typer.Option(
        None,
        "--only",
        help="Filename to pick from the source. Required with --source gdrive/local.",
    ),
    folder_id: Optional[str] = typer.Option(
        None,
        "--folder-id",
        help="Google Drive folder UUID (default: $FOLDER_ID env var). "
        "Used only with --source gdrive.",
    ),
    local_dir: Path = typer.Option(
        Path("data/local_files"),
        "--local-dir",
        help="Local folder to scan when --source local.",
    ),
    download_dir: Path = typer.Option(
        Path("data/gdrive_files"),
        "--download-dir",
        help="Cache dir for Drive downloads when --source gdrive.",
    ),
) -> None:
    """Parse a markdown bug list into a structured state.json.

    Three input sources, sharing the same Source protocol that
    jira-task-agent uses:

    \b
    --source file   (default): pass a positional path to a local .md file.
    --source gdrive          : reads from a Google Drive folder (FOLDER_ID
                               env var or --folder-id), requires --only to
                               pick one file. Needs credentials.json + token.json.
    --source local           : scans --local-dir (default data/local_files),
                               requires --only to pick one file.
    """
    import os
    import uuid

    from _shared.io.sources import GDriveSource, LocalFolderSource

    from .input import F2JFileSource
    from .models import BugRecord, BugStage, IntermediateFile
    from .parse import ParseError, parse_markdown
    from .state import save_state

    configure_logging()
    log = get_logger("parse")

    if out.exists() and not force:
        err_console.print(
            f"[red]Refusing to overwrite[/red] {out} (use --force to override)"
        )
        raise typer.Exit(code=1)

    # Pick the source per --source flag.
    src, source_label = _build_parse_source(
        source=source,
        input_file=input_file,
        only=only,
        folder_id=folder_id,
        local_dir=local_dir,
        download_dir=download_dir,
    )

    try:
        documents = list(src.iter_documents(only=only))
    except ParseError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e
    except FileNotFoundError as e:
        # GDrive auth failure (credentials.json missing) lands here.
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e

    if not documents:
        err_console.print(
            f"[red]No documents found via --source {source}"
            + (f" (only={only!r})" if only else "")
            + ".[/red]"
        )
        raise typer.Exit(code=1)

    if len(documents) > 1:
        console.print(
            f"[yellow]warn:[/yellow] {len(documents)} documents matched; "
            f"parsing the first ({documents[0].name!r}). Pass --only to scope explicitly."
        )

    doc = documents[0]
    # SingleFile / F2JFileSource expose `content_sha256` in metadata; gdrive /
    # local fall back to a hash of the decoded content.
    source_sha = doc.metadata.get("content_sha256")
    if source_sha is None:
        import hashlib
        source_sha = hashlib.sha256(doc.content.encode("utf-8")).hexdigest()

    try:
        result = parse_markdown(doc.content, source_sha256=source_sha)
    except ParseError as e:
        err_console.print(f"[red]Parse error: {e}[/red]")
        raise typer.Exit(code=1) from e

    if result.warnings:
        for w in result.warnings:
            console.print(f"[yellow]warn:[/yellow] {w}")

    cfg = load_config(config)

    selected = [
        b for b in result.bugs if include_resolved or not b.closed_section
    ]
    skipped_closed = len(result.bugs) - len(selected)

    bug_records = [
        BugRecord(stage=BugStage.PARSED, parsed=b) for b in selected
    ]

    state = IntermediateFile(
        run_id=str(uuid.uuid4()),
        source_file=source_label,
        source_file_sha256=source_sha,
        config_snapshot={
            "anthropic_model": cfg.anthropic.model,
            "fix_proposals": cfg.enrichment.fix_proposals,
            "jira_project_key": cfg.jira.project_key,
            "jira_issue_type": cfg.jira.issue_type,
            "include_resolved": include_resolved,
        },
        bugs=bug_records,
    )
    save_state(state, out)

    console.rule("[bold]parse[/bold]")
    console.print(f"  [green]parsed[/green]   {len(result.bugs)} bug entries")
    console.print(f"  [cyan]selected[/cyan] {len(selected)} (open={len(selected)})")
    if skipped_closed > 0:
        console.print(
            f"  [dim]skipped[/dim]  {skipped_closed} closed-section bugs "
            f"(use --include-resolved to include)"
        )
    if result.warnings:
        console.print(f"  [yellow]warnings[/yellow] {len(result.warnings)}")
    console.print(f"  [bold]wrote[/bold]    {out}")

    log.info(
        "parse complete",
        total=len(result.bugs),
        selected=len(selected),
        skipped_closed=skipped_closed,
        warnings=len(result.warnings),
        out=str(out),
    )


def _build_parse_source(
    *,
    source: str,
    input_file: Optional[Path],
    only: Optional[str],
    folder_id: Optional[str],
    local_dir: Path,
    download_dir: Path,
):
    """Pick the right Source impl per --source, validating CLI args.

    Returns ``(source_impl, source_label)`` where ``source_label`` is what
    gets stored as ``state.source_file`` (a path for file source, a
    ``"gdrive::folder/<id>"`` style URI for drive, etc.).

    Exits the process on misconfiguration.
    """
    import os

    from _shared.io.sources import GDriveSource, LocalFolderSource

    from .input import F2JFileSource

    if source == "file":
        if input_file is None:
            err_console.print(
                "[red]--source file requires a positional INPUT_FILE path.[/red]"
            )
            raise typer.Exit(code=1)
        if not input_file.exists():
            err_console.print(f"[red]{input_file} does not exist.[/red]")
            raise typer.Exit(code=1)
        return F2JFileSource(input_file), str(input_file)

    if source == "gdrive":
        fid = folder_id or os.environ.get("FOLDER_ID")
        if not fid:
            err_console.print(
                "[red]--source gdrive requires --folder-id or $FOLDER_ID.[/red]"
            )
            raise typer.Exit(code=1)
        if not only:
            err_console.print(
                "[red]--source gdrive requires --only <filename>.[/red]"
            )
            raise typer.Exit(code=1)
        return (
            GDriveSource(folder_id=fid, download_dir=download_dir),
            f"gdrive::folder/{fid}/{only}",
        )

    if source == "local":
        if not only:
            err_console.print(
                "[red]--source local requires --only <filename>.[/red]"
            )
            raise typer.Exit(code=1)
        return LocalFolderSource(local_dir), f"local::{local_dir}/{only}"

    err_console.print(
        f"[red]unknown --source {source!r} (expected 'file', 'gdrive', or 'local').[/red]"
    )
    raise typer.Exit(code=1)


@app.command()
def enrich(
    state_file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    concurrency: int = typer.Option(8, "--concurrency"),
    only: list[str] = typer.Option([], "--only"),
    retry_failed: bool = typer.Option(False, "--retry-failed"),
    max_turns: int = typer.Option(20, "--max-turns"),
    model: Optional[str] = typer.Option(None, "--model"),
    fix_proposals: str = typer.Option("strip", "--fix-proposals"),
) -> None:
    """Run AI enrichment on parsed bugs.

    Step 5 implementation: serial execution. Step 6 layers in bounded-concurrency
    parallelism, retry, and the post-hoc fix-language linter.
    """
    from .enrich.orchestrator import run_enrich

    configure_logging()
    cfg = load_config()
    run_enrich(
        state_file=state_file,
        cfg=cfg,
        only=set(only),
        retry_failed=retry_failed,
        concurrency=concurrency,
        max_turns=max_turns,
        model=model,
        fix_proposals=fix_proposals,
        console=console,
        err_console=err_console,
    )


@app.command()
def upload(
    state_file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    concurrency: int = typer.Option(8, "--concurrency"),
    only: list[str] = typer.Option([], "--only"),
    retry_failed: bool = typer.Option(False, "--retry-failed"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Upload enriched bugs to Jira."""
    from .upload import upload_state

    configure_logging()
    cfg = load_config()
    upload_state(
        state_file=state_file,
        cfg=cfg,
        only=set(only),
        retry_failed=retry_failed,
        dry_run=dry_run,
        concurrency=concurrency,
        console=console,
        err_console=err_console,
    )


@app.command()
def run(
    input_file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    out: Path = typer.Option(Path("state.json"), "--out", "-o"),
    upload_after: bool = typer.Option(True, "--upload/--no-upload"),
    include_resolved: bool = typer.Option(False, "--include-resolved"),
    concurrency: int = typer.Option(4, "--concurrency"),
    dry_run_upload: bool = typer.Option(False, "--dry-run-upload"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """End-to-end: parse -> enrich -> upload."""
    from .enrich.orchestrator import run_enrich
    from .upload import upload_state

    configure_logging()
    cfg = load_config()

    # Step 1: parse
    parse(
        input_file=input_file,
        out=out,
        config=None,
        force=force,
        include_resolved=include_resolved,
    )

    # Step 2: enrich
    run_enrich(
        state_file=out,
        cfg=cfg,
        concurrency=concurrency,
        max_turns=cfg.enrichment.max_turns,
        model=None,
        fix_proposals=cfg.enrichment.fix_proposals,
        console=console,
        err_console=err_console,
    )

    # Step 3: upload
    if upload_after:
        upload_state(
            state_file=out,
            cfg=cfg,
            dry_run=dry_run_upload,
            concurrency=concurrency,
            console=console,
            err_console=err_console,
        )


@app.command()
def inspect(
    state_file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    bug: Optional[str] = typer.Option(None, "--bug", help="Show one bug by bug_id or external_id."),
    stage: Optional[str] = typer.Option(None, "--stage", help="Filter by stage."),
    show_stripped: bool = typer.Option(
        False, "--show-stripped", help="Include removed fix-proposal text."
    ),
) -> None:
    """Inspect a state.json file."""
    from .inspect_view import print_detail, print_summary
    from .models import BugStage
    from .state import StateFileCorruptError, load_state

    configure_logging()

    try:
        state = load_state(state_file)
    except StateFileCorruptError as e:
        err_console.print(f"[red]Corrupt state file:[/red] {e}")
        raise typer.Exit(code=1) from e

    if bug is not None:
        match = next(
            (
                r
                for r in state.bugs
                if r.parsed.bug_id == bug or r.parsed.external_id == bug
            ),
            None,
        )
        if match is None:
            err_console.print(f"[red]No bug found matching '{bug}'[/red]")
            raise typer.Exit(code=1)
        from .upload import _resolve_epic_for_inspect as _resolve_epic

        cfg = load_config()
        epic_lookup = {e.key: e.summary for e in cfg.jira.available_epics}
        resolved_epic = _resolve_epic(match, cfg)
        print_detail(
            match, console, show_stripped=show_stripped,
            epic_lookup=epic_lookup, resolved_epic=resolved_epic,
        )
        return

    bugs = state.bugs
    if stage is not None:
        try:
            target = BugStage(stage)
        except ValueError as e:
            valid = ", ".join(s.value for s in BugStage)
            err_console.print(f"[red]Invalid stage '{stage}'. Valid: {valid}[/red]")
            raise typer.Exit(code=1) from e
        bugs = [b for b in bugs if b.stage == target]

    print_summary(state, bugs, console)


@app.command(name="validate-config")
def validate_config(
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Resolve and validate configuration; print redacted summary."""
    configure_logging()
    log = get_logger("validate_config")

    try:
        cfg = load_config(config)
    except Exception as e:
        err_console.print(f"[red]Failed to load config: {e}[/red]")
        raise typer.Exit(code=1) from e

    console.rule("[bold]Config files seen[/bold]")
    for p in config_paths(config):
        present = "[green]ok[/green]" if p.exists() else "[dim] - [/dim]"
        console.print(f"  {present}  {p}")

    console.rule("[bold]Resolved configuration[/bold]")
    _print_config_summary(cfg)

    console.rule("[bold]Secrets (env vars)[/bold]")
    missing = _print_secrets_table(cfg)

    console.rule("[bold]External tools[/bold]")
    _print_tools_table()

    console.rule("[bold]CLI cross-checks (optional)[/bold]")
    _cli_cross_checks()

    console.rule("[bold]Phase 1 readiness[/bold]")
    blockers = _phase1_readiness(cfg, missing, console)

    if missing:
        console.print(
            f"\n[yellow]Warning:[/yellow] {len(missing)} secret(s) missing: "
            f"{', '.join(missing)}. Set them in `.env` or your shell environment."
        )
        log.warning("validate_config: missing secrets", missing=missing)
    if blockers:
        console.print(
            f"[yellow]{len(blockers)} item(s) still need configuration before "
            "you can run end-to-end.[/yellow] See TROUBLESHOOTING.md."
        )
    elif not missing:
        console.print(
            "\n[green]All set.[/green] You can run "
            "`f2j parse <input.md>` and proceed through enrich/upload."
        )
        log.info("validate_config: all secrets and config present")


def _check_enrich_readiness(
    cfg: AppConfig, missing_secrets: list[str]
) -> tuple[bool, list[str]]:
    """Check whether the enrich stage has its credentials. Returns (ok, blockers)."""
    if cfg.enrichment.provider == "openai_compatible":
        env_name = cfg.openai_compatible.api_key_env
        if not get_secret(env_name):
            return False, [f"{env_name} (for openai_compatible enrichment)"]
        return True, []
    if "ANTHROPIC_API_KEY" in missing_secrets:
        return False, ["ANTHROPIC_API_KEY (for enrichment)"]
    return True, []


def _check_upload_readiness(
    cfg: AppConfig, missing_secrets: list[str]
) -> tuple[bool, list[str]]:
    """Check whether the upload stage has Jira config + creds. Returns (ok, blockers)."""
    blockers: list[str] = []
    if "JIRA_PAT" in missing_secrets:
        blockers.append("JIRA_PAT (for upload)")
    if not cfg.jira.url:
        blockers.append("jira.url (for upload)")
    if not cfg.jira.project_key:
        blockers.append("jira.project_key (for upload)")
    if not cfg.jira.field_map:
        blockers.append("jira.field_map (run `f2j jira fields --project <KEY>`)")
    return (not blockers, blockers)


def _phase1_readiness(cfg: AppConfig, missing_secrets: list[str], console: Console) -> list[str]:
    """Print a per-stage readiness checklist; return list of unmet blockers."""
    parse_ok = True
    enrich_ok, enrich_blockers = _check_enrich_readiness(cfg, missing_secrets)
    upload_ok, upload_blockers = _check_upload_readiness(cfg, missing_secrets)
    blockers = enrich_blockers + upload_blockers

    if not cfg.repo_aliases:
        # Not strictly a blocker: agent can run without repos, just can't browse code.
        console.print(
            "  [dim]note:[/dim] no repo_aliases configured — agent can't browse code."
        )

    def _row(label: str, ready: bool, why: str | None = None) -> None:
        marker = "[green]ready[/green]" if ready else "[yellow]not ready[/yellow]"
        suffix = f"  [dim]({why})[/dim]" if why else ""
        console.print(f"  {marker}  {label}{suffix}")

    _row("parse", parse_ok)
    _enrich_why = None
    if not enrich_ok:
        _enrich_why = (
            f"needs {cfg.openai_compatible.api_key_env}"
            if cfg.enrichment.provider == "openai_compatible"
            else "needs ANTHROPIC_API_KEY"
        )
    _row("enrich", enrich_ok, _enrich_why)
    _row(
        "upload",
        upload_ok,
        None if upload_ok else "needs JIRA_PAT + jira.url + project_key + field_map",
    )
    return blockers


def _print_config_summary(cfg: AppConfig) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Key", style="cyan", no_wrap=True)
    table.add_column("Value")

    table.add_row("enrichment.provider", cfg.enrichment.provider)
    if cfg.enrichment.provider == "openai_compatible":
        oc = cfg.openai_compatible
        table.add_row("openai_compatible.base_url", oc.base_url)
        table.add_row("openai_compatible.api_key_env", oc.api_key_env)
        table.add_row("openai_compatible.model", oc.model)
    else:
        table.add_row("anthropic.model", cfg.anthropic.model)
        table.add_row(
            "anthropic.base_url",
            cfg.anthropic.base_url or "[dim](SDK default — api.anthropic.com)[/dim]",
        )
        table.add_row(
            "anthropic.auth_token_env",
            cfg.anthropic.auth_token_env or "[dim](SDK default — ANTHROPIC_API_KEY)[/dim]",
        )
        table.add_row(
            "anthropic.enable_prompt_caching", str(cfg.anthropic.enable_prompt_caching)
        )
    table.add_row("enrichment.concurrency", str(cfg.enrichment.concurrency))
    table.add_row("enrichment.fix_proposals", cfg.enrichment.fix_proposals)
    table.add_row("repo_cache.dir", str(cfg.repo_cache.dir or "(OS default)"))
    table.add_row("repo_cache.max_size_gb", str(cfg.repo_cache.max_size_gb))
    table.add_row("git_auth.ca_bundle", str(cfg.git_auth.ca_bundle or "(unset)"))
    table.add_row("jira.url", str(cfg.jira.url or "[yellow](unset)[/yellow]"))
    table.add_row("jira.project_key", str(cfg.jira.project_key or "[yellow](unset)[/yellow]"))
    table.add_row("jira.issue_type", cfg.jira.issue_type)
    table.add_row(
        "jira.auth_mode",
        f"{cfg.jira.auth_mode}"
        + (f" (email: {cfg.jira.user_email})" if cfg.jira.auth_mode == "basic" else ""),
    )
    table.add_row("jira.unknown_assignee_policy", cfg.jira.unknown_assignee_policy)
    table.add_row(
        "repo_aliases",
        ", ".join(cfg.repo_aliases.keys()) if cfg.repo_aliases else "[yellow](none)[/yellow]",
    )
    table.add_row("logging.level", cfg.logging.level)
    table.add_row("logging.format", cfg.logging.format)
    console.print(table)


def _print_secrets_table(cfg: AppConfig | None = None) -> list[str]:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Variable")
    table.add_column("Value")
    table.add_column("Status")

    # Build the env-var list dynamically: always include the static ones, plus
    # the openai_compatible one when that provider is selected. Defensive
    # dedupe via dict.fromkeys preserves insertion order even if upstream
    # somehow leaks duplicates.
    raw_names: list[str] = list(SECRET_ENV_VARS.values())
    if cfg is not None and cfg.enrichment.provider == "openai_compatible":
        raw_names.append(cfg.openai_compatible.api_key_env)
    env_names: list[str] = list(dict.fromkeys(raw_names))

    missing: list[str] = []
    for env_name in env_names:
        v = get_secret(env_name)
        if v:
            table.add_row(env_name, redact_secret(v), "[green]set[/green]")
        else:
            table.add_row(env_name, "(not set)", "[yellow]missing[/yellow]")
            missing.append(env_name)
    console.print(table)
    return missing


def _print_tools_table() -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Tool")
    table.add_column("Path")
    table.add_column("Note")
    for tool, why in [
        ("git", "required for repo cloning"),
        ("rg", "required for code search (Step 4+)"),
        ("jira", "optional CLI cross-check"),
        ("glab", "optional CLI cross-check + glab clone strategy"),
    ]:
        path = shutil.which(tool)
        if path:
            table.add_row(tool, path, "[green]found[/green]")
        else:
            table.add_row(tool, "(not found)", f"[yellow]{why}[/yellow]")
    console.print(table)


def _first_line(text: str) -> str:
    text = text.strip()
    return text.splitlines()[0] if text else ""


def _run_cross_check(label: str, argv: list[str], *, show_stdout_on_ok: bool) -> None:
    """Invoke a CLI cross-check tool and pretty-print the outcome."""
    binary = argv[0]
    if not shutil.which(binary):
        console.print(f"  [dim]{binary} CLI not on PATH (skipping)[/dim]")
        return
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError) as e:
        console.print(f"  [yellow]{label} cross-check failed:[/yellow] {e}")
        return
    if r.returncode == 0:
        suffix = f": {_first_line(r.stdout)}" if show_stdout_on_ok else ""
        console.print(f"  [green]{label} ok[/green]{suffix}")
        return
    diag = _first_line(r.stderr) or _first_line(r.stdout)
    console.print(f"  [yellow]{label} exit={r.returncode}[/yellow]: {diag}")


def _cli_cross_checks() -> None:
    """Best-effort: invoke `jira me` and `glab auth status` if installed."""
    _run_cross_check("jira me", ["jira", "me"], show_stdout_on_ok=True)
    _run_cross_check("glab auth", ["glab", "auth", "status"], show_stdout_on_ok=False)


@repo_cache_app.command("prune")
def repo_cache_prune(older_than: str = typer.Option("14d", "--older-than")) -> None:
    """Prune the repo cache."""
    _stub(4, "repo-cache prune")


def _build_jira_client_for_cli(cfg: AppConfig):
    """Build a JiraClient for CLI commands; print + raise typer.Exit on misconfig."""
    import os

    from _shared.io.sinks.jira import JiraClient

    if not cfg.jira.url:
        err_console.print("[red]jira.url is not configured.[/red]")
        raise typer.Exit(code=1)
    pat = os.environ.get("JIRA_PAT")
    if not pat:
        err_console.print("[red]JIRA_PAT env var is not set.[/red]")
        raise typer.Exit(code=1)
    return JiraClient.from_config(
        url=cfg.jira.url,
        pat=pat,
        auth_mode=cfg.jira.auth_mode,
        user_email=cfg.jira.user_email,
        ca_bundle=cfg.jira.ca_bundle,
    )


def _discover_fields_or_exit(client, *, project, issue_type, from_issue):
    """Run the chosen discovery path. Returns (fields, source_label)."""
    from _shared.io.sinks.jira import discover_create_meta, discover_fields_from_issue

    try:
        if from_issue:
            return discover_fields_from_issue(client, from_issue), f"issue={from_issue}"
        return (
            discover_create_meta(client, project, issue_type),
            f"project={project} issue_type={issue_type}",
        )
    except Exception as e:
        err_console.print(f"[red]field discovery failed:[/red] {e}")
        if not from_issue:
            err_console.print(
                "[yellow]Hint:[/yellow] try `--from-issue <KEY-NNN>` against an "
                "existing ticket instead — that path doesn't need createmeta."
            )
        raise typer.Exit(code=1) from e


def _render_fields_table(fields, source_label: str) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Field name", style="cyan")
    table.add_column("ID")
    table.add_column("Required")
    table.add_column("Type")
    table.add_column("Allowed values (sample)")
    seen_ids: set[str] = set()
    for _key, info in sorted(fields.items()):
        if info.field_id in seen_ids:
            continue
        seen_ids.add(info.field_id)
        allowed = (
            ", ".join(str(v) for v in info.allowed_values)
            if info.allowed_values
            else ""
        )
        table.add_row(
            info.name,
            info.field_id,
            "yes" if info.required else "",
            info.schema_type or "",
            allowed,
        )
    console.print(f"[bold]Fields for {source_label}:[/bold]")
    console.print(table)


@jira_app.command("fields")
def jira_fields(
    project: Optional[str] = typer.Option(
        None, "--project", help="Discover via createmeta (needs Create Issues perm)."
    ),
    issue_type: str = typer.Option("Bug", "--issue-type"),
    from_issue: Optional[str] = typer.Option(
        None,
        "--from-issue",
        help="Discover via an existing issue you can read (e.g. CENTPM-1285). "
        "Use this when createmeta is restricted on your Jira instance.",
    ),
) -> None:
    """Discover custom field IDs for a Jira project.

    Two modes:

    \b
    --project KEY        : uses /rest/api/2/issue/createmeta (admins / users
                           with Create Issues permission). Returns full schema
                           including required flags and allowedValues.
    --from-issue KEY-N   : reads an existing ticket. Works on any read-only
                           account. Returns field IDs but not required flags.
                           Surfaces priority's current display value.
    """
    if not project and not from_issue:
        err_console.print(
            "[red]Specify either --project KEY or --from-issue KEY-NNN[/red]"
        )
        raise typer.Exit(code=1)

    configure_logging()
    cfg = load_config()
    client = _build_jira_client_for_cli(cfg)
    fields, source_label = _discover_fields_or_exit(
        client, project=project, issue_type=issue_type, from_issue=from_issue
    )
    _render_fields_table(fields, source_label)


@jira_app.command("whoami")
def jira_whoami() -> None:
    """Verify Jira PAT works."""
    import os

    from _shared.io.sinks.jira import JiraClient

    configure_logging()
    cfg = load_config()
    if not cfg.jira.url:
        err_console.print("[red]jira.url is not configured.[/red]")
        raise typer.Exit(code=1)
    pat = os.environ.get("JIRA_PAT")
    if not pat:
        err_console.print("[red]JIRA_PAT env var is not set.[/red]")
        raise typer.Exit(code=1)

    client = JiraClient.from_config(
        url=cfg.jira.url,
        pat=pat,
        auth_mode=cfg.jira.auth_mode,
        user_email=cfg.jira.user_email,
        ca_bundle=cfg.jira.ca_bundle,
    )
    try:
        me = client.whoami()
    except Exception as e:
        err_console.print(f"[red]whoami failed:[/red] {e}")
        raise typer.Exit(code=1) from e
    console.print(f"[green]ok[/green]: {me.display_name} ({me.username})")
    if me.email:
        console.print(f"      email: {me.email}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
