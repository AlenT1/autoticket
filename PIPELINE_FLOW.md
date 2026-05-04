# Pipeline Flow

What `run_once` does, end to end. Every cache, every LLM call, every Jira
read/write — and what happens for each kind of doc change.

---

## 1. End-to-end stages

```
Drive folder ──► list ──► download ──► dedupe ──► classify
                                                    │
                                                    ▼
                                              bundle_root_context
                                                    │
                                                    ▼
                  per file: extract_or_reuse  ──►  (extraction, dirty_anchors)
                                                    │
                                                    ▼
                          fetch_project_tree (one paginated /search)
                                                    │
                                                    ▼
                          match_with_cache  ──►  MatcherResult
                                                    │
                                                    ▼
                          filter_dirty  ──►  list[DirtySection]
                                                    │
                                                    ▼
                          build_plans_from_dirty  ──►  list[ReconcilePlan]
                                                    │
                                            ┌───────┴────────┐
                                          dry-run         --apply
                                       (capture only)   (writes Jira)
                                                    │
                                                    ▼
                                        save cache + state
```

Each per-file stage looks at one cache key to decide whether to skip,
reuse, or recompute. Caches are layered so a "warm" run does the
absolute minimum work: only items the doc changed reach the LLM, and
only those items reach Jira.

---

## 2. The cache (`data/cache.json`)

One `FileCacheEntry` per Drive file id. Fields:

| field | what it is | populated by |
|---|---|---|
| `modified_time` | Drive's `modifiedTime` ISO string | classify step |
| `content_sha` | sha256 of the local file bytes | classify step |
| `role` | `single_epic` / `multi_epic` / `root` / `skip` | classify step |
| `extraction_payload` | serialized `Extraction` (epic + tasks) | extract step |
| `matcher_payload` | `{content_sha, prompt_sha, topology_sha, results: [FileEpicResult]}` | match step |
| `diff_payload` | `{file_text: "..."}` — the file content from last extract | extract step |

Three reuse tiers:

- **Classify hit** — `(file_id, modified_time)` unchanged → reuse `role`.
- **Extract hit** — `content_sha` matches `extraction_payload`'s key →
  reuse `Extraction` verbatim, no LLM. `dirty_anchors=∅`.
- **Match hit** — `content_sha + prompt_sha` match in `matcher_payload`
  AND every cached `matched_jira_key` still exists in Jira → reuse
  `FileEpicResult`s verbatim, no LLM.

Each tier's invalidation is independent. Bumping a matcher prompt
(`matcher.txt` / `matcher_grouped.txt`) or `LLM_MODEL_CLASSIFY` changes
`prompt_sha` and invalidates every cached match across the project. A
structural change in CENTPM (epic add/remove/rename) changes
`topology_sha`; that's stored for diagnostics but is not a hard
invalidation — the runner soft-falls-back when a cached
`matched_jira_key` is missing from the live tree.

---

## 3. Per-file flow inside `run_once`

For one file, the runner visits these stages in order:

### 3.1. Classify (`pipeline/classifier.py`)

Cache-keyed by `(file_id, modified_time)`.

- **Hit**: reuse cached `role`. No LLM.
- **Miss**: one LLM call → `single_epic` / `multi_epic` / `root` / `skip`.
  Cache stores the verdict.

`root` files are bundled into `root_context` (background for extract).
`skip` files (Drive duplicates) are dropped entirely.

### 3.2. Extract (`pipeline/file_extract.py::extract_or_reuse`)

Returns `(extraction, dirty_anchors)`. Three branches:

#### (a) Tier 2 hit — content_sha matches cached

- 0 LLM calls.
- Returns the cached extraction verbatim.
- `dirty_anchors = ∅` → reconciler will noop this file completely.

#### (b) Cached + content changed — diff path

Run only when `extraction_payload` AND `diff_payload.file_text` are both
set in the cache:

1. `compute_unified_diff(cached_text, current_text)` — pure Python,
   `difflib.unified_diff` with 3 lines of context.
2. **LLM #1: `extract_diff`** (prompt `extractor_diff.txt`) — labels-only
   verdict on each diff region:
   ```json
   {
     "modified_anchors": [...],
     "removed_anchors":  [...],
     "added": [{"summary": "...", "section": "..."}],
     "new_subepics": [{"summary": "..."}],
     "epic_changed": true | false
   }
   ```
3. `labels_to_targets(labels, cached)` — picks the items needing fresh
   bodies (modified anchors → cached summary; added → LLM-given summary;
   epic targets cover single-epic body change + new sub-epics).
4. **LLM #2: `extract_targeted`** (prompt `extractor_targeted.txt`) —
   produces full Jira-quality bodies (with DoD, Acceptance criteria,
   Source) for **only** the items in `targets`. Output is bounded by
   change count, not file size.
5. `apply_changes(cached, labels, bodies)` — pure Python merge.
   Cached items kept verbatim except for the changed slices: removed
   dropped, modified replaced, added appended (multi-epic: routed by
   `section`), `epic_changed` swaps the cached epic.
6. `compute_dirty(cached, merged)` — pure Python diff over the merged
   extraction. Returns the set of changed identifiers:
   - tasks: their `source_anchor` strings.
   - epics: `"<epic>:N"` where N is the section index.

Persists the new merged extraction + new `file_text` back to cache.

#### (c) No usable cache — cold path

- **LLM call: `extract_from_file` / `extract_multi_from_file`** — full
  extraction over the file, same prompt as the original cold pipeline.
- Persists extraction + `file_text`.
- `dirty_anchors = None` → reconciler treats every item as "process".

### 3.3. Match (`pipeline/file_match.py::match_with_cache`)

Three branches per file:

#### (a) Cache hit
`content_sha + prompt_sha` matched in `matcher_payload` and every cached
`matched_jira_key` still exists in `project_tree`. Returns cached
`FileEpicResult`s verbatim. 0 LLM calls.

#### (b) Partial path
Cache exists; caller passed `dirty_anchors`. For each section:

- **Processed** = section has any dirty content (epic token OR a dirty
  task) OR is brand new (beyond cached_frs length).
- For every processed section's epic: re-run **Stage 1** in one batched
  LLM call against the full project tree. The matcher sees the section's
  epic body + every CENTPM epic with summaries/descriptions/children.
  Self-heals stale `matched_jira_key=None` from prior cold misses.
- For each processed section, run **Stage 2** (`match_grouped`):
  - If Stage 1 confirmed the cached match (key unchanged): only dirty
    tasks go through Stage 2; clean tasks keep their cached
    `MatchDecision` byte-for-byte.
  - If Stage 1 changed the key (or there was no cached key): all tasks
    under the new epic re-evaluate against the new candidate set.
- Untouched sections: kept verbatim from cache, no LLM.

LLM cost: 1 Stage 1 call (over all processed sections) + 1 Stage 2
call per processed section. Persists the merged `FileEpicResult`s.

#### (c) Fresh full match
No usable cache (or `use_cache=False`). Calls `run_matcher` — Stage 1
over all extracted epics; Stage 2 grouped-batched over each matched
epic's tasks. When Stage 1 returns no Jira match for an epic, that
section's tasks get default no-match `MatchDecision`s (so the reconciler
emits `create_task` under the just-created epic).

### 3.4. Filter (`pipeline/dirty_filter.py::filter_dirty`)

Walks `MatcherResult.file_results` × `extractions` × `dirty_anchors_per_file`,
producing a flat `list[DirtySection]`. A section is **kept** when:

- `dirty_anchors=None` (cold path — keep every section, every task).
- The section's `<epic>:N` token is in dirty.
- Any of the section's task anchors is in dirty.
- It is brand new (idx >= len(cached_frs)).

Within a kept section, only dirty task anchors survive (cold path keeps
all tasks). Sections with no dirty content disappear from the output —
the reconciler never iterates them.

Each `DirtySection` carries:
- the section's matched_jira_key + epic match metadata,
- `epic_dirty: bool` (was `<epic>:N` in dirty),
- `tasks: list[DirtyTask]` (extracted task + its `MatchDecision`),
- `orphan_keys: list[str]`.

### 3.5. Reconcile (`pipeline/reconciler.py::build_plans_from_dirty`)

Pure logic. Per `DirtySection`, emits one `EpicGroup`:

| Section state | Epic action | Per-task action |
|---|---|---|
| `matched_jira_key=None` | `create_epic` | `create_task` for each (LLM produced None decisions for them) |
| matched + status ∈ completed | `skip_completed_epic` | suppressed (no task actions) |
| matched + status active + `epic_dirty=True` | `update_epic` (full new summary + description) | per-task per the matcher decision |
| matched + status active + `epic_dirty=False` | `noop` (informational; carries `target_key` so dirty tasks attach under it) | per-task per the matcher decision |

Per dirty task (under matched + active + non-skipped epic):

| `decision.candidate_key` | Action |
|---|---|
| `None` | `create_task` |
| Some key, unique to this task | `update_task` |
| Some key, **shared with sibling task(s)** | `covered_by_rollup` (no Jira write; rollup pattern means one Jira issue covers multiple doc tasks) |

`orphan_keys` from the matcher (Jira children that don't pair with any
extracted task) become `kind="orphan"` actions — informational only,
no Jira write.

Reconciler's only Jira touch: one `get_issue(matched_jira_key)` per
dirty epic to check status.

### 3.6. Apply (`runner.py::_apply_plan`)

Walks each `EpicGroup`. For `--apply` runs:

- `create_epic` → `POST /issue` (issuetype=Epic, with `ai-generated` label) → returns key → `POST /issue/<key>/remotelink` to the Drive doc URL.
- `update_epic` → `PUT /issue/<key>` (summary + description) → `POST /issue/<key>/comment` (changelog using live before-state).
- `noop` (epic) → no Jira write; just propagates `target_key` to child tasks.
- `skip_completed_epic` → no Jira writes anywhere in the group.
- `create_task` → `POST /issue` (issuetype=Task, `epic_link` set) → `POST /issue/<key>/remotelink`.
- `update_task` → `PUT /issue/<key>` (summary + description) → `POST /issue/<key>/comment` (changelog).
- `covered_by_rollup` / `orphan` → no Jira write; surface in report.

For `--capture` runs, `jira.post` and `jira.put` are replaced with
in-memory recorders that append to a JSON file. Jira reads (project
tree, status guard, comment-side `get_issue`) still hit live Jira.

---

## 4. What happens for each kind of doc change

For every scenario below, **everything else** in the file is byte-equal
to cached. The runner does the work for those changes only; the rest of
the file stays cached at every layer.

### 4.1. New file (never seen before)

- Classify: 1 LLM call → `single_epic` / `multi_epic`. Cache stores role.
- Extract: cold path → 1 LLM call → full extraction. Cache stores it +
  `file_text`. `dirty_anchors=None`.
- Match: fresh full match → Stage 1 (1 LLM call, this section's epic vs
  full project tree) + Stage 2 (1 LLM call per matched epic). Cache
  stores results.
- Filter: cold-path keep-everything → all sections + all tasks.
- Reconcile: every section/task → `create_epic` + `create_task`s, or
  `update_epic`/`update_task` if Stage 1/2 paired them.
- Apply: writes everything Jira-side.

### 4.2. Same file, no edits

- Classify hit, **extract hit (Tier 2)** — 0 LLM calls, `dirty=∅`.
- Match: Tier 3 cache hit if prompt + project topology unchanged.
- Filter: empty input (file dropped because `dirty=∅`).
- Reconcile: emits zero `EpicGroup`s for this file.
- Apply: nothing.

### 4.3. Updated file: one task body edited

`MON-NEW: change 'enable' → 'enforce'` inside an existing task description.

- Classify hit; extract diff path runs (file_text differs).
- `extract_diff` returns `{"modified_anchors": ["MON-NEW anchor"], ...}`.
- `extract_targeted` produces a fresh body for that one task.
- `compute_dirty` returns `{"MON-NEW anchor"}`.
- Match partial: section is processed (one dirty task) → Stage 1 on its
  epic (re-confirms or self-heals the cached pairing) → Stage 2 on the
  one dirty task only.
- Filter: keeps only that section, with one DirtyTask.
- Reconcile: `noop` epic action + `update_task` for the dirty task.
- Apply: 1 `PUT /issue/<key>` + 1 `POST .../comment`.

### 4.4. Updated file: one task added

`+ - DR-1 New backup task` under an existing section.

- Diff: `extract_diff` returns `{"added": [{"summary": "DR-1 ...", "section": "<existing section>"}]}`.
- `extract_targeted` produces the full body for the new task.
- `apply_changes` appends it to the cached section's task list.
- `dirty = {"DR-1 anchor"}` (new anchor not in cached → flagged).
- Match partial: section processed → Stage 1 confirms epic →  Stage 2
  for the one new task.
- Reconcile: `noop` epic action + `create_task` (Stage 2's
  `candidate_key=None` → no existing Jira issue) + remote_link.
- Apply: 1 `POST /issue` (issuetype=Task) + 1 `POST .../remotelink`.

### 4.5. Updated file: one task removed

`- - OLD-X retired step` deleted from the doc.

- `extract_diff` returns `{"removed_anchors": ["OLD-X anchor"]}`.
- `apply_changes` drops the cached entry. No targeted body needed.
- `compute_dirty` returns `∅` (the merged extraction has one fewer
  anchor; nothing in merged is "changed", just absent).
- Match partial: section may still be processed if other content is
  dirty; otherwise clean → no work.
- Reconcile: nothing for the removed task. The Jira issue stays alive
  (the agent does not delete; it surfaces as `orphan` on the next pass
  if its parent epic still gets processed).
- Apply: zero writes for this change. Removal is intentionally
  conservative: human review required.

### 4.6. Updated file: epic title or description changed

`## Old title` → `## New title (Q3 deadline)` at the H2 of a multi-epic
sub-section, or the file's H1 in single-epic.

- `extract_diff` returns `{"epic_changed": true}` (single) or, for
  multi-epic, the section's diff registers as a renamed sub-epic body.
- `extract_targeted` produces the new epic body.
- `apply_changes` replaces the cached epic.
- `compute_dirty` returns `{"<epic>:N"}` (and any tasks whose body
  shifted).
- Match partial: the section is processed (`<epic>:N` in dirty) → Stage
  1 may re-pair the epic if its title shifted. Stage 2 runs in `_full`
  mode if the matched key changes (children set changed); else only
  dirty tasks via `_partial`.
- Reconcile: `update_epic` (new summary + new description). No
  no-rename rule — doc is the source of truth.
- Apply: 1 `PUT /issue/<key>` + 1 `POST .../comment`.

### 4.7. Updated file: brand-new sub-epic added (multi-epic only)

`+ ## J. New section\n+ - DR-1 ...` — a whole new H2 with tasks.

- `extract_diff` returns `{"new_subepics": [{"summary": "J. New section"}], "added": [{"summary": "DR-1 ...", "section": "J. New section"}, ...]}`.
- `extract_targeted` produces the new sub-epic body + each new task body.
- `apply_changes` appends the new sub-epic to the merged extraction;
  added tasks route to it via their `section`.
- `compute_dirty` returns `{"<epic>:N", "DR-1 anchor", ...}` for the new
  section index N.
- Match partial: section idx N >= len(cached_frs) → processed → Stage 1
  on its epic (full tree) + Stage 2 on its tasks.
- Reconcile: `create_epic` for the new section (Stage 1 returned None)
  + `create_task` per task; remote_links.
- Apply: 1 `POST /issue` (Epic) + 1 remote_link + N × (`POST /issue` Task
  + remote_link).

### 4.8. Combinations

The above compose. May1 with all three mutations (UI-1 task body edit,
MON-NEW added in section C, section J added with DR-1/2/3) produces:

- `dirty = {<epic>:9, UI-1 anchor, MON-NEW anchor, DR-1 anchor, DR-2 anchor, DR-3 anchor}` — 6 ids, 5 task changes + 1 new sub-epic.
- 2 LLM calls in extract (diff labels + targeted bodies for 1 modified epic + 1 modified task + 4 added tasks).
- 1 Stage 1 LLM call (3 processed sections — C, I, J) + 3 Stage 2 calls
  (one per processed section, each scoped to its dirty tasks).
- Reconciler: 1 `update_task` (UI-1) + 4 `create_task` (MON-NEW + DR-1/2/3)
  + 1 `create_epic` (J) + 2 `noop` epics (C and I — body unchanged).
- Apply (capture or live): 12 writes — 1 PUT + 1 comment + 5 creates +
  5 remote_links.

That's the property the live test `tests/test_may1_full_pipeline_live.py`
asserts strictly.

---

## 5. What's stub-friendly

Every external dependency has a clear seam. For unit testing without
network:

| Layer | How to stub |
|---|---|
| Drive (`list_folder`, `download_file`) | monkey-patch `runner.list_folder` / `runner.download_file`; capture-mode warm tests do this with `_no_redownload` to read a local file in place |
| Drive auth | the runner pulls via `build_service`; tests don't need auth when they patch the two functions above |
| LLM (`chat()`) | every prompt module imports `chat` from `..llm.client`. Monkey-patch `chat` to return `(parsed_dict, meta_dict)`. Examples: `tests/test_extractor_*.py`, `tests/test_file_match_unit.py` (stubs `match`, `match_grouped`, `run_matcher` directly). |
| Jira reads (`fetch_project_tree`, `get_issue`) | `tests/conftest.py::MockJiraClient` returns canned data and records writes to `recorded`. Inject via `JiraClient.from_env()` patch. |
| Jira writes (`apply` path) | use `--capture` (CLI) or `capture_path=...` (programmatic). Replaces `jira.post` and `jira.put` with recorders before any apply runs. No network writes. |
| Cache | `Cache()` is fully in-process; `Cache.load(path)` / `Cache.save(path)` for I/O. Tests construct `Cache()` and pre-seed via `set_classification` / `set_extraction` / `set_match` / `set_file_text`. |
| State (cursor) | `State` dataclass + `state.json` loader/saver — same pattern. |

The pipeline never imports a third-party Jira/Drive lib at the call
sites; everything goes through `JiraClient` and a small set of Drive
helpers. That's the seam.

---

## 6. CLI knobs that affect this flow

```sh
.venv/bin/python -m jira_task_agent run [--apply] [--no-cache] \
                                         [--since DATE] \
                                         [--only NAME.md] \
                                         [--target-epic CENTPM-XXXX] \
                                         [--capture data/would_send.json]
```

- `--apply` — actually write to Jira. Default is dry-run (plan only).
- `--no-cache` — force re-classify and re-extract everything (use when
  prompts change or for debugging).
- `--since DATE` — override `state.last_run_at` cursor; files modified
  before DATE are skipped at the extract step.
- `--only NAME.md` — process only one Drive file by name (still
  classifies all of them so root context is built).
- `--target-epic CENTPM-XXXX` — route every `create_task` action under
  this fixed epic. Used during dev so test issues land under
  `CENTPM-1253` regardless of the matcher's verdict.
- `--capture PATH` — implies `--apply`; intercepts every PUT/POST and
  writes the JSON payload to `PATH` instead of sending. Read paths still
  hit Jira.
