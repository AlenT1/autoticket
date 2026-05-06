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

from .cache import Cache, file_content_sha
from .drive.client import (
    DriveFile, build_service, download_file, list_folder, list_local_folder,
)
from .jira.client import JiraClient, get_issue
from .jira.project_tree import fetch_project_tree
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
    if source not in ("both", "gdrive", "local"):
        raise ValueError(f"invalid source {source!r}; expected both/gdrive/local")

    started = datetime.now(tz=timezone.utc)
    report = RunReport(started_at=started, apply=apply)
    cache = Cache.load(cache_path) if use_cache else Cache()

    project_key = os.environ.get("JIRA_PROJECT_KEY")
    if not project_key:
        report.errors.append("JIRA_PROJECT_KEY is not set in env")
        report.finished_at = datetime.now(tz=timezone.utc)
        return report

    folder_id = os.environ.get("FOLDER_ID") if source in ("both", "gdrive") else None
    if source in ("both", "gdrive") and not folder_id:
        report.errors.append("FOLDER_ID is not set in env")
        report.finished_at = datetime.now(tz=timezone.utc)
        return report

    state = load_state(state_path)
    cursor = since_override or state.last_run_at
    logger.info(
        "run_once: cursor=%s apply=%s source=%s", cursor, apply, source,
    )

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

    report.files_total = len(files)
    if not files:
        logger.info("run_once: no files from source=%s; nothing to do", source)
        report.finished_at = datetime.now(tz=timezone.utc)
        if apply:
            save_state(State(last_run_at=started, last_run_status="ok"), state_path)
        return report

    # 3+4. Dedupe + classify all ------------------------------------------
    duplicate_of = find_duplicate_copies(files, local_paths)
    classifications: dict[str, ClassifyResult] = {}
    neighbor_names = [f.name for f in files]
    # Pre-compute file content shas (used by both classify-cache and
    # extract-cache). Cheap (sha256 over a few KB).
    content_shas: dict[str, str] = {}
    for f in files:
        if f.id in local_paths:
            try:
                content_shas[f.id] = file_content_sha(local_paths[f.id])
            except Exception:
                pass

    for f in files:
        if f.id not in local_paths:
            continue
        if f.id in duplicate_of:
            res = ClassifyResult(
                file_id=f.id,
                role="skip",
                confidence=1.0,
                reason=f"content-duplicate of canonical file {duplicate_of[f.id]}",
            )
            classifications[f.id] = res
            report.files_classified["skip"] = (
                report.files_classified.get("skip", 0) + 1
            )
            logger.info("dedup: %s is a copy of %s", f.name, duplicate_of[f.id])
            continue

        # Tier 1 cache: skip classify if (file_id, modified_time) match.
        mtime_iso = f.modified_time.isoformat()
        cached = cache.get_classification(f.id, mtime_iso)
        if cached is not None:
            role, conf, reason = cached
            res = ClassifyResult(
                file_id=f.id,
                role=role,
                confidence=conf or 0.0,
                reason=(reason or "") + " [cache hit]",
            )
            classifications[f.id] = res
            report.files_classified[role] = report.files_classified.get(role, 0) + 1
            report.cache_hits_classify += 1
            logger.info("cache: classify hit for %s -> %s", f.name, role)
            continue

        try:
            res = classify_file(
                f, local_path=local_paths[f.id], neighbor_names=neighbor_names
            )
            classifications[f.id] = res
            report.files_classified[res.role] = (
                report.files_classified.get(res.role, 0) + 1
            )
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
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"classifier failed for {f.name}: {e}")

    # 5. Bundle root context (always — regardless of cursor) --------------
    root_pairs: list[tuple[str, Path]] = sorted(
        (
            (f.name, local_paths[f.id])
            for f in files
            if classifications.get(f.id)
            and classifications[f.id].role == "root"
            and f.id in local_paths
        ),
        key=lambda np: np[0],
    )
    root_context = bundle_root_context(root_pairs)

    # 6. Extract task-bearing files modified since cursor (+ --only filter) ---
    extractions: list[tuple[DriveFile, object]] = []

    def _ok():
        report.extractions_ok += 1

    def _failed(msg: str):
        report.extractions_failed += 1
        report.errors.append(msg)

    def _hit():
        report.cache_hits_extract += 1

    # Per-file dirty-anchor map: which task source_anchors did the doc
    # actually change this run? None = process-all (cold), set() = none
    # changed (Tier 2 hit), set(...) = exactly these changed.
    dirty_anchors_per_file: dict[str, set[str] | None] = {}

    for f in files:
        c = classifications.get(f.id)
        if not c or c.role not in ("single_epic", "multi_epic"):
            continue
        if f.id not in local_paths:
            continue
        if cursor is not None and f.modified_time <= cursor:
            logger.info(
                "skip extract %s: modifiedTime %s <= cursor %s",
                f.name, f.modified_time, cursor,
            )
            continue
        if only_file_name and f.name != only_file_name:
            logger.info("skip extract %s: --only is set to %r", f.name, only_file_name)
            continue

        ext, dirty = extract_or_reuse(
            f,
            classification=c,
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

    if not extractions:
        logger.info("run_once: no task-bearing files to reconcile")
        report.finished_at = datetime.now(tz=timezone.utc)
        if apply:
            save_state(State(last_run_at=started, last_run_status="ok"), state_path)
        return report

    # 7. Fetch project tree from Jira (one paginated /search) -------------
    jira = JiraClient.from_env()
    captured_writes: list[dict] = []
    if capture_path:
        _enable_capture(jira, captured_writes)
    try:
        project_tree = fetch_project_tree(jira, project_key)
        logger.info(
            "fetched project tree: %d epics, %d children",
            project_tree["epic_count"], project_tree["child_count"],
        )
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"failed to fetch project tree: {e}")
        report.finished_at = datetime.now(tz=timezone.utc)
        return report

    # 8. Matcher ----------------------------------------------------------
    def _hit_match():
        report.cache_hits_match += 1

    try:
        matcher_result = match_with_cache(
            extractions,
            project_tree,
            cache,
            content_shas=content_shas,
            use_cache=use_cache,
            matcher_batch_size=matcher_batch_size,
            matcher_max_workers=matcher_max_workers,
            on_cache_hits_match=_hit_match,
            dirty_anchors_per_file=dirty_anchors_per_file,
        )
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"matcher failed: {e}")
        report.finished_at = datetime.now(tz=timezone.utc)
        return report

    # 9. Filter to dirty + build ReconcilePlans ---------------------------
    try:
        from _shared.io.sinks.jira import JiraSink
        from _shared.io.sinks.jira.strategies import StaticMapStrategy
        _resolver = StaticMapStrategy()
        _sink = JiraSink(
            client=jira,
            project_key=project_key,
            assignee_resolver=_resolver,
            filter_components=False,
        )
        dirty_sections = filter_dirty(
            matcher_result, extractions, dirty_anchors_per_file,
        )
        plans = build_plans_from_dirty(
            dirty_sections, sink=_sink, resolver=_resolver,
        )
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"build_plans failed: {e}")
        report.finished_at = datetime.now(tz=timezone.utc)
        return report

    for plan in plans:
        report.plans.append(plan)

    # 10. apply (or report-only) ------------------------------------------
    if apply and verify_before_apply:
        verify_outcome = _verify_gate(
            report, plans, jira=jira, source=source,
            verify_md_path=verify_md_path,
        )
        if verify_outcome is None:
            report.finished_at = datetime.now(tz=timezone.utc)
            return report

    # Count actions AFTER verify so skipped_by_user appears correctly.
    for plan in plans:
        for a in plan.actions:
            report.bump_action(a.kind)

    if apply:
        for plan in plans:
            f = next((df for df, _ in extractions if _.file_id == plan.file_id), None)
            if f is None:
                continue
            try:
                _apply_plan(
                    plan, drive_file=f, jira=jira, project_key=project_key,
                    target_epic=target_epic,
                )
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"apply failed for {plan.file_name}: {e}")

    # 11. Persist cursor + dump captured writes ---------------------------
    finished = datetime.now(tz=timezone.utc)
    report.finished_at = finished
    if apply and not capture_path and not report.errors:
        save_state(State(last_run_at=started, last_run_status="ok"), state_path)
    elif apply and not capture_path and report.errors:
        save_state(
            State(last_run_at=state.last_run_at, last_run_status="error"),
            state_path,
        )

    if capture_path:
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

    # Persist cache only when writes actually landed in Jira: same gate
    # as state.json. Capture-mode runs and dry-runs intentionally don't
    # save — saving them would lie about Jira state and cause `create_*`
    # actions to be silently skipped on the next warm run (Tier 2 hit
    # with dirty=∅) while Jira has no record of the issue.
    if use_cache and apply and not capture_path and not report.errors:
        try:
            cache.save(cache_path)
            logger.info(
                "cache: saved %d file entr(ies) (classify hits=%d, extract hits=%d)",
                len(cache.files), report.cache_hits_classify, report.cache_hits_extract,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("cache: save failed: %s", e)
    elif use_cache:
        logger.info(
            "cache: NOT saved (apply=%s capture=%s errors=%d) — "
            "preserves cache/Jira consistency",
            apply, bool(capture_path), len(report.errors),
        )
    return report


# ----------------------------------------------------------------------
# capture mode: monkey-patch the JiraClient HTTP write methods so the
# apply path runs without sending anything. Reads still go through.
# ----------------------------------------------------------------------


def _enable_capture(jira: JiraClient, sink: list[dict]) -> None:
    counter = {"n": 0}

    def fake_post(path: str, json_body: dict):
        counter["n"] += 1
        sink.append({"method": "POST", "path": path, "body": json_body})
        if path == "/issue":
            return {
                "id": str(counter["n"]),
                "key": f"CAPTURED-{counter['n']}",
                "self": "(captured)",
            }
        return {}

    def fake_put(path: str, json_body: dict) -> None:
        counter["n"] += 1
        sink.append({"method": "PUT", "path": path, "body": json_body})

    jira.post = fake_post  # type: ignore[method-assign]
    jira.put = fake_put  # type: ignore[method-assign]


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
    jira: JiraClient,
    project_key: str,
    target_epic: str | None = None,
) -> None:
    """Walk each EpicGroup; epic action first, then its task actions."""
    for group in plan.groups:
        if target_epic:
            epic_key = target_epic
        else:
            epic_key = _apply_epic_action(
                group.epic_action,
                drive_file=drive_file,
                jira=jira,
                project_key=project_key,
            )
        for task_action in group.task_actions:
            _apply_task_action(
                task_action,
                epic_key=epic_key,
                drive_file=drive_file,
                jira=jira,
                project_key=project_key,
            )


def _apply_epic_action(
    a: Action,
    *,
    drive_file: DriveFile,
    jira: JiraClient,
    project_key: str,
) -> str | None:
    if a.kind == "create_epic":
        extra: dict = {}
        if a.assignee_username:
            extra["assignee"] = {"name": a.assignee_username}
        created = jira.create_issue(
            project_key=project_key,
            summary=a.summary or "",
            description=a.description or "",
            issue_type="Epic",
            extra_fields=extra or None,
        )
        return created.get("key")
    if a.kind == "update_epic":
        live = get_issue(a.target_key, client=jira) or {}
        fields: dict = {
            "summary": a.summary,
            "description": finalize_body(
                a.description or "", live.get("description") or "",
            ),
        }
        if a.assignee_username:
            fields["assignee"] = {"name": a.assignee_username}
        jira.update_issue(a.target_key, fields)
        jira.post_comment(a.target_key, _comment_for(a, drive_file=drive_file, jira=jira))
        return a.target_key
    if a.kind == "noop":
        return a.target_key
    return None


def _apply_task_action(
    a: Action,
    *,
    epic_key: str | None,
    drive_file: DriveFile,
    jira: JiraClient,
    project_key: str,
) -> None:
    if a.kind == "create_task":
        extra: dict = {}
        if a.assignee_username:
            extra["assignee"] = {"name": a.assignee_username}
        created = jira.create_issue(
            project_key=project_key,
            summary=a.summary or "",
            description=a.description or "",
            issue_type="Task",
            epic_link=epic_key,
            extra_fields=extra or None,
        )
        return
    if a.kind == "update_task":
        live = get_issue(a.target_key, client=jira) or {}
        fields: dict = {
            "summary": a.summary,
            "description": finalize_body(
                a.description or "", live.get("description") or "",
            ),
        }
        if a.assignee_username:
            fields["assignee"] = {"name": a.assignee_username}
        jira.update_issue(a.target_key, fields)
        jira.post_comment(a.target_key, _comment_for(a, drive_file=drive_file, jira=jira))
        return
    # noop / orphan: nothing to write


def _comment_for(action: Action, *, drive_file: DriveFile, jira: JiraClient) -> str:
    live = get_issue(action.target_key, client=jira)
    return format_update_comment(
        assignee_username=live.get("assignee_username"),
        fallback_username=live.get("reporter_username"),
        drive_file=drive_file,
        before_summary=live.get("summary"),
        after_summary=action.summary,
        before_description=live.get("description"),
        after_description=action.description,
    )
