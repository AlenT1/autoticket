"""End-to-end orchestrator. One function: `run_once`.

Pipeline:
  1. List all Drive files (no time filter — root files always available).
  2. Download.
  3. Dedupe (md vs Google-Docs export twins).
  4. Classify each file -> single_epic | multi_epic | root.
  5. Bundle all root files into a context blob.
  6. For each task-bearing file (single_epic / multi_epic) modified
     since the cursor, extract via LLM.
  7. Fetch the project tree from Jira (one paginated /search).
  8. Run the matcher ONCE across all extractions:
        Stage 1 — flat epic match (all extracted epics vs project epics).
        Stage 2 — grouped task match (per matched epic, batched + parallel).
  9. Build a ReconcilePlan per file from the MatcherResult.
 10. apply or capture the plans.
 11. Persist the cursor on success.
"""
from __future__ import annotations

import json as _json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cache import Cache, file_content_sha
from .drive.client import (
    DriveFile, build_service, download_file, list_folder, list_local_folder,
)
from _shared.io.sinks import Ticket
from _shared.io.sinks.jira import CapturingJiraSink, JiraClient, JiraSink
from _shared.io.sinks.jira.strategies import StaticMapStrategy
from .pipeline.classifier import ClassifyResult, classify_file
from .pipeline.commenter import format_update_comment
from .pipeline.context_bundler import bundle_root_context
from .pipeline.dedupe import find_duplicate_copies
from .pipeline.dirty_filter import filter_dirty
from .pipeline.file_extract import extract_or_reuse
from .pipeline.extractor import finalize_body
from .pipeline.file_match import match_with_cache
from .pipeline.reconciler import (
    Action,
    EpicGroup,
    ReconcilePlan,
    build_plans_from_dirty,
)
from .state import State, load as load_state, save as save_state

logger = logging.getLogger(__name__)


@dataclass
class RunReport:
    started_at: datetime
    finished_at: datetime | None = None
    apply: bool = False
    files_total: int = 0
    files_classified: dict[str, int] = field(default_factory=dict)
    extractions_ok: int = 0
    extractions_failed: int = 0
    cache_hits_classify: int = 0
    cache_hits_extract: int = 0
    cache_hits_match: int = 0
    actions_by_kind: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    plans: list[ReconcilePlan] = field(default_factory=list)

    def bump_action(self, kind: str) -> None:
        self.actions_by_kind[kind] = self.actions_by_kind.get(kind, 0) + 1


def run_once(
    *,
    apply: bool = False,
    since_override: datetime | None = None,
    download_dir: str = "data/gdrive_files",
    local_dir: str = "data/local_files",
    source: str = "both",
    state_path: Path | None = None,
    cache_path: Path | None = None,
    use_cache: bool = True,
    only_file_name: str | None = None,
    target_epic: str | None = None,
    capture_path: str | None = None,
    matcher_batch_size: int = 4,
    matcher_max_workers: int = 3,
    verify_before_apply: bool = False,
    verify_md_path: Path | None = None,
) -> RunReport:
    """Single end-to-end run.

    `use_cache=True` (default) consults `cache.json` to skip the classify
    LLM call when a file's modified_time hasn't changed, and to skip the
    extract LLM call when the file's content sha hasn't changed. Pass
    `use_cache=False` (or `--no-cache` on the CLI) to force re-run
    everything.
    """
    started = datetime.now(tz=timezone.utc)
    report = RunReport(started_at=started, apply=apply)
    cache = Cache.load(cache_path) if use_cache else Cache()

    env = _validate_run_env(source, report)
    if env is None:
        return report
    project_key, folder_id = env

    state = load_state(state_path)
    cursor = since_override or state.last_run_at
    logger.info(
        "run_once: cursor=%s apply=%s source=%s", cursor, apply, source,
    )

    files, local_paths = _collect_files(
        source, folder_id, download_dir, local_dir, report,
    )

    report.files_total = len(files)
    if not files:
        return _finalize_empty_run(report, started, apply, state_path, "no files")

    # 3+4. Dedupe + classify all ------------------------------------------
    content_shas = _compute_content_shas(files, local_paths)
    classifications = _classify_all_files(
        files, local_paths, content_shas, cache, report,
    )

    # 5. Bundle root context (always — regardless of cursor) --------------
    root_context = bundle_root_context(
        _root_pairs(files, classifications, local_paths),
    )

    # 6. Extract task-bearing files modified since cursor (+ --only filter) ---
    extractions, dirty_anchors_per_file = _extract_task_bearing_files(
        files, classifications, local_paths, content_shas, root_context,
        cache, use_cache, cursor, only_file_name, report,
    )

    if not extractions:
        return _finalize_empty_run(
            report, started, apply, state_path, "no task-bearing files to reconcile",
        )

    # 7. Fetch project tree from Jira (one paginated /search) -------------
    jira = JiraClient.from_env()
    _resolver = StaticMapStrategy()
    sink, captured_writes = _make_sink(jira, project_key, capture_path, _resolver)
    project_tree = _fetch_project_tree(sink, project_key, report)
    if project_tree is None:
        report.finished_at = datetime.now(tz=timezone.utc)
        return report

    # 8. Matcher ----------------------------------------------------------
    matcher_result = _run_matcher(
        extractions, project_tree, cache, content_shas, use_cache,
        matcher_batch_size, matcher_max_workers, dirty_anchors_per_file,
        report,
    )
    if matcher_result is None:
        report.finished_at = datetime.now(tz=timezone.utc)
        return report

    # 9. Filter to dirty + build ReconcilePlans ---------------------------
    plans = _build_plans(
        matcher_result, extractions, dirty_anchors_per_file,
        sink, _resolver, report,
    )
    if plans is None:
        report.finished_at = datetime.now(tz=timezone.utc)
        return report

    report.plans.extend(plans)

    # 10. apply (or report-only) ------------------------------------------
    if apply and verify_before_apply:
        verify_outcome = _verify_gate(
            report, plans, jira=jira, source=source,
            verify_md_path=verify_md_path,
        )
        if verify_outcome is None:
            report.finished_at = datetime.now(tz=timezone.utc)
            return report

    _count_action_kinds(plans, report)

    if apply:
        _run_apply_phase(plans, extractions, sink, target_epic, report)

    # 11. Persist cursor + dump captured writes ---------------------------
    report.finished_at = datetime.now(tz=timezone.utc)
    _persist_state(state, started, apply, capture_path, report, state_path)
    _dump_capture(capture_path, captured_writes)
    _persist_cache(cache, cache_path, use_cache, apply, capture_path, report)
    return report


# ----------------------------------------------------------------------
# run_once helpers (extracted to keep run_once orchestration-only;
# each helper owns one phase's branching so cognitive complexity stays
# bounded per function).
# ----------------------------------------------------------------------


def _validate_run_env(source: str, report: "RunReport") -> tuple[str, str | None] | None:
    """Validate inputs and required env vars. Returns (project_key, folder_id)
    or None if the run should bail (with errors already on `report`)."""
    if source not in ("both", "gdrive", "local"):
        raise ValueError(f"invalid source {source!r}; expected both/gdrive/local")
    project_key = os.environ.get("JIRA_PROJECT_KEY")
    if not project_key:
        report.errors.append("JIRA_PROJECT_KEY is not set in env")
        report.finished_at = datetime.now(tz=timezone.utc)
        return None
    folder_id = os.environ.get("FOLDER_ID") if source in ("both", "gdrive") else None
    if source in ("both", "gdrive") and not folder_id:
        report.errors.append("FOLDER_ID is not set in env")
        report.finished_at = datetime.now(tz=timezone.utc)
        return None
    return project_key, folder_id


def _collect_files(
    source: str,
    folder_id: str | None,
    download_dir: str,
    local_dir: str,
    report: "RunReport",
) -> tuple[list[DriveFile], dict[str, Path]]:
    """List + download files from the configured sources."""
    files: list[DriveFile] = []
    local_paths: dict[str, Path] = {}
    if source in ("both", "gdrive"):
        drive_service = build_service()
        drive_files = list_folder(folder_id, service=drive_service)
        files.extend(drive_files)
        download_root = Path(download_dir)
        for f in drive_files:
            try:
                p = download_file(f, download_root, service=drive_service)
                if p is not None:
                    local_paths[f.id] = p
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"download failed for {f.name}: {e}")
    if source in ("both", "local"):
        local_files, local_only_paths = list_local_folder(Path(local_dir))
        files.extend(local_files)
        local_paths.update(local_only_paths)
    return files, local_paths


def _finalize_empty_run(
    report: "RunReport",
    started: datetime,
    apply: bool,
    state_path: Path | None,
    reason: str,
) -> "RunReport":
    """Short-circuit when there's nothing to reconcile (no files, or no
    task-bearing extractions). Logs the reason; persists the cursor only
    if `apply=True`."""
    logger.info("run_once: %s; nothing to do", reason)
    report.finished_at = datetime.now(tz=timezone.utc)
    if apply:
        save_state(State(last_run_at=started, last_run_status="ok"), state_path)
    return report


def _compute_content_shas(
    files: list[DriveFile], local_paths: dict[str, Path],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in files:
        if f.id in local_paths:
            try:
                out[f.id] = file_content_sha(local_paths[f.id])
            except Exception:
                pass
    return out


def _classify_all_files(
    files: list[DriveFile],
    local_paths: dict[str, Path],
    content_shas: dict[str, str],
    cache: Cache,
    report: "RunReport",
) -> dict[str, ClassifyResult]:
    """Tier-1-cached classification for every file. Mutates `report`'s
    classification counters."""
    duplicate_of = find_duplicate_copies(files, local_paths)
    classifications: dict[str, ClassifyResult] = {}
    neighbor_names = [f.name for f in files]
    for f in files:
        if f.id not in local_paths:
            continue
        res = _classify_one(
            f, local_paths, content_shas, neighbor_names, cache,
            duplicate_of, report,
        )
        if res is not None:
            classifications[f.id] = res
    return classifications


def _classify_one(
    f: DriveFile,
    local_paths: dict[str, Path],
    content_shas: dict[str, str],
    neighbor_names: list[str],
    cache: Cache,
    duplicate_of: dict[str, str],
    report: "RunReport",
) -> ClassifyResult | None:
    if f.id in duplicate_of:
        res = ClassifyResult(
            file_id=f.id,
            role="skip",
            confidence=1.0,
            reason=f"content-duplicate of canonical file {duplicate_of[f.id]}",
        )
        report.files_classified["skip"] = report.files_classified.get("skip", 0) + 1
        logger.info("dedup: %s is a copy of %s", f.name, duplicate_of[f.id])
        return res
    mtime_iso = f.modified_time.isoformat()
    cached = cache.get_classification(f.id, mtime_iso)
    if cached is not None:
        role, conf, reason = cached
        report.files_classified[role] = report.files_classified.get(role, 0) + 1
        report.cache_hits_classify += 1
        logger.info("cache: classify hit for %s -> %s", f.name, role)
        return ClassifyResult(
            file_id=f.id, role=role, confidence=conf or 0.0,
            reason=(reason or "") + " [cache hit]",
        )
    try:
        res = classify_file(
            f, local_path=local_paths[f.id], neighbor_names=neighbor_names,
        )
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"classifier failed for {f.name}: {e}")
        return None
    report.files_classified[res.role] = report.files_classified.get(res.role, 0) + 1
    logger.info(
        "classified %s -> %s (conf=%.2f) %s",
        f.name, res.role, res.confidence, res.reason,
    )
    cache.set_classification(
        file_id=f.id,
        modified_time=mtime_iso,
        content_sha=content_shas.get(f.id, ""),
        role=res.role,
        confidence=res.confidence,
        reason=res.reason,
    )
    return res


def _root_pairs(
    files: list[DriveFile],
    classifications: dict[str, ClassifyResult],
    local_paths: dict[str, Path],
) -> list[tuple[str, Path]]:
    return sorted(
        (
            (f.name, local_paths[f.id])
            for f in files
            if classifications.get(f.id)
            and classifications[f.id].role == "root"
            and f.id in local_paths
        ),
        key=lambda np: np[0],
    )


def _extract_task_bearing_files(
    files: list[DriveFile],
    classifications: dict[str, ClassifyResult],
    local_paths: dict[str, Path],
    content_shas: dict[str, str],
    root_context: str,
    cache: Cache,
    use_cache: bool,
    cursor: datetime | None,
    only_file_name: str | None,
    report: "RunReport",
) -> tuple[list[tuple[DriveFile, object]], dict[str, set[str] | None]]:
    """Extract every task-bearing file modified since `cursor` (and matching
    `--only`). Per-file dirty-anchor map is `None` for cold (process-all),
    `set()` for Tier 2 hits (no changes), or the changed source_anchors."""
    extractions: list[tuple[DriveFile, object]] = []
    dirty_anchors_per_file: dict[str, set[str] | None] = {}

    def _ok():
        report.extractions_ok += 1

    def _failed(msg: str):
        report.extractions_failed += 1
        report.errors.append(msg)

    def _hit():
        report.cache_hits_extract += 1

    for f in files:
        if not _should_extract(f, classifications, local_paths, cursor, only_file_name):
            continue
        ext, dirty = extract_or_reuse(
            f,
            classification=classifications[f.id],
            local_path=local_paths[f.id],
            content_sha=content_shas.get(f.id, ""),
            root_context=root_context,
            cache=cache,
            use_cache=use_cache,
            on_extract_ok=_ok,
            on_extract_failed=_failed,
            on_cache_hit_extract=_hit,
        )
        if ext is not None:
            extractions.append((f, ext))
            dirty_anchors_per_file[f.id] = dirty
            if dirty is not None:
                logger.info(
                    "dirty anchors for %s: %d (%s)",
                    f.name, len(dirty),
                    ", ".join(sorted(dirty)[:5]) + ("..." if len(dirty) > 5 else ""),
                )
    return extractions, dirty_anchors_per_file


def _should_extract(
    f: DriveFile,
    classifications: dict[str, ClassifyResult],
    local_paths: dict[str, Path],
    cursor: datetime | None,
    only_file_name: str | None,
) -> bool:
    c = classifications.get(f.id)
    if not c or c.role not in ("single_epic", "multi_epic"):
        return False
    if f.id not in local_paths:
        return False
    if cursor is not None and f.modified_time <= cursor:
        logger.info(
            "skip extract %s: modifiedTime %s <= cursor %s",
            f.name, f.modified_time, cursor,
        )
        return False
    if only_file_name and f.name != only_file_name:
        logger.info("skip extract %s: --only is set to %r", f.name, only_file_name)
        return False
    return True


def _make_sink(
    jira: JiraClient,
    project_key: str,
    capture_path: str | None,
    resolver: Any,
) -> tuple[JiraSink, list[dict]]:
    """Construct the sink (CapturingJiraSink if --capture, otherwise
    JiraSink). Returns `(sink, captured_writes)` — `captured_writes` is
    a live reference to the sink's recorder list when capturing, or an
    empty list when not."""
    kwargs = {
        "client": jira,
        "project_key": project_key,
        "assignee_resolver": resolver,
        "filter_components": False,
    }
    if capture_path:
        sink: JiraSink = CapturingJiraSink(**kwargs)
        return sink, sink.captured_writes
    return JiraSink(**kwargs), []


def _fetch_project_tree(
    sink: JiraSink, project_key: str, report: "RunReport",
) -> dict | None:
    try:
        project_tree = sink.fetch_project_tree(project_key)
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"failed to fetch project tree: {e}")
        return None
    logger.info(
        "fetched project tree: %d epics, %d children",
        project_tree["epic_count"], project_tree["child_count"],
    )
    return project_tree


def _run_matcher(
    extractions: list[tuple[DriveFile, object]],
    project_tree: dict,
    cache: Cache,
    content_shas: dict[str, str],
    use_cache: bool,
    matcher_batch_size: int,
    matcher_max_workers: int,
    dirty_anchors_per_file: dict[str, set[str] | None],
    report: "RunReport",
):
    """Run the two-stage LLM matcher; return `MatcherResult` or `None`
    if the matcher errored (with the error already on `report`)."""
    def _hit_match():
        report.cache_hits_match += 1
    try:
        return match_with_cache(
            extractions, project_tree, cache,
            content_shas=content_shas,
            use_cache=use_cache,
            matcher_batch_size=matcher_batch_size,
            matcher_max_workers=matcher_max_workers,
            on_cache_hits_match=_hit_match,
            dirty_anchors_per_file=dirty_anchors_per_file,
        )
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"matcher failed: {e}")
        return None


def _build_plans(
    matcher_result,
    extractions: list[tuple[DriveFile, object]],
    dirty_anchors_per_file: dict[str, set[str] | None],
    sink: JiraSink,
    resolver: Any,
    report: "RunReport",
) -> list[ReconcilePlan] | None:
    try:
        dirty_sections = filter_dirty(
            matcher_result, extractions, dirty_anchors_per_file,
        )
        return build_plans_from_dirty(
            dirty_sections, sink=sink, resolver=resolver,
        )
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"build_plans failed: {e}")
        return None


def _count_action_kinds(
    plans: list[ReconcilePlan], report: "RunReport",
) -> None:
    for plan in plans:
        for a in plan.actions:
            report.bump_action(a.kind)


def _run_apply_phase(
    plans: list[ReconcilePlan],
    extractions: list[tuple[DriveFile, object]],
    sink: JiraSink,
    target_epic: str | None,
    report: "RunReport",
) -> None:
    for plan in plans:
        f = next(
            (df for df, _ in extractions if _.file_id == plan.file_id), None,
        )
        if f is None:
            continue
        try:
            _apply_plan(
                plan, drive_file=f, sink=sink, target_epic=target_epic,
            )
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"apply failed for {plan.file_name}: {e}")


def _persist_state(
    state: State,
    started: datetime,
    apply: bool,
    capture_path: str | None,
    report: "RunReport",
    state_path: Path | None,
) -> None:
    """Persist the run-cursor state. Same gate as cache: only saves on
    a successful apply, and an error apply records the previous cursor
    with an `error` status flag."""
    if not (apply and not capture_path):
        return
    if not report.errors:
        save_state(State(last_run_at=started, last_run_status="ok"), state_path)
    else:
        save_state(
            State(last_run_at=state.last_run_at, last_run_status="error"),
            state_path,
        )


def _dump_capture(
    capture_path: str | None, captured_writes: list[dict],
) -> None:
    if not capture_path:
        return
    cap = Path(capture_path)
    cap.parent.mkdir(parents=True, exist_ok=True)
    cap.write_text(
        _json.dumps(captured_writes, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info(
        "capture: %d intended write(s) recorded to %s",
        len(captured_writes), capture_path,
    )


def _persist_cache(
    cache: Cache,
    cache_path: Path | None,
    use_cache: bool,
    apply: bool,
    capture_path: str | None,
    report: "RunReport",
) -> None:
    """Save the matcher cache only when actual writes landed in Jira.
    Same gate as `_persist_state`. Capture-mode and dry-runs intentionally
    don't save — saving them would lie about Jira state and cause
    `create_*` actions to be silently skipped on the next warm run
    (Tier 2 hit with dirty=∅) while Jira has no record of the issue."""
    if not use_cache:
        return
    if apply and not capture_path and not report.errors:
        try:
            cache.save(cache_path)
            logger.info(
                "cache: saved %d file entr(ies) (classify hits=%d, extract hits=%d)",
                len(cache.files), report.cache_hits_classify, report.cache_hits_extract,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("cache: save failed: %s", e)
        return
    logger.info(
        "cache: NOT saved (apply=%s capture=%s errors=%d) — "
        "preserves cache/Jira consistency",
        apply, bool(capture_path), len(report.errors),
    )


# ----------------------------------------------------------------------
# verify gate: render data/run_plan.md and let the user approve / pick
# a subset / cancel before any write fires.
# ----------------------------------------------------------------------


_SKIPPED_BY_USER = "skipped_by_user"
_VERIFY_HELP = (
    "  ENTER         approve all\n"
    "  e.g. 1,3,7    approve only those numbers\n"
    "  x             cancel everything"
)


def _verify_gate(
    report: "RunReport",
    plans: list[ReconcilePlan],
    *,
    jira: JiraClient,
    source: str,
    verify_md_path: Path | None,
) -> bool | None:
    """Render the run plan, prompt the user, mutate `plans` in place
    to drop non-approved actions. Returns True if any writes should
    proceed, None if the run was cancelled (caller should bail)."""
    from .pipeline.run_plan_md import (
        WRITEABLE_KINDS, build_run_plan_dict, render_run_plan_md,
    )

    plan_dict = build_run_plan_dict(
        report, jira=jira, mode="apply (verify)", source=source,
    )
    md_path = verify_md_path or Path("data") / "run_plan.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_run_plan_md(plan_dict), encoding="utf-8")

    n_actions = sum(
        plan_dict["totals"].get(k, 0) for k in WRITEABLE_KINDS
    )
    if n_actions == 0:
        print(
            f"\nRun plan written to: {md_path}\nNo writeable actions; "
            "nothing to apply.", file=sys.stderr,
        )
        return None

    print(
        f"\nReview the plan in {md_path} ({n_actions} change(s) proposed).\n"
        f"{_VERIFY_HELP}",
        file=sys.stderr,
    )
    try:
        ans = input("> ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n[verify] aborted; no Jira writes.", file=sys.stderr)
        return None

    approved = _parse_approved(ans, n_actions)
    if approved is None:
        print("[verify] cancelled by user.", file=sys.stderr)
        return None
    if approved == "all":
        return True

    if not approved:
        print("[verify] no valid numbers parsed; cancelled.", file=sys.stderr)
        return None

    skipped = _mark_unapproved_as_skipped(plans, approved, WRITEABLE_KINDS)
    print(
        f"[verify] approved {sorted(approved)}; "
        f"skipping {skipped} other action(s).",
        file=sys.stderr,
    )
    return True


def _parse_approved(
    ans: str, n_actions: int,
) -> set[int] | str | None:
    """Returns 'all' (Enter), None (cancel), or a set of approved
    1-based indices. Out-of-range numbers are dropped silently."""
    if ans == "":
        return "all"
    if ans.lower() == "x":
        return None
    try:
        nums = {int(x.strip()) for x in ans.split(",") if x.strip()}
    except ValueError:
        return None
    return {i for i in nums if 1 <= i <= n_actions}


def _mark_unapproved_as_skipped(
    plans: list[ReconcilePlan],
    approved: set[int],
    writeable_kinds: frozenset[str],
) -> int:
    """Walk plans in apply order; for each writeable action whose 1-based
    index is not in `approved`, set its kind to a sentinel that the apply
    handlers don't recognise (so no write fires). Returns the count
    skipped."""
    state = {"idx": 0, "skipped": 0}
    for plan in plans:
        for g in plan.groups:
            _maybe_skip(g.epic_action, approved, writeable_kinds, state)
            for ta in g.task_actions:
                _maybe_skip(ta, approved, writeable_kinds, state)
    return state["skipped"]


def _maybe_skip(
    action: Action,
    approved: set[int],
    writeable_kinds: frozenset[str],
    state: dict,
) -> None:
    if action.kind not in writeable_kinds:
        return
    state["idx"] += 1
    if state["idx"] not in approved:
        action.kind = _SKIPPED_BY_USER
        state["skipped"] += 1


# ----------------------------------------------------------------------
# apply
# ----------------------------------------------------------------------


def _apply_plan(
    plan: ReconcilePlan,
    *,
    drive_file: DriveFile,
    sink: JiraSink,
    target_epic: str | None = None,
) -> None:
    """Walk each EpicGroup; epic action first, then its task actions."""
    for group in plan.groups:
        if target_epic:
            epic_key = target_epic
        else:
            epic_key = _apply_epic_action(
                group.epic_action, drive_file=drive_file, sink=sink,
            )
        for task_action in group.task_actions:
            _apply_task_action(
                task_action,
                epic_key=epic_key,
                drive_file=drive_file,
                sink=sink,
            )


def _apply_epic_action(
    a: Action,
    *,
    drive_file: DriveFile,
    sink: JiraSink,
) -> str | None:
    if a.kind == "create_epic":
        return sink.create(_ticket_for_create(a, issue_type="Epic", epic_key=None))
    if a.kind == "update_epic":
        live = sink.get_issue_normalized(a.target_key) or {}
        sink.update(a.target_key, _ticket_for_update(a, live, issue_type="Epic"))
        sink.comment(a.target_key, _comment_for(a, drive_file=drive_file, sink=sink))
        return a.target_key
    if a.kind == "noop":
        return a.target_key
    return None


def _apply_task_action(
    a: Action,
    *,
    epic_key: str | None,
    drive_file: DriveFile,
    sink: JiraSink,
) -> None:
    if a.kind == "create_task":
        sink.create(_ticket_for_create(a, issue_type="Task", epic_key=epic_key))
        return
    if a.kind == "update_task":
        live = sink.get_issue_normalized(a.target_key) or {}
        sink.update(a.target_key, _ticket_for_update(a, live, issue_type="Task"))
        sink.comment(a.target_key, _comment_for(a, drive_file=drive_file, sink=sink))
    # noop / orphan: nothing to write


def _ticket_for_create(
    a: Action, *, issue_type: str, epic_key: str | None,
) -> Ticket:
    """Build a Ticket for `sink.create`. Pre-resolved assignee is passed
    through `custom_fields` so the sink's `assignee_resolver` does not
    re-resolve the already-resolved username from the reconciler."""
    custom: dict = {}
    if a.assignee_username:
        custom["assignee"] = {"name": a.assignee_username}
    return Ticket(
        summary=a.summary or "",
        description=a.description or "",
        type=issue_type,
        epic_key=epic_key,
        custom_fields=custom,
    )


def _ticket_for_update(
    a: Action, live: dict, *, issue_type: str,
) -> Ticket:
    """Build a Ticket for `sink.update`. Runs `finalize_body` against the
    live Jira description before passing it to the sink — load-bearing
    for DoD checkbox preservation across `update_*` writes."""
    custom: dict = {}
    if a.assignee_username:
        custom["assignee"] = {"name": a.assignee_username}
    new_desc = finalize_body(a.description or "", live.get("description") or "")
    return Ticket(
        summary=a.summary or "",
        description=new_desc,
        type=issue_type,
        custom_fields=custom,
    )


def _comment_for(action: Action, *, drive_file: DriveFile, sink: JiraSink) -> str:
    live = sink.get_issue_normalized(action.target_key) or {}
    return format_update_comment(
        assignee_username=live.get("assignee_username"),
        fallback_username=live.get("reporter_username"),
        drive_file=drive_file,
        before_summary=live.get("summary"),
        after_summary=action.summary,
        before_description=live.get("description"),
        after_description=action.description,
    )
