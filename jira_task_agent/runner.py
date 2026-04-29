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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .cache import (
    Cache,
    deserialize_extraction,
    file_content_sha,
    serialize_extraction,
)
from .drive.client import DriveFile, build_service, download_file, list_folder
from .jira.client import JiraClient
from .jira.project_tree import fetch_project_tree
from .pipeline.classifier import ClassifyResult, classify_file
from .pipeline.commenter import format_manual_edit_comment, format_update_comment
from .pipeline.context_bundler import bundle_root_context
from .pipeline.dedupe import find_duplicate_copies
from .pipeline.extractor import (
    ExtractionError,
    extract_from_file,
    extract_multi_from_file,
)
from .pipeline.matcher import (
    FileEpicResult,
    MatcherResult,
    compute_matcher_prompt_sha,
    compute_project_topology_sha,
    file_epic_result_from_json,
    file_epic_result_to_json,
    run_matcher,
)
from .pipeline.reconciler import (
    Action,
    EpicGroup,
    ProjectEpicsIndex,
    ReconcilePlan,
    build_plans_from_match,
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
    state_path: Path | None = None,
    cache_path: Path | None = None,
    use_cache: bool = True,
    only_file_name: str | None = None,
    target_epic: str | None = None,
    capture_path: str | None = None,
    matcher_batch_size: int = 4,
    matcher_max_workers: int = 3,
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

    folder_id = os.environ.get("FOLDER_ID")
    project_key = os.environ.get("JIRA_PROJECT_KEY")
    if not folder_id:
        report.errors.append("FOLDER_ID is not set in env")
        report.finished_at = datetime.now(tz=timezone.utc)
        return report
    if not project_key:
        report.errors.append("JIRA_PROJECT_KEY is not set in env")
        report.finished_at = datetime.now(tz=timezone.utc)
        return report

    state = load_state(state_path)
    cursor = since_override or state.last_run_at
    logger.info("run_once: cursor=%s apply=%s", cursor, apply)

    # 1+2. Drive list + download (no cursor; root files must be visible) ----
    drive_service = build_service()
    files = list_folder(folder_id, service=drive_service)
    report.files_total = len(files)
    if not files:
        logger.info("run_once: no Drive files in folder; nothing to do")
        report.finished_at = datetime.now(tz=timezone.utc)
        if apply:
            save_state(State(last_run_at=started, last_run_status="ok"), state_path)
        return report

    download_root = Path(download_dir)
    local_paths: dict[str, Path] = {}
    for f in files:
        try:
            p = download_file(f, download_root, service=drive_service)
            if p is not None:
                local_paths[f.id] = p
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"download failed for {f.name}: {e}")

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
        # Tier 2 cache: skip extract if content_sha unchanged.
        sha = content_shas.get(f.id, "")
        cached_payload = cache.get_extraction(f.id, sha) if sha else None
        if cached_payload is not None:
            try:
                ext = deserialize_extraction(cached_payload)
                extractions.append((f, ext))
                report.extractions_ok += 1
                report.cache_hits_extract += 1
                logger.info("cache: extract hit for %s", f.name)
                continue
            except Exception as e:  # noqa: BLE001
                # Bad cache entry — fall through to fresh extract.
                logger.warning(
                    "cache: extract payload for %s unusable (%s); re-extracting",
                    f.name, e,
                )

        try:
            if c.role == "single_epic":
                ext = extract_from_file(
                    f, local_path=local_paths[f.id], root_context=root_context
                )
            else:
                ext = extract_multi_from_file(
                    f, local_path=local_paths[f.id], root_context=root_context
                )
            extractions.append((f, ext))
            report.extractions_ok += 1
            try:
                cache.set_extraction(
                    file_id=f.id,
                    modified_time=f.modified_time.isoformat(),
                    content_sha=sha,
                    extraction_payload=serialize_extraction(ext),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("cache: failed to store extraction for %s: %s", f.name, e)
        except (ExtractionError, Exception) as e:  # noqa: BLE001
            report.extractions_failed += 1
            report.errors.append(f"extract failed for {f.name}: {e}")

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

    # 8. Matcher (with Tier 3 cache: per-file matcher decisions) ----------
    # Cache invariant: a cached decision is reusable when (content_sha,
    # prompt_sha) both match. The doc is the source of truth; dev edits
    # in Jira don't invalidate — their edits must stay. The reconciler's
    # per-issue manual-edit + status guards still protect the apply path
    # on doc-changed runs.
    #
    # Soft fallback: if a cached `matched_jira_key` no longer exists in
    # the current project_tree (epic was deleted), drop the cache entry
    # for that file and fall through to a fresh match.
    topology_sha = compute_project_topology_sha(project_tree)
    prompt_sha = compute_matcher_prompt_sha()
    live_epic_keys: set[str] = {
        e.get("key") for e in (project_tree.get("epics") or []) if e.get("key")
    }

    cached_results: list[FileEpicResult] = []
    fresh_extractions: list[tuple[object, object]] = []
    fresh_keys: list[tuple[str, str]] = []  # (file_id, content_sha) per fresh entry

    for drive_file, ext in extractions:
        fid = ext.file_id
        csha = content_shas.get(fid, "")
        cached = (
            cache.get_match(fid, content_sha=csha, prompt_sha=prompt_sha)
            if use_cache and csha
            else None
        )
        if cached is not None:
            # Validate referenced Jira keys still exist (soft fallback).
            stale_key = next(
                (
                    r.get("matched_jira_key")
                    for r in cached
                    if r.get("matched_jira_key")
                    and r["matched_jira_key"] not in live_epic_keys
                ),
                None,
            )
            if stale_key is not None:
                logger.info(
                    "matcher cache STALE for %s: cached match %s no longer in "
                    "project tree; re-running matcher for this file",
                    ext.file_name, stale_key,
                )
                cache.drop_match(fid)
                cached = None
        if cached is not None:
            this_file: list[FileEpicResult] = []
            ok = True
            for r in cached:
                try:
                    this_file.append(file_epic_result_from_json(r))
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "matcher cache: failed to deserialize entry for %s "
                        "(%s); treating as miss",
                        fid, e,
                    )
                    ok = False
                    break
            if ok:
                cached_results.extend(this_file)
                report.cache_hits_match += 1
                logger.info(
                    "matcher cache HIT: %s (%d section result(s))",
                    ext.file_name, len(this_file),
                )
                continue
        fresh_extractions.append((drive_file, ext))
        fresh_keys.append((fid, csha))

    fresh_results: list[FileEpicResult] = []
    if fresh_extractions:
        try:
            fresh_matcher_result = run_matcher(
                fresh_extractions,
                project_tree,
                batch_size=matcher_batch_size,
                max_workers=matcher_max_workers,
            )
            fresh_results = list(fresh_matcher_result.file_results)
            logger.info(
                "matcher: %d epic decision(s) across %d uncached file(s) "
                "(cache hits: %d)",
                len(fresh_results), len(fresh_extractions), report.cache_hits_match,
            )
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"matcher failed: {e}")
            report.finished_at = datetime.now(tz=timezone.utc)
            return report

        # Persist fresh decisions back to the cache, grouped per file.
        results_by_file_id: dict[str, list[dict]] = {}
        for fr in fresh_results:
            results_by_file_id.setdefault(fr.file_id, []).append(
                file_epic_result_to_json(fr)
            )
        if use_cache:
            for fid, csha in fresh_keys:
                # Look up the corresponding extraction's mtime for cache bookkeeping.
                ext = next(
                    (e for _, e in fresh_extractions if e.file_id == fid), None
                )
                drive_file_for_id = next(
                    (df for df, e in fresh_extractions if e.file_id == fid), None
                )
                mtime_iso = (
                    drive_file_for_id.modified_time.isoformat()
                    if drive_file_for_id is not None
                    else ""
                )
                cache.set_match(
                    file_id=fid,
                    modified_time=mtime_iso,
                    content_sha=csha,
                    prompt_sha=prompt_sha,
                    topology_sha=topology_sha,
                    results=results_by_file_id.get(fid, []),
                )
    else:
        logger.info(
            "matcher: 0 LLM calls — all %d file(s) served from Tier 3 cache",
            report.cache_hits_match,
        )

    matcher_result = MatcherResult(file_results=cached_results + fresh_results)

    # 9. Build ReconcilePlans -----------------------------------------------
    try:
        plans = build_plans_from_match(matcher_result, extractions, client=jira)
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"build_plans failed: {e}")
        report.finished_at = datetime.now(tz=timezone.utc)
        return report

    for plan in plans:
        report.plans.append(plan)
        for a in plan.actions:
            report.bump_action(a.kind)

    # 10. apply (or report-only) ------------------------------------------
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

    # Persist cache (best-effort; failures don't fail the run).
    if use_cache:
        try:
            cache.save(cache_path)
            logger.info(
                "cache: saved %d file entr(ies) (classify hits=%d, extract hits=%d)",
                len(cache.files), report.cache_hits_classify, report.cache_hits_extract,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("cache: save failed: %s", e)
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
        new_key = created.get("key")
        if new_key and drive_file.web_view_link:
            jira.add_remote_link(
                new_key,
                url=drive_file.web_view_link,
                title=f"Source doc: {drive_file.name}",
            )
        return new_key
    if a.kind == "update_epic":
        fields: dict = {"summary": a.summary, "description": a.description}
        if a.assignee_username:
            fields["assignee"] = {"name": a.assignee_username}
        jira.update_issue(a.target_key, fields)
        jira.post_comment(a.target_key, _comment_for(a, drive_file=drive_file, jira=jira))
        return a.target_key
    if a.kind == "skip_manual_edits":
        jira.post_comment(a.target_key, format_manual_edit_comment(drive_file))
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
        new_key = created.get("key")
        if new_key and drive_file.web_view_link:
            jira.add_remote_link(
                new_key,
                url=drive_file.web_view_link,
                title=f"Source doc: {drive_file.name}",
            )
        return
    if a.kind == "update_task":
        fields: dict = {"summary": a.summary, "description": a.description}
        if a.assignee_username:
            fields["assignee"] = {"name": a.assignee_username}
        jira.update_issue(a.target_key, fields)
        jira.post_comment(a.target_key, _comment_for(a, drive_file=drive_file, jira=jira))
        return
    if a.kind == "skip_manual_edits":
        jira.post_comment(a.target_key, format_manual_edit_comment(drive_file))
        return
    # noop / orphan: nothing to write


def _comment_for(action: Action, *, drive_file: DriveFile, jira: JiraClient) -> str:
    from .jira.client import get_issue
    live = get_issue(action.target_key, client=jira)
    return format_update_comment(
        assignee_username=live.get("assignee_username"),
        fallback_username=live.get("reporter_username"),
        drive_file=drive_file,
        before_summary=action.before_summary,
        after_summary=action.summary,
        before_description=action.before_description,
        after_description=action.description,
    )
