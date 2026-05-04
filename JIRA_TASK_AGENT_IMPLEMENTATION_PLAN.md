# Jira Task Agent — Implementation Plan

See [JIRA_TASK_AGENT_OVERVIEW.md](./JIRA_TASK_AGENT_OVERVIEW.md) for the goal
and [PIPELINE_FLOW.md](./PIPELINE_FLOW.md) for the canonical end-to-end flow,
cache layout, and per-mutation expectations.

## 1. Goal

Automate the path from "task written in a planning doc" to "ticket in Jira."
The agent runs **on a schedule** (every X hours, unattended) **or manually**
(one-shot from the CLI). It scans **two configurable sources** —
a Google Drive folder and/or a local `data/local_files/` directory — for
documents touched since the last run, classifies each file via an LLM,
extracts task items, compares them to the Jira backlog via a two-stage LLM
matcher, then **creates** new issues and **updates** existing ones — leaving
a tagged audit comment on every change.

Operators don't pass IDs/paths on every run — `FOLDER_ID`, `JIRA_HOST`,
`JIRA_PROJECT_KEY`, etc. are set once in `.env` and read automatically.
CLI flags are reserved for **per-run overrides** (time filter, dry-run,
target epic, source mode).

## 2. High-Level Flow

The same `run_once()` body is invoked from two entry points; the run logic
itself is identical.

```
┌─────────────┐                   ┌──────────────────┐
│  Scheduler  │  every X hours    │                  │
│  (cron /    │ ───────────────►  │   run_once()     │
│   launchd / │                   │   (single agent  │
│   APSched)  │                   │   pipeline)      │
└─────────────┘                   │                  │
                                  │                  │
┌─────────────┐  manual CLI:      │                  │
│  Operator   │  `python -m       │                  │
│  (you)      │  jira_task_agent  │                  │
│             │  run`            ─┼──►  ...          │
└─────────────┘                   └──────────────────┘
                                         │
   1. LIST + DOWNLOAD files from configured sources
        - Drive folder (modifiedTime > last_run_at)
        - data/local_files/ (mtime > last_run_at)
        - dedupe by content hash
                                         ▼
   2. CLASSIFY each file via LLM   →  role: single_epic | multi_epic | root | skip
                                         ▼
   3. AGGREGATE root files         →  shared context bundle
                                         ▼
   4. For each task-bearing file (per-file caching):
        a. EXTRACT (extract_or_reuse)  →  (extraction, dirty_anchors)
              ▸ Tier 2 hit (content unchanged)   → reuse, dirty = ∅
              ▸ cached + content changed         → diff path, dirty = exact set
              ▸ no cache                         → cold full extract, dirty = None
        b. MATCH (match_with_cache)
              ▸ Tier 3 hit (full re-use)         → reuse cached decisions
              ▸ partial (dirty present)          → re-Stage-1 processed sections,
                                                   re-Stage-2 dirty tasks only,
                                                   splice cached decisions
              ▸ fresh                            → run_matcher end-to-end
                                         ▼
   5. FILTER (dirty_filter.filter_dirty)
        →  list[DirtySection] — only sections with dirty epic OR dirty tasks
                                         ▼
   6. RECONCILE (build_plans_from_dirty) — pure mapping from matcher
       decisions to write actions: create_epic / update_epic / create_task /
       update_task / covered_by_rollup / skip_completed_epic. No body
       comparison, no extra Jira reads.
                                         ▼
   7. APPLY (--apply) or CAPTURE (--capture file.json)
        + post changelog comment on every update
                                         ▼
   8. PERSIST cursor (last_run_at, last_run_status) + cache (extraction,
        file_text, matcher result, project topology sha)
```

## 3. File classification

The classifier reads each file and produces a role. Same filename can carry
different roles across the folder (we have multiple files literally named
`May1_Initial_Version_Tasks.md` — one root, the rest task sources).

| Role          | Meaning                                                                 | Drives writes? |
|---------------|-------------------------------------------------------------------------|----------------|
| `single_epic` | A single vertical/feature task list. One epic + its child tasks.        | yes |
| `multi_epic`  | A "release plan" doc with multiple `## A. …` / `## B. …` sub-epics, each owning its own tasks. | yes (one epic per section) |
| `root`        | Background/context (e.g. master release plan, vertical-tasks plan). Used as input context to enrich extracted bodies. | no |
| `skip`        | Not relevant (presentations, changelogs, untriaged drafts).             | no |

## 4. Doc structure

**`single_epic` files:**

```
<file title>
<top section — highlights / overview / context>      ← becomes epic description
...
<heading or table introducing the task list>          ← boundary (LLM-detected)
- Task 1 ...
- Task 2 ...
```

**`multi_epic` files (e.g. `May1_Initial_Version_Tasks.md`):**

```
# Release plan title
<intro paragraph(s) — root context for every sub-epic>

## A. <Sub-epic A title>
<sub-epic A overview>
- A-Task 1
- A-Task 2

## B. <Sub-epic B title>
<sub-epic B overview>
- B-Task 1
...
```

Each `## ` (or `### `) heading whose body contains task bullets becomes one
epic. The extractor returns `{epics: [{summary, description, tasks: [...]}]}`.

## 5. Epic & task generation rules

### Epic — one task-bearing section = one epic

| Field          | Source                                                                 |
|----------------|------------------------------------------------------------------------|
| **Summary**    | LLM-derived from the section's overview/highlights, not the filename. No `V<N>_` / `_Tasks` artifacts. |
| **Description**| Highlights/overview of that section + a distilled relevant slice of the root-context bundle. |
| **Issue type** | Epic                                                                   |
| **Project**    | `JIRA_PROJECT_KEY` from `.env`                                         |
| **Source URL** | Embedded in the description footer + changelog comment as plain text. The agent does not write Jira remote-links. |

### Tasks — each task line becomes a Jira task

| Field          | Source                                                                 |
|----------------|------------------------------------------------------------------------|
| **Summary**    | LLM-generated, informative — ticket-title quality, not the raw bullet. |
| **Description**| LLM-generated, comprehensive. Must include `### Definition of Done`.   |
| **Issue type** | Task (configurable).                                                   |
| **Epic Link**  | The matched epic (Stage 1) — or, if no match, the just-created epic.   |
| **Source URL** | Embedded in the description footer + changelog comment as plain text. No remote-link write. |
| **`source_anchor`** | Stable identifier produced by the extractor. Threads through caching, dirty detection, and matcher splice. |

### Description template (enforced by extractor prompt)

```
<comprehensive context: what + why, drawn from task line + root files>

### Acceptance criteria
- ...

### Definition of Done
- [ ] Code merged, reviewed
- [ ] Tests cover the change
- [ ] <task-specific gates>

### Source
- Doc: <doc name> (<link>)
- Last edited by: <last_modifying_user>
```

The DoD requirement is enforced both by the prompt and by a post-validation
check (`pipeline/extractor.py`).

## 6. Reconcile path: filter_dirty → build_plans_from_dirty

The reconciler is a pure action mapper — it does not call the LLM, does not
compare bodies, does not read Jira beyond what's already in the matcher
result. The "doc is source of truth" rule is enforced **before** it.

### Identification rule

Doc-to-Jira pairing is decided by the LLM matcher (Stage 1 epic + Stage 2
task) and persisted in the Tier 3 cache. `ai-generated` is a content
label only, never an identifier. The source-doc URL appears in the
description footer + changelog comment as plain text for human
navigation; the agent does not write Jira remote-links.

### Step 1 — filter_dirty

`pipeline/dirty_filter.py::filter_dirty(matcher_result, extractions,
dirty_anchors_per_file) → list[DirtySection]`

A section is kept if **any** of:
- the `<epic>:N` token is in the file's `dirty_anchors`,
- it has at least one task whose `source_anchor` is in `dirty_anchors`,
- it is brand-new this run (index ≥ len(cached file_results)),
- the file's `dirty_anchors` is `None` (cold path: process everything).

`DirtySection` carries `epic_dirty: bool` so the reconciler can decide
whether to issue an epic write (versus pass-through for child tasks only).

### Step 2 — build_plans_from_dirty

`pipeline/reconciler.py::build_plans_from_dirty(sections, *, client) →
list[ReconcilePlan]`

Per `DirtySection`:

- **Epic action**
  - `matched_jira_key is None`            → `create_epic`
  - matched, status in completed set      → `skip_completed_epic`
  - matched, `epic_dirty=False`           → `noop` (preserves target_key for tasks)
  - matched, `epic_dirty=True`            → `update_epic` (writes new summary AND description)
- **Task actions** (only the dirty ones reach this point)
  - `candidate_key is None`               → `create_task`
  - same key cited by ≥2 dirty tasks      → first → `update_task`, rest → `covered_by_rollup`
  - else                                  → `update_task`

Status guard: `_COMPLETED_EPIC_STATUSES = {In Staging, In Review, Done,
Closed, Resolved, Cancelled, Won't Do, Won't Fix}`. Active: `Backlog`,
`In Progress`.

### Per-run Jira fetch

`fetch_project_tree` issues one paginated `/search` returning all epics +
their direct children grouped locally. The reconciler does **not** issue
additional Jira reads (orphan reporting comes from the matcher's leftover
candidates).

### Agent-touch marker

Every agent-written description ends with:

```
<!-- managed-by:jira-task-agent v1 -->
```

Indicator only — never used as a write gate. The doc is the source of
truth: a doc edit that maps to an existing issue triggers `update_*`
regardless of who last authored the body.

## 7. Update flow + Jira comment with @mention

Every create/update posts a comment on the issue (LLM-summarized
changelog), with `[~currentAssigneeUsername]` mention. Unassigned →
fall back to reporter; both empty → omit mention with a warning prepended.

## 8. Architecture — components

| Component                       | Responsibility                                                          |
|---------------------------------|--------------------------------------------------------------------------|
| `drive/client`                  | Drive list/download AND `list_local_folder(data/local_files)`. Local files get id `local::<filename>` and `file://` URIs. |
| `jira/client`                   | Jira reads + writes; markdown→wiki at the boundary; `_enable_capture` for dry-runs.|
| `jira/project_tree`             | One paginated `/search` returning all project epics + their direct children. |
| `pipeline/classifier`           | LLM call: file → `single_epic` / `multi_epic` / `root` / `skip`.        |
| `pipeline/context_bundler`      | Length-bounded concat of root files.                                   |
| `pipeline/extractor`            | Cold extract (single + multi) and the two diff prompts: `extract_diff` (labels) + `extract_targeted` (full bodies). |
| `pipeline/file_extract`         | `extract_or_reuse` — Tier 2 hit / diff path / cold; returns `(extraction, dirty_anchors)`. Owns `apply_changes` + `compute_dirty`. |
| `pipeline/matcher`              | Two-stage LLM matcher (Stage 1: epics; Stage 2: grouped tasks, batched). Public helpers: `epic_candidates_from_tree`, `task_candidates_from_children`. |
| `pipeline/file_match`           | `match_with_cache` — Tier 3 hit / partial (dirty-only) / fresh. Always re-runs Stage 1 for processed sections (self-heals stochastic Stage-1 misses). |
| `pipeline/dirty_filter`         | `filter_dirty` → `list[DirtySection]`. Drops every section without dirty content. |
| `pipeline/reconciler`           | `build_plans_from_dirty` → action plans. Pure logic, no Jira reads, no body comparison. |
| `pipeline/commenter`            | Composes the changelog comment + resolves `[~username]`.                |
| `pipeline/dedupe`               | Content-hash de-twinning (Drive-export vs local copy collisions).       |
| `state`                         | `state.json` cursor.                                                    |
| `cache`                         | `cache.json` — Tier 1/2/3 caches + `diff_payload.file_text`.            |
| `runner`                        | Orchestrates the flow. Three source modes (`gdrive` / `local` / `both`). |
| `__main__`                      | CLI: `--apply`, `--since`, `--only`, `--target-epic`, `--capture`, `--no-cache`, `--source`, `--local-dir`. |

## 9. Persistence

### `state.json`

```json
{ "last_run_at": "2026-04-27T13:42:11+03:00", "last_run_status": "ok" }
```

Single cursor. Losing it costs at most one full run.

### `cache.json` (version 2)

Per file id:
- **Tier 1 — classify cache**, key `(file_id, modified_time)`.
- **Tier 2 — extract cache**, key `(file_id, content_sha)`. Stores
  `extraction_payload` JSON.
- **Tier 3 — matcher cache**, key `(file_id, content_sha,
  project_topology_sha, matcher_prompt_sha)`. Stores `matcher_payload`
  (one `FileEpicResult` per section, including `task_anchors`).
- **`diff_payload`** — `{"file_text": "..."}` — the cached file content,
  used by the diff path's `unified_diff` invocation.

Atomic writes via tempfile+rename. `compute_matcher_prompt_sha` invalidates
matcher cache on any prompt or model-id change. `compute_project_topology_sha`
invalidates on any structural change in CENTPM (epic add/remove/rename,
child summary or status change, description rewrites).

## 10. LLM contracts

**Provider:** NVIDIA Inference (OpenAI-compatible). Client wrapper
`llm/client.py` exposes `chat_json(model, system, user, schema)` and uses
JSON mode + Pydantic validation + retry-on-invalid-JSON.

### Configuration

```
NVIDIA_API_KEY=...
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
LLM_MODEL_CLASSIFY=meta/llama-3.3-70b-instruct
LLM_MODEL_EXTRACT=nvidia/llama-3.1-nemotron-70b-instruct
LLM_MODEL_SUMMARIZE=meta/llama-3.3-70b-instruct
```

### Prompts (in `jira_task_agent/llm/prompts/`)

| File                       | Purpose                                                                   |
|----------------------------|----------------------------------------------------------------------------|
| `classifier.txt`           | File → `{role, confidence, reason}`.                                       |
| `extractor_single.txt`     | Cold full extract for `single_epic` files.                                 |
| `extractor_multi.txt`      | Cold full extract for `multi_epic` files.                                  |
| `extractor_diff.txt`       | Diff path — **labels only**: `{modified_anchors, removed_anchors, added, new_subepics, epic_changed}`. No bodies, no DoD. Inputs: cached extraction + unified diff. |
| `extractor_targeted.txt`   | Diff path — full Jira-quality bodies for the items the labels named. Output: `{tasks, epics}` with full descriptions. |
| `matcher.txt`              | Stage 1 (epic) and Stage 2 (task) matching. Stage 2 leans toward PAIRING — false-pair is bounded; missed-pair = duplicate Jira issue. Description preview is 3000 chars to spot rollup epic descriptions. |
| `matcher_grouped.txt`      | Stage 2 grouping prompt (4 epics per call, parallel 3 workers).            |
| `summarizer.txt`           | Comment changelog — 2-5 plain-text bullets.                                |

### Diff-aware extract pipeline (the doc-as-truth rule)

When a file is cached and content changed:

1. `unified_diff(cached_file_text, current_text)` — pure Python,
   deterministic.
2. **`extract_diff` LLM call** — labels only. Returns the structural
   verdict: which anchors moved, which were removed, which were added,
   which sub-epics are brand-new, whether the epic body changed.
3. **`extract_targeted` LLM call** — given those targets, produces full
   Jira-quality bodies for ONLY the changed items.
4. `apply_changes(cached, labels, bodies, drive_file)` — pure Python merge.
   Cached items kept verbatim except for the named slices. Section keys
   are matched case+whitespace-insensitively (`_normalize_section`) so
   the two LLM calls don't have to agree on title casing.
5. `compute_dirty(cached, merged) → set[str]` — pure Python; returns task
   `source_anchor` strings + `"<epic>:N"` tokens for changed epics.

### Matcher partial path (Tier 3 partial reuse)

Triggered when `dirty_anchors` is non-empty and there's a cached matcher
result for the file. For each section:

- Brand-new section, or any task in dirty, or epic token in dirty
  → re-run **Stage 1** for that section's epic (always, even if cached
  matched it before; this self-heals stochastic Stage-1 misses).
- For matched epics with `epic_dirty=False` and only some tasks dirty
  → run Stage 2 on dirty tasks only; splice fresh decisions into the
  cached `MatchDecision` list using `task_anchors` as keys.
- Matched epic + any structural change → run Stage 2 on the full task list.
- Untouched sections → cached `FileEpicResult` returned byte-for-byte.

## 11. Implementation phases — current progress

Legend: ✅ done · 🟡 partial · ⏳ not started

### Phase 0 — scaffolding ✅
Repo + venv + `.env`; Drive + Jira clients; markdown→wiki at the boundary.

### Phase 1 — classifier ✅
Four-role classifier (`single_epic` / `multi_epic` / `root` / `skip`),
≥0.95 confidence on the live folder.

### Phase 2 — extractor ✅
- Single + multi cold extractors with DoD validator + composite-owner injection.
- Two-prompt diff path: `extractor_diff` (labels) + `extractor_targeted` (bodies).
- `apply_changes` + `compute_dirty` in `pipeline/file_extract`.

### Phase 3 — Jira write primitives ✅
`create_issue` (auto Epic Name + `ai-generated` label + Epic Link),
`update_issue`, `post_comment`, `transition_issue`.
Smoke-tested on CENTPM-1253.

### Phase 4 — commenter + change-tracking ✅
`format_update_comment` with `[~name]` mention + LLM-summarized bullets.
HTML marker on every agent-written description.

### Phase 5 — comparator ✅
- Two-stage LLM matcher with Tier 3 caching.
- `dirty_filter` produces `list[DirtySection]`.
- `build_plans_from_dirty` — pure mapping from matcher decisions to actions.
  No body comparison, no `list_epic_children`, no Jira reads.

### Phase 6 — orchestrator + cursor + capture ✅
- `run_once` end-to-end with `--source {gdrive,local,both}` (default `both`),
  `--local-dir`, `--apply`, `--since`, `--only`, `--target-epic`,
  `--capture`, `--no-cache`.
- `data/local_files/` source: `list_local_folder` scans `*.md`/`*.html`/`*.txt`,
  ids `local::<filename>`, `file://` URIs.
- `state.json` cursor + per-run `run_plan.json`.

### Phase 7 — testing ✅
- 90+ unit + logical tests (helpers, dedupe, md→wiki, matcher unit/grouped,
  reconciler scenarios, file_extract apply_changes/compute_dirty,
  file_match three branches, dirty_filter, local source).
- 8 live integration tests (classifier, extractor, matcher with hardcoded
  + real-snapshot data).
- **End-to-end live capture tests** (`pytest -m live`):
  - `test_may1_extract_diff_live.py` — parametric over May1/V11/NemoClaw/V0_Lior;
    asserts `dirty_anchors` exactness on each scenario.
  - `test_may1_match_dirty_live.py` — Stage 1/Stage 2 invocation count;
    cached decisions preserved byte-for-byte for clean tasks.
  - `test_may1_full_pipeline_live.py` — May1's 3 mutations → exactly 7
    captured Jira ops (1 update + 4 create_task + 1 create_epic + 1 comment).
    Strict enforcement.
  - `test_mixed_warm_and_new_live.py` — May1 warm + V11 cold in one pipeline
    run; asserts the warm file produces only its 5 mutation-driven writes
    while the cold file gets full create-everything treatment.
  - `test_warm_scenarios_live.py` — broader warm-run scenarios per file.

### Phase 8 — efficiency caching tiers ✅
1. ✅ **Tier 1** — classification cache, key `(file_id, modified_time)`.
2. ✅ **Tier 2** — extraction cache, key `(file_id, content_sha)`.
3. ✅ **Tier 3** — matcher cache, key `(file_id, content_sha,
   project_topology_sha, matcher_prompt_sha)`. Reuses entire matcher
   result when file content + Jira topology + matcher prompt all unchanged.
4. ✅ **Diff path** — `diff_payload.file_text` enables `unified_diff` on
   warm runs; two-prompt LLM extract returns only the changed slices;
   `dirty_anchors` gates downstream stages.
5. ✅ **Partial matcher path** — Stage 1 only re-runs for processed
   sections; Stage 2 only for dirty tasks; cached decisions reused
   byte-for-byte for everything else.

### Phase 9 — production rollout ⏳
1. Run `--report-only` against the real folder for ≥2 weeks. Operator
   reviews `run_plan.json` daily.
2. Tune classifier / extractor / matcher prompts on real misses.
3. Flip to `--apply`. Keep a `--pause` flag for emergencies.

### Phase 10 — scheduling + ops ⏳
1. `python -m jira_task_agent watch --interval 4h` (or cron / launchd
   snippet in `docs/deploy.md`).
2. Backoff/retry on Drive 5xx/429 and Jira 5xx/429.
3. (Optional) Post the run report to Slack via webhook.

## 12. Risks & residual open questions

| # | Risk / Question                                                                          | Default / Mitigation                                                                  |
|---|------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------|
| 1 | Classifier mis-labels a context file as a task-bearing file → spurious epic created.     | Confidence threshold; below it, route to report-only queue for human review.           |
| 2 | Multiple files share the name `May1_Initial_Version_Tasks.md`.                           | Content-hash dedupe; if multiple `single_epic`/`multi_epic` classifications collide on cleaned-name, prefer the most recently edited; flag the others. |
| 3 | Jira `@mention` username (`name`) differs from email/displayName.                        | Resolve once via `/issue/{key}` and cache per assignee.                                |
| 4 | Doc edits overwrite hand-edits made directly in Jira.                                    | **By design.** Doc is source of truth — agent updates and posts a changelog comment naming the source doc + last editor. Prior body lives in Jira's edit history. |
| 5 | Stage 1 LLM stochasticity — clear matches sometimes return None.                         | Matcher partial path always re-runs Stage 1 for processed sections (self-heals on next warm run, before any duplicate-create can happen). |
| 6 | Bumping `matcher.txt` / `matcher_grouped.txt` / model IDs invalidates Tier 3 cache project-wide. | Intended: prompt-sha is part of the matcher cache key so behavior changes can't be silently masked. Cost is one full re-match; one-time per prompt change. |
| 7 | Token cost of LLM calls.                                                                 | `last_run_at` cursor pre-filters; Tier 1/2/3 caches collapse warm-run cost; diff path collapses warm-run extract cost; partial matcher collapses warm-run match cost. |
| 8 | Task files deleted/moved in the source folder.                                           | **OPEN.** Default: leave existing Jira issues alone, surface in the report.            |
| 9 | Issue type for child tasks — always `Task`, or sometimes `Story`?                        | **OPEN.** Default to `Task`; allow extractor to suggest `Story`.                       |

## 13. Definition of Done — for the agent

1. Given a folder where the LLM correctly classifies the `V*_*.md` files as
   `single_epic`, the multi-section release plan as `multi_epic`, NVIS /
   the master May1 as `root`, kickoff html as `skip`, the agent creates one
   epic per task-bearing section under `JIRA_PROJECT_KEY`, each with a
   description that contains highlights + relevant root context, and child
   tasks each containing a populated **Definition of Done** section.
2. Re-running the agent against the same files creates **zero** new
   issues and posts **zero** comments — Tier 2 + Tier 3 caches collapse
   the run to read-only.
3. Editing one task line in a doc and re-running:
   - The diff path tags exactly that one task as dirty.
   - Updates exactly that one Jira task.
   - Posts exactly one comment, tagging the current assignee with `[~name]`.
4. Adding a new section + N tasks to a `multi_epic` doc and re-running:
   - The diff path tags `<epic>:N` + N task anchors as dirty.
   - Creates 1 epic + N tasks; 0 comments (creates aren't commented).
     Other sections stay untouched.
5. Manually editing a Jira description (outside the agent) and re-running
   results in the doc-side update being applied, with a changelog comment
   naming the source doc + last editor. Prior content is preserved in
   Jira's edit history.
6. The agent runs unattended for 7 days on its schedule with no manual
   intervention; report stays green.
7. A manual one-shot (`python -m jira_task_agent run`) executed on the same
   environment produces an identical Plan to the scheduled run that would
   fire at the same moment — i.e. the two entry points are interchangeable
   and configuration-driven, not flag-driven.
