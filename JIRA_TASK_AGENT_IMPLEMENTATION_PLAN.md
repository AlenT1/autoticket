# Jira Task Agent — Implementation Plan

See [JIRA_TASK_AGENT_OVERVIEW.md](./JIRA_TASK_AGENT_OVERVIEW.md) for the goal and high-level flow.

## 1. Goal

Automate the path from "task written in a planning doc" to "ticket in Jira."
The agent can run **on a schedule** (every X hours, unattended) **or be
triggered manually** (one-shot from the CLI). Either way it scans a
**pre-configured** Google Drive folder for documents updated since the last
run, classifies each file via an LLM, extracts task items from "task files"
using the surrounding "root/context files" as background, compares them to
the Jira backlog, then **creates** new issues and **updates** existing ones —
leaving a tagged audit comment on every change.

Operators don't pass a folder ID on every run — `FOLDER_ID`, `JIRA_HOST`,
`JIRA_PROJECT_KEY`, etc. are set once in `.env` (or `config.yaml`) and read
automatically by both modes. CLI flags are reserved for **per-run overrides**
(time filter, dry-run, target epic, etc.) — never required.

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
   1. LIST + DOWNLOAD Drive files (one pass, filtered by
      modifiedTime > last_run_at; saved as files/<id>__<name>)
                                         ▼
   2. CLASSIFY each file via LLM   →  role: task | root | skip
                                         ▼
   3. AGGREGATE root files         →  shared context bundle
                                         ▼
   4. For each TASK file:
        a. EXTRACT epic + tasks via LLM (uses root bundle as context)
        b. RECONCILE with Jira (resolve epic via state DB; list its children)
        c. CREATE new / UPDATE changed / NO-OP unchanged
        d. POST comment + @mention assignee on every update
                                         ▼
   5. PERSIST cursor (last_run_at, last_run_status) — that's all
```

## 3. File classification

The Drive folder is a mixed bag. The agent doesn't try to identify file role
from filename or path — instead, an **LLM classifier skill** reads each file
and decides:

| Role     | Meaning                                                                 | Drives writes? |
|----------|-------------------------------------------------------------------------|----------------|
| `task`   | A vertical/feature task list. Top of file = highlights/overview, body has the task table/list. | yes — creates an epic + child tasks |
| `root`   | Background/context (e.g. `NVIS_Central_Vertical_Tasks_Plan.md`, the master `May1_Initial_Version_Tasks.md`). Used only as input context to enrich epics/tasks generated from `task` files. | no |
| `skip`   | Not relevant (presentations, changelogs, untriaged drafts).             | no |

The classifier prompt is the agent's "brain" for this stage — it must be
authoritative enough that we never need a filename-based fallback. **Same
filename can have different roles** (we already have 5 files literally called
`May1_Initial_Version_Tasks.md` — one of them is root, the rest are task
sources).

## 4. Doc structure (assumption for `task` files)

Every task file follows this shape:

```
<file title>
<top section — "highlights / overview / context">      ← becomes part of the EPIC description
...

<a heading or table that introduces the task list>     ← the boundary
- Task 1 ...                                           ← becomes a child task
- Task 2 ...
- ...
```

The extractor prompt is told this convention explicitly. The split between
"highlights" and "tasks" is detected by the LLM, not by a hardcoded regex
against headings — we trust the prompt to find the boundary.

## 5. Epic & task generation rules

### Epic — 1 task file = 1 epic

| Field          | Source                                                                 |
|----------------|------------------------------------------------------------------------|
| **Summary**    | **LLM-derived from the file's overview/highlights, not the filename.** No `V<N>_` / `Vx` prefix; no `_Tasks` suffix. The summary should read like a real epic title that a stakeholder would write — e.g. `V2_CentARB_Tasks.md` becomes something like `CentARB Vertical — Production Readiness & Conversation Flow`, not `V2 CentARB Tasks`. The filename is only used as a hint to the prompt; the overview content is the authoritative source. |
| **Description**| Highlights/overview from the top of the task file **+** distilled relevant slice of the root-context bundle (LLM picks what's relevant). |
| **Issue type** | Epic                                                                   |
| **Project**    | `JIRA_PROJECT_KEY` from `.env`                                         |
| **Remote link**| Source Drive file (`webViewLink`). Discoverable back-pointer in the Jira UI; also the agent's only on-Jira marker for "this epic was created from this doc" (see §6). |

### Tasks — each task line in the file becomes a Jira task

| Field          | Source                                                                 |
|----------------|------------------------------------------------------------------------|
| **Summary**    | LLM-generated, *highly informative* — not the raw bullet text. Should read like a Jira ticket title, not a doc TODO. |
| **Description**| LLM-generated, comprehensive. **Must include a "Definition of Done" section** rich enough that a human reviewer could break the task into subtasks without re-reading the source doc. Uses task line + relevant root context. |
| **Issue type** | Task (default; configurable)                                           |
| **Epic Link**  | The epic associated with this `task` file. **May already exist** from a prior run — resolved fresh each run by remote-link lookup (see §6). Only created in this run when no existing epic is found. New tasks discovered in an updated file are added under that **same** epic, never under a new one. |
| **Remote link**| Source Drive file (`webViewLink`). Same role as on the epic — discoverable back-pointer + the agent's only on-Jira marker. |

### Description template (enforced by extractor prompt)

```
<comprehensive context: what + why, drawn from task line + root files>

### Acceptance criteria
- ...
- ...

### Definition of Done
- [ ] Code merged, reviewed
- [ ] Tests cover the change
- [ ] <task-specific gates from the extractor>

### Source
- Doc: <doc name> (<drive link>)
- Last edited by: <last_modifying_user>
```

The "highly informative" requirement is encoded in the extractor prompt + a
post-validation check (min description length, presence of "Definition of Done"
heading).

## 6. Reconcile: create / update / no-op

For each `task` file the agent has freshly extracted `{epic, [tasks...]}` in
memory. The matching decisions are made by an **LLM matcher** (no fuzzy
ratios, no string heuristics); the reconciler then turns those decisions
into write actions, entirely from live Jira state — no local cache, no hashes.

**Identification rule:** every Jira issue the agent creates carries a
**remote link** back to the source Drive file (`webViewLink`). That remote
link is the agent's only on-Jira marker. It's also useful to humans browsing
the issue in the UI.

### Matcher (two-stage, batched LLM)

Lives in `pipeline/matcher.py`. Run once per pipeline invocation against the
full project tree (`fetch_project_tree`):

- **Stage 1 — epic matching.** One LLM call pairs ALL extracted epics
  (across all files in this run) against ALL project epics. Output: a
  `(extracted_epic → jira_key | None, confidence, reason)` decision per
  extracted epic. Confidence floor `_MIN_CONFIDENCE = 0.70`; below that the
  decision is treated as "no match."
- **Stage 2 — task matching.** Grouped per matched epic, batched 4 epics
  per LLM call, run in parallel up to 3 workers. For each group the LLM is
  scoped to that epic's children only — items in group A can only match
  group A's candidates. The same candidate key may be cited by multiple
  extracted tasks (rollup pattern); reconciler turns that into
  `covered_by_rollup`.

Status guard: epics in `_COMPLETED_EPIC_STATUSES` (`In Staging`, `In Review`,
`Done`, `Closed`, `Resolved`, `Cancelled`, `Won't Do`, `Won't Fix`) emit
`skip_completed_epic` and are not touched. `Backlog` and `In Progress` are
active.

### Reconciler (pure-logic action emitter)

Given the matcher's decisions, `build_plans_from_match` produces one
`EpicGroup` per extracted epic with the following `Action.kind` values:

- `create_epic` — extracted epic has no match (or below confidence floor).
- `update_epic` — match found, descriptions differ after normalization
  (extracted markdown is run through `_md_to_jira_wiki` first so we compare
  apples-to-apples).
- `noop` — match found, descriptions equivalent.
- `skip_completed_epic` — matched epic is in a terminal status; no writes.
- `create_task` — extracted task with no candidate from Stage 2.
- `update_task` — candidate found, content differs.
- `covered_by_rollup` — multiple extracted tasks cite the same candidate
  (rollup-style child); no per-task write, just reported.
- `orphan` — existing child has no extracted-task counterpart this run.
  Report-only; never deleted, never commented on.

### Per-run Jira fetch (bounded, complete)

Tasks are reached only via their epic. `fetch_project_tree` issues one
paginated `/search` returning all epics + their direct children grouped
locally. Per run we therefore fetch:

1. All project epics + their direct children — one paginated query.
2. `remote_links(epic_key)` — lazy, cached per run, only if needed.

This is bounded (proportional to project size, one round-trip) and complete
(every candidate, including `Done` / `Cancelled` children, is inspected for
matching — no pre-filtering by status).

### Agent-touch marker

Every agent-written description ends with a hidden marker:

```
<!-- managed-by:jira-task-agent v1 -->
```

It indicates "the agent last touched this body" — useful for humans
browsing Jira and for future tooling that wants to filter to
agent-managed issues. The doc is the source of truth: when a doc edit
maps to an existing Jira issue, the agent updates the issue regardless
of who previously authored its description, and posts a changelog
comment naming the source doc + last editor so the human reviewer
sees what changed and why.

## 7. Update flow + Jira comment with @mention

Every successful **update** (epic OR task) posts a comment on the issue:

```
[~currentAssigneeUsername]

This issue was updated by the doc-sync agent.

Changed:
- Summary: "old text" → "new text"      (only if changed)
- Description updated. (See diff below.)

Source change:
- Doc: <doc name> (<drive link>)
- Edited by: <last_modifying_user_name> at <doc.modified_time>

Diff:
<unified diff or LLM-summarized "what changed and why">
```

**Tag rule:** `[~<username>]` of the **current Jira assignee** (per your
decision). For unassigned issues, fall back to the reporter; if reporter is
also empty, omit the mention and prepend a warning to the comment body.

Note: Jira Server `@mentions` use `name` (the login), not `displayName`. The
client must fetch and persist `assignee.name` along with the existing
`displayName` / `emailAddress`.

## 8. Architecture — components

| Component             | Responsibility                                                          |
|-----------------------|--------------------------------------------------------------------------|
| `drive_client`        | List folder + filter + download. Already built.                          |
| `jira_client`         | Read epic + children. Already built. Add: create issue, update issue, post comment, fetch assignee.name. |
| `classifier`          | LLM call: file → role (`task` / `root` / `skip`). Cheap model.           |
| `context_bundler`     | Concatenate `root` files into a length-bounded context blob.             |
| `extractor`           | LLM call (per `task` file): produces `{epic: {...}, tasks: [{...}]}` JSON conforming to the schema in §5. Receives the task file + the bundled root context. |
| `matcher`             | Two-stage LLM matcher (Stage 1: all-epics → project epics; Stage 2: grouped per-epic task matching, batched 4 per call, parallel 3 workers). One run per pipeline invocation. |
| `reconciler`          | Pure-logic Action emitter: takes matcher decisions + live Jira issues, applies marker/status guards, emits `create_*` / `update_*` / `noop` / `skip_*` / `covered_by_rollup` / `orphan`. No LLM calls. |
| `commenter`           | Composes the changelog comment + resolves `[~username]`.                |
| `state`               | Single tiny `state.json` holding `last_run_at` + `last_run_status`. Nothing else persists. |
| `runner`              | Orchestrates 1–6 of §2. Handles errors, retries, dry-run, report-only.   |
| `config`              | `.env` + `config.yaml` for prompt paths, model IDs, mention fallback policy. |

## 9. Persistence — minimal cursor only

The only thing the agent persists across runs is the "since when" cursor.
That's it. Everything else (file role, extracted content, which epic belongs
to which doc, whether anything changed) is derived fresh each run from
Drive + Jira.

`state.json` (lives next to `.env`):

```json
{
  "last_run_at": "2026-04-27T13:42:11+03:00",
  "last_run_status": "ok"
}
```

That is the entire on-disk schema. The cursor lets the next run filter Drive
via `modifiedTime > last_run_at` — the only optimization we keep, and it
matters because Drive listing scales with folder size, not with file size.
Losing `state.json` costs at most one full "process everything" run; there
is nothing to recover.

Runtime-only objects (`DriveFile`, `ExtractedEpic`, `ExtractedTask`) live in
memory for the duration of one run. They're not persisted — they're shaped
by the dataclasses in the code, not by the plan.

## 10. LLM contracts (overview)

**Provider: NVIDIA Inference.** All LLM calls go through NVIDIA's NIM /
build.nvidia.com inference endpoints (or an internal NVIDIA inference URL —
configurable). The endpoints are **OpenAI-compatible**, so the client can be
the standard `openai` Python SDK pointed at `NVIDIA_BASE_URL`. This avoids a
proprietary SDK and keeps prompts/JSON-mode portable.

### Configuration (in `.env`)

```
NVIDIA_API_KEY=...                                    # required
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1   # default; override for internal endpoints
LLM_MODEL_CLASSIFY=meta/llama-3.3-70b-instruct        # small/fast for classification
LLM_MODEL_EXTRACT=nvidia/llama-3.1-nemotron-70b-instruct   # high-quality for extraction
LLM_MODEL_SUMMARIZE=meta/llama-3.3-70b-instruct       # changelog comments
```

(Model IDs are placeholders — choose what's available on the NVIDIA endpoint
this project is licensed against. The pattern is "small + fast for classify
& summarize, larger + better-instruction-following for extract.")

### Client wrapper

A thin `llm_client.py` exposes `chat_json(model, system, user, schema)` and
hides the OpenAI-compatible plumbing. Every call uses **JSON mode** (or
prompt-enforced JSON for models without native JSON mode) and validates
against a Pydantic schema; on validation failure, retry up to 2x with a
"your previous output was invalid JSON, return only valid JSON conforming
to the schema" follow-up.

### Classifier
- **Inputs:** file name, first ~3 KB of file content, the names of all other
  files in the folder (relative context).
- **Output (JSON):** `{role: "task"|"root"|"skip", confidence: 0–1, reason: string}`.
- **Model:** `LLM_MODEL_CLASSIFY` (small/fast).

### Extractor
- **Inputs:** full task-file content + bundled root context (length-budgeted
  to fit the model's context window).
- **Output (JSON):**
  ```json
  {
    "epic": {"summary": "...", "description": "..."},
    "tasks": [
      {"summary": "...", "description": "...", "source_anchor": "..."},
      ...
    ]
  }
  ```
- **Constraints (validated post-call):** every task description contains a
  `### Definition of Done` heading; every `summary` is between 8 and 120 chars.
- **Model:** `LLM_MODEL_EXTRACT` (higher-quality).

### Changelog summarizer (used inside `commenter`)
- **Inputs:** before/after summary + description (epic or task).
- **Output:** 2-5 bullet points, plain text, suitable for a Jira comment.
- **Model:** `LLM_MODEL_SUMMARIZE` (small/fast).

## 11. Implementation phases — current progress

Legend: ✅ done · 🟡 partial · ⏳ not started

### Phase 0 — scaffolding ✅
- Repo + venv + `.env`. ✅
- `drive/client`: list + filter + download. ✅
- `jira/client`: list epics, get issue, list epic children, write primitives (create / update / comment / remote-link / transition). ✅
- Markdown → Jira-wiki conversion at the wrapper boundary. ✅

### Phase 1 — classifier ✅
- LLM client (`llm/client.py`) over NVIDIA Inference (OpenAI-compatible). ✅
- `pipeline/classifier.py` — three-role classifier (`single_epic / multi_epic / root`). ✅
- `scripts/classify_files.py`. ✅
- Confirmed correct labels on all 20 files in the live folder, ≥0.95 confidence. ✅

### Phase 2 — extractor ✅
- `pipeline/context_bundler.py` — concatenates root files into a length-budgeted blob. ✅
- `pipeline/extractor.py` — single-epic + multi-epic extractors. ✅
- DoD post-validator + composite-owner `Co-owners:` injection. ✅
- `scripts/extract_one.py` for ad-hoc extraction. ✅

### Phase 3 — Jira write primitives ✅
- `JiraClient.create_issue` (auto Epic Name, auto `ai-generated` label, auto Epic Link). ✅
- `update_issue`, `post_comment`, `add_remote_link`, `transition_issue`. ✅
- **Smoke-tested live** on CENTPM-1253 (epic) and CENTPM-1255 (task), with `[~assignee]` mention. ✅
- Delete: 403 by design — orphans are flagged, never deleted. ✅

### Phase 4 — commenter + change-tracking ✅
- `pipeline/commenter.py` — `format_update_comment` with `[~name]` mention + LLM-summarised changelog bullets. ✅
- HTML-comment marker (`<!-- managed-by:jira-task-agent v1 -->`) on every agent-written description as a "last-touched-by-agent" indicator. ✅
- Unassigned / no-reporter fallback. ✅

### Phase 5 — comparator (matcher + reconciler) ✅
- `pipeline/matcher.py` — **two-stage LLM matcher**:
  - Stage 1: one batched LLM call pairing ALL extracted epics across all files vs ALL project epics. ✅
  - Stage 2: grouped per-epic task matching, batched 4 epics per LLM call, parallel up to 3 workers. ✅
  - `run_matcher(extractions, project_tree)` orchestrates both stages and returns one `FileEpicResult` per extracted epic. ✅
- `jira/project_tree.fetch_project_tree()` — one paginated `/search` call returns all epics + their direct children. ✅
- `pipeline/reconciler.py` — pure-logic `build_plans_from_match(matcher_result, extractions, ...)`: marker check + content compare + Action emission. **No LLM calls in the reconciler.** ✅

### Phase 6 — orchestrator + cursor + capture ✅
- `pipeline/dedupe.py` — content-hash-based de-twinning of Google-Doc / markdown duplicates. ✅
- `runner.run_once()` end-to-end pipeline: list → download → dedupe → classify → bundle → extract (cursor-gated) → fetch project tree → run matcher (one batched call) → build plans → apply / capture / report. ✅
- `state.json` cursor (last_run_at + last_run_status). ✅
- CLI: `python -m jira_task_agent run [--apply] [--since] [--only NAME] [--target-epic KEY] [--capture PATH]`. ✅
- Per-run `run_plan.json` report. ✅

### Phase 7 — testing 🟡
- 50 unit + logical tests (helpers, dedupe, md→wiki, matcher unit, grouped matcher unit, reconciler scenarios). ✅
- 8 live integration tests (classifier, extractor, matcher with hardcoded data + with real local artifacts). ✅
- Run with `pytest` (offline) or `pytest -m live` (~30s, ~$0.01). ✅
- ⏳ One end-to-end **live capture run** on a small file (e.g., V11_Dashboard_Tasks.md) — full pipeline, real Drive + Jira reads, real LLMs, `--capture`, **zero Jira writes**. Verifies the captured payloads are what `--apply` would send.

### Phase 8 — efficiency caching tiers 🟡
**Goal:** warm runs (no doc changes) cost ~$0.

1. ✅ **Tier 1 — classification cache.** `cache.json` next to `state.json`. Skip the LLM classify call when `(file_id, modified_time)` matches a prior run.
2. ✅ **Tier 2 — extraction cache.** Skip the (large) LLM extract call when `(file_id, content_sha)` matches a prior run. The cached extraction is round-tripped through JSON; `mtime` change drops stale payloads.
3. ✅ `--no-cache` CLI flag for forced re-run; cache version mismatch / corrupt JSON → cold start.
4. ⏳ **Tier 3 — matcher skip path.** When `epic_key` is already cached for a file, skip Stage 1 of the matcher and go straight to live content compare. Remote-link match remains the fallback.
5. ⏳ **Per-section remote links** for multi_epic adoption (URL fragment `webViewLink#section-A`), so re-runs find the right Jira epic per section without going through the matcher.

### Phase 9 — production rollout ⏳
1. Run `--report-only` mode against the real folder for ≥2 weeks. Operator reviews `run_plan.json` daily.
2. Tune classifier / extractor / matcher prompts on real misses.
3. Flip to `--apply`. Keep a `--pause` flag for emergencies.

### Phase 10 — scheduling + ops ⏳
1. `python -m jira_task_agent watch --interval 4h` (or `cron` / `launchd` snippet in `docs/deploy.md`).
2. Backoff/retry on Drive 5xx/429 and Jira 5xx/429.
3. (Optional) Post the run report to Slack via webhook.

## 12. Risks & residual open questions

| # | Risk / Question                                                                          | Default / Mitigation                                                                  |
|---|------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------|
| 1 | Classifier mis-labels a context file as a `task` → spurious epic created.                | Confidence threshold; below it, route to `report-only` queue for human review.         |
| 2 | Five files share the name `May1_Initial_Version_Tasks.md` — multiple may be `task` role. | If multiple `task` classifications collide on cleaned-name, prefer the most recently   |
|   |                                                                                          | edited; flag the others as duplicates in the report.                                  |
| 3 | Jira `@mention` username (`name`) differs from email/displayName.                        | Resolve once via `/issue/{key}` and cache per assignee.                                |
| 4 | Updating descriptions could overwrite hand-edits made directly in Jira.                  | The doc is the source of truth: doc edits trigger updates and post a changelog comment |
|   |                                                                                          | naming the source doc + last editor. The previous body remains in Jira's edit history. |
| 5 | Token cost of LLM calls (re-extracting unchanged docs every run).                        | The `last_run_at` cursor filters Drive to files with `modifiedTime > last_run_at`, so  |
|   |                                                                                          | the extractor only sees files that actually moved since last success.                  |
| 6 | What to do with `task` files that no longer exist in Drive (deleted / moved).            | **OPEN.** Default proposal: leave existing Jira issues alone, surface in the report.   |
| 7 | Issue type for child tasks — always `Task`, or sometimes `Story`?                        | **OPEN.** Default to `Task`; allow extractor to suggest `Story` when it identifies one.|

## 13. Definition of Done — for the agent

1. Given a folder where the LLM correctly classifies the `V*_*.md` files as
   `task`, NVIS / latest-May1 as `root`, kickoff html as `skip`, the agent
   creates one epic per `task` file under `JIRA_PROJECT_KEY`, each with a
   description that contains highlights + relevant root context, and child
   tasks each containing a populated **Definition of Done** section.
2. Re-running the agent against the same files creates **zero** new
   issues and posts **zero** comments.
3. Editing one task line in a doc and re-running:
   - Updates exactly that one Jira task.
   - Posts exactly one comment, tagging the current assignee with `[~name]`,
     showing the before/after summary + a description-change summary.
4. Manually editing a Jira description (outside the agent) and re-running
   results in the doc-side update being applied, with a changelog comment
   on the issue naming the source doc + last editor. Prior content is
   preserved in Jira's edit history.
5. The agent runs unattended for 7 days on its schedule with no manual
   intervention; report stays green.
6. A manual one-shot (`python -m jira_task_agent run`) executed on the same
   environment produces an identical Plan to the scheduled run that would
   fire at the same moment — i.e. the two entry points are interchangeable
   and configuration-driven, not flag-driven.
