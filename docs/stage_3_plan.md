# Stage 3 — make the abstractions abstractable

Tracking doc for the stage-3 cleanup. Tick `- [ ]` boxes as commits land so
anyone picking up the work knows where we stopped.

**Branch:** `scope/final-tool-abstract`
**Stage 2 base:** `scope/sharon-merged` (already merged into this branch's
ancestry).
**Stage 3 done condition:** the two greps at the bottom of this file return
zero matches.

---

## ▶ Current position — stopped at end of commit 4

Phase 1 (cherry-picks from `main`) landed at `ba0b72d` and was published
to `autoticket/scope/final-tool-abstract`. Stage 3 c1+c2+c3 already
landed before that as a single bundled commit (they share
`src/_shared/io/sinks/jira/client.py`). Stage 3 c4 just landed:
`pipeline/reconciler.py` now takes `sink: JiraSink` + `resolver:
AssigneeResolver` instead of `client: JiraClient`. Net result:

- The shared `JiraClient` lives at
  [src/_shared/io/sinks/jira/client.py](../src/_shared/io/sinks/jira/client.py).
- The `_shared → body` import leak is gone (verify with the greps at the
  bottom of this doc — they return only the drive-body shims, deleted in
  c6).
- `JiraSink` exposes everything the drive runner will need in c5:
  `get_issue_normalized`, `fetch_project_tree`, plus the new
  `CapturingJiraSink` subclass.
- The reconciler is now sink/resolver-driven; the runner constructs a
  `StaticMapStrategy` + `JiraSink` and threads them through. Apply path
  is still on the raw client — that's c5's job.
- 398 offline tests pass post-c4 (was 393 post-Phase-1, +5 c4-specific
  tests), modulo the pre-existing `test_payload_includes_summary_priority_labels`
  failure that's unrelated to stage 3.

**Next up:** [commit 5](#commit-5--drive-runner-uses-jirasink-for-writes-capture-via-capturingjirasink)
— route the drive runner's apply path through `JiraSink.create/update/comment`
and replace capture-mode monkey-patching with `CapturingJiraSink`. **Riskiest
commit** — preserve `finalize_body` calls and the cache-save gate
(`apply and not capture_path and not report.errors`) per
[stage_3_phase1_integration.md § c4-c8 deltas](./stage_3_phase1_integration.md).

For everything a fresh dev needs to pick this up cold (env setup, gotchas,
locked-in sub-decisions, exact starting line numbers for c4) see the
**[Picking this up](#picking-this-up--handoff-context)** appendix at the
bottom of this doc.

---

## Context

Stage 2 fused f2j and jira_task_agent under one repo with a sandwich
architecture: shared input sources (`src/_shared/io/sources/`), shared LLM
provider (`src/_shared/llm/`), shared output sink (`src/_shared/io/sinks/`),
and two distinct bodies in the middle.

Three seams still leak across that architecture and prevent the shared layer
from being honestly autodev-consumable:

1. [src/_shared/io/sinks/jira/sink.py:21](../src/_shared/io/sinks/jira/sink.py#L21)
   imports `JiraClient` from `jira_task_agent.jira.client` — the only
   `_shared → body` import in the repo.
   [src/file_to_jira/upload.py](../src/file_to_jira/upload.py) reaches
   sideways into the same module (lines 38–42, 131).
2. The drive runner still drives `JiraClient` directly (6 write sites + 2
   read sites + capture-mode monkey-patching) instead of going through
   `JiraSink`.
3. f2j's legacy `src/file_to_jira/jira/{client, uploader, field_map,
   user_resolver}.py` (1,353 LOC) is dead from the runtime perspective except
   for two diagnostic CLI subcommands (`f2j jira whoami` / `f2j jira fields`)
   that depend on auth-mode features the shared client doesn't have yet.

Stage 3 closes all three seams. **Not** in scope: new sinks (Monday, Linear),
extracting cache utilities (deferred to stage 4). Multi-auth-mode (Server/DC
bearer + Cloud basic) is ported to the shared client to unblock item 3.

---

## Sub-decisions resolved

- **Auth-mode unification** — one constructor with `auth_mode` param.
  `JiraClient.from_env()` keeps drive's behavior; new
  `JiraClient.from_config(*, url, pat, auth_mode, user_email=None,
  ca_bundle=None)` matches f2j's CLI call shape.
  `_build_auth_header(token, auth_mode, user_email)` becomes 3-arg.
- **Backend choice for the unified client** — keep drive's raw-`requests`
  core; port the f2j-only surface (`whoami`, `create_meta`,
  `download_attachment`, retry) on top via direct REST calls. Avoids
  dragging `atlassian-python-api` into `_shared`.
- **`get_issue` shape** — sink's `get_issue` returns the raw Jira REST dict;
  reconciler consumes the *normalized* shape. Add
  `JiraSink.get_issue_normalized(key)` so the protocol's raw contract stays
  clean.
- **Capture mode** — replace the `runner.py` monkey-patch with `class
  CapturingJiraSink(JiraSink)` that overrides `create/update/comment` only.
  Reads pass through.

---

## Implementation sequence

### Commit 1 — Move `JiraClient` into `_shared`; unify auth modes ✅

- [x] New: [src/_shared/io/sinks/jira/client.py](../src/_shared/io/sinks/jira/client.py)
  — port of drive's client + 3-arg `_build_auth_header` + `from_config()`.
- [x] New: [src/_shared/io/sinks/jira/project_tree.py](../src/_shared/io/sinks/jira/project_tree.py)
  — moved from drive.
- [x] Modify: [src/_shared/io/sinks/jira/sink.py:21](../src/_shared/io/sinks/jira/sink.py#L21)
  → `from .client import JiraClient`.
- [x] Modify: `src/jira_task_agent/jira/client.py` → re-export shim.
- [x] Modify: `src/jira_task_agent/jira/project_tree.py` → re-export shim.
- [x] Modify: [src/file_to_jira/upload.py](../src/file_to_jira/upload.py)
  imports.
- [x] Gate (offline): 394 passed, 24 deselected. Live `test_runner_cache_live`
  skipped at this commit boundary because the move is a no-op import-path
  change with no behavior delta — the topology hash *cannot* shift here.
  Live re-smoke deferred to commit 5 where it actually has work to do.

### Commit 2 — Port f2j-only surface onto the shared client ✅

- [x] Add to [src/_shared/io/sinks/jira/client.py](../src/_shared/io/sinks/jira/client.py):
  `WhoamiResult`, `whoami()`, `create_meta()`, `download_attachment()`,
  `JiraError`, `issue_browse_url()`, retry decorator.
- [x] Update [src/_shared/io/sinks/jira/__init__.py](../src/_shared/io/sinks/jira/__init__.py)
  exports.
- [x] Gate: 21 sink tests passed. Full offline 394 passed (no regression).

### Commit 3 — `JiraSink` grows the methods the drive runner needs ✅

- [x] Add `get_issue_normalized(key)` to
  [src/_shared/io/sinks/jira/sink.py](../src/_shared/io/sinks/jira/sink.py).
- [x] Add `fetch_project_tree(self, project_key=None)` method.
- [x] New `class CapturingJiraSink(JiraSink)` — wraps the underlying client's
  `.post`/`.put` with recorders so writes get captured at the HTTP-payload
  level (preserves byte-equivalence with the existing `data/would_send.json`
  shape, doesn't break `test_warm_scenarios_live.py` assertions).
- [x] Gate: 8 new tests passed (3 capture write paths + capture key
  sequencing + capture reads pass-through + `get_issue_normalized` shape +
  2 `fetch_project_tree` delegate tests). Full offline 402 passed.

### Commit 4 — Reconciler takes an `AssigneeResolver`, not a client ✅

- [x] Modify [src/jira_task_agent/pipeline/reconciler.py](../src/jira_task_agent/pipeline/reconciler.py):
  `_resolve_assignee(resolver, raw)`; thread `resolver` + `sink` through
  `_build_epic_action`, `_build_task_actions`, `_build_task_action`,
  `build_plans_from_dirty`. Replace `get_issue(key, client=client)` with
  `sink.get_issue_normalized(key)`.
- [x] Modify [src/jira_task_agent/runner.py](../src/jira_task_agent/runner.py)
  call site at line 342 — construct `StaticMapStrategy` + `JiraSink`,
  pass both. Apply path unchanged (c5's territory).
- [x] Update existing 14 reconciler test call sites to use `**_open_status_kw()`
  (returns `sink` + `resolver` kwargs).
- [x] Add 5 new c4-specific tests covering the injection points: resolver
  consulted for epic + task assignees, resolver short-circuits on `None`,
  `sink.get_issue_normalized` is the only live-status reader, sink status
  drives `skip_completed_epic`.
- [x] Gate: `uv run pytest tests/jira_task_agent/test_reconciler_logical.py`
  → 23 passed (was 18 pre-c4). Full offline gate at 398 (was 393).

### Commit 5 — Drive runner uses `JiraSink` for writes; capture via `CapturingJiraSink`

**Riskiest commit. Live re-smoke required before proceeding to commit 6.**

- [ ] Modify [src/jira_task_agent/runner.py](../src/jira_task_agent/runner.py)
  line 301 — construct `client + sink` (Capturing if `--capture`).
- [ ] `runner.py:306` → `sink.fetch_project_tree(project_key)`.
- [ ] Delete `_enable_capture` and the captured-writes list (lines 302–304,
  429–448). Read `sink.captured_writes` for the capture dump.
- [ ] Modify `_apply_epic_action` / `_apply_task_action` (lines 485–545) to
  build a `Ticket` and call `sink.create/update/comment`.
- [ ] `runner.py:548` (`_comment_for`) → `sink.get_issue_normalized(...)`.
- [ ] **Topology-hash safety check**: tree dict from
  `sink.fetch_project_tree(...)` must equal the prior tree dict
  byte-for-byte (matcher hash is computed over `json.dumps(tree,
  sort_keys=True)`).
- [ ] Live gate (run all six):
  ```powershell
  uv run pytest -m live tests/jira_task_agent/test_runner_cache_live.py -x
  uv run pytest -m live tests/jira_task_agent/test_warm_scenarios_live.py -x
  uv run pytest -m live tests/jira_task_agent/test_local_e2e_md_live.py -x
  uv run pytest -m live tests/jira_task_agent/test_may1_full_pipeline_live.py -x
  uv run pytest -m live tests/jira_task_agent/test_may1_run_plan_md_live.py -x
  uv run pytest -m live tests/jira_task_agent/test_mixed_warm_and_new_live.py -x
  ```

### Commit 6 — Delete the drive shim

- [ ] Delete `src/jira_task_agent/jira/{client,project_tree,__init__}.py`.
- [ ] Re-point any remaining `from .jira.client import …` to
  `_shared.io.sinks.jira.client`.
- [ ] Gate: `uv run pytest tests/jira_task_agent -x` + one live smoke.

### Commit 7 — f2j CLI + delete legacy `file_to_jira/jira/`

- [ ] Modify [src/file_to_jira/cli.py](../src/file_to_jira/cli.py) lines 732,
  752, 844 — repoint at `_shared.io.sinks.jira.client`. Switch
  `_build_jira_client_for_cli` to `JiraClient.from_config(...)`.
- [ ] New: `src/_shared/io/sinks/jira/field_discovery.py` — port `FieldInfo`,
  `build_field_map`, `discover_create_meta`, `discover_fields_from_issue`.
- [ ] Migrate `build_issue_payload` from
  `src/file_to_jira/jira/uploader.py` to alongside f2j's `upload.py`.
- [ ] Delete `src/file_to_jira/jira/{client,field_map,uploader,user_resolver,__init__}.py`
  (1,353 LOC removed).
- [ ] Smoke gate:
  ```powershell
  uv run f2j jira whoami
  uv run f2j jira fields --from-issue CENTPM-1253
  uv run f2j jira fields --project CENTPM
  uv run f2j upload state.json --only <id> --dry-run
  ```

### Commit 8 — Migrate `tests/file_to_jira/test_jira.py` (21 tests)

| Old test | New home | Strategy gap filled? |
|---|---|---|
| `test_whoami_extracts_username` | `tests/_shared/io/sinks/test_jira_client.py` | — |
| `test_create_issue_routes_through_atlassian` | `test_jira_client.py` | — |
| `test_browse_url_format` | `test_jira_client.py` | — |
| `test_basic_auth_requires_user_email` | `test_jira_client.py` | — |
| `test_unknown_auth_mode_raises` | `test_jira_client.py` | — |
| `test_attachment_download_caps_at_max_bytes` | `test_jira_client.py` | — |
| `test_attachment_download_writes_to_dest` | `test_jira_client.py` | — |
| `test_discover_create_meta` | `test_jira_field_discovery.py` | — |
| `test_build_field_map_marks_unknown_fields` | `test_jira_field_discovery.py` | — |
| `test_user_resolver_loads_existing_yaml` | `test_jira_sink.py::test_picker_with_cache_loads_yaml` | ✅ `PickerWithCacheStrategy` |
| `test_user_resolver_searches_jira_and_caches` | `test_jira_sink.py::test_picker_with_cache_searches_and_persists` | ✅ |
| `test_user_resolver_unknown_default_policy` | `test_jira_sink.py::test_picker_with_cache_default_policy` | ✅ |
| `test_user_resolver_unknown_skip_policy` | `test_jira_sink.py::test_picker_with_cache_skip_policy` | ✅ |
| `test_user_resolver_unknown_fail_policy` | `test_jira_sink.py::test_picker_with_cache_fail_policy` | ✅ |
| `test_payload_includes_summary_priority_labels` | `tests/file_to_jira/test_upload.py` (sink-level) | — |
| `test_payload_truncates_oversize_description` | `tests/file_to_jira/test_upload.py` | — |
| `test_payload_includes_external_id_field_when_configured` | `tests/file_to_jira/test_upload.py` | — |
| `test_payload_assignee_routed_by_module` | `test_jira_sink.py::test_deterministic_chain_module` | ✅ `DeterministicChainStrategy` |
| `test_payload_assignee_falls_through_to_default` | `test_jira_sink.py::test_deterministic_chain_default` | ✅ |
| `test_payload_explicit_hint_wins_over_module` | `test_jira_sink.py::test_static_map_first_owner` | ✅ `StaticMapStrategy` |
| `test_payload_assignee_resolved_from_user_map` | covered by picker tests above | — |

- [ ] New: `tests/_shared/io/sinks/test_jira_client.py` (7 tests).
- [ ] New: `tests/_shared/io/sinks/test_jira_field_discovery.py` (2 tests).
- [ ] Extend `tests/_shared/io/sinks/test_jira_sink.py` with the strategy
  gap-fillers + `test_passthrough_returns_input` +
  `test_noop_returns_ticket_epic_key` (5 of 5 untested strategies covered).
- [ ] Move payload-shape tests to `tests/file_to_jira/test_upload.py`.
- [ ] Delete `tests/file_to_jira/test_jira.py`.
- [ ] Gate: full offline + live re-smoke.

---

## Risks and gotchas

1. **`atlassian-python-api` removal** —
   `src/file_to_jira/jira/field_map.py::discover_fields_from_issue` uses
   `client._client.get(...)` (the atlassian instance). Port must rewrite as
   `client.get(...)`.
2. **`whoami` Cloud vs Server shape** — Server returns `name`, Cloud returns
   `accountId`. `WhoamiResult` falls back `name → accountId → key`.
3. **`get_issue` dual semantics** — keep both `get_issue` (raw) and
   `get_issue_normalized` (drive's shape). Don't conflate.
4. **Topology hash byte-identity** — matcher hashes `json.dumps(tree,
   sort_keys=True)`. Don't reorder dict keys in `_normalize_min`. List order
   (epic ordering, child ordering) is **not** protected by `sort_keys` —
   don't change list sorts inside `fetch_project_tree`.
5. **Auth-mode call-site audit** — every direct `JiraClient(...)`
   construction breaks when the constructor changes. Known sites:
   `runner.py:301` (`from_env()` ✓), `cli.py:741, 856` (`from_config()` ✓),
   `upload.py:131`. Verify before commit 5.
6. **`team_mapping.json` location** — drive's
   `JiraClient.resolve_assignee_username` defaulted to
   `Path("team_mapping.json")` (CWD). `StaticMapStrategy` defaults to the
   same. No migration.
7. **`CapturingJiraSink` must NOT stub reads** — only override
   `create/update/comment`. `search/get_issue/get_issue_normalized/fetch_project_tree`
   pass through.
8. **f2j `upload.py:131` `_load_token` import** — points at drive client
   today. After commit 1 it points at `_shared.io.sinks.jira.client`.

---

## Reuse — existing code we lean on

- `StaticMapStrategy` ([src/_shared/io/sinks/jira/strategies/assignee.py:39](../src/_shared/io/sinks/jira/strategies/assignee.py#L39))
  — drop-in replacement for `JiraClient.resolve_assignee_username`.
- `NoOpStrategy` ([src/_shared/io/sinks/jira/strategies/epic_router.py:23](../src/_shared/io/sinks/jira/strategies/epic_router.py#L23))
  — drive's epic-routing pattern.
- `PickerWithCacheStrategy` ([src/_shared/io/sinks/jira/strategies/assignee.py:100](../src/_shared/io/sinks/jira/strategies/assignee.py#L100))
  — already replaces f2j's `UserResolver`.
- `_md_to_jira_wiki` (inside `JiraClient.create_issue`/`update_issue`/`post_comment`)
  — preserved as-is on the moved client.

---

## Verification — done condition

After commit 8:

```powershell
$env:VIRTUAL_ENV = $null
uv run pytest -x --tb=short
uv run pytest -m live -x
uv run f2j parse examples\bugs_for_dev_review_2026_05_04.md
uv run f2j enrich state.json --only ARB-AUTH-001 --concurrency 1
uv run f2j upload state.json --only ARB-AUTH-001 --dry-run
uv run f2j jira whoami
uv run f2j jira fields --from-issue CENTPM-1253
uv run jira-task-agent run --capture data\would_send.json --target-epic CENTPM-1253
uv run jira-task-agent run --apply --only V11_Dashboard_Tasks.md --target-epic CENTPM-1253
```

The two greps that close stage 3:

```powershell
Select-String -Path src\_shared\**\*.py -Pattern 'jira_task_agent|file_to_jira'
Select-String -Path src\file_to_jira\**\*.py -Pattern 'jira_task_agent\.jira'
```

Both should return zero matches.

---

## Picking this up — handoff context

Everything a fresh dev needs to continue from where we stopped (end of
commit 3). Pair this with the plan body above and the
[CLAUDE.md](../CLAUDE.md) repo guidance.

### Where we are

- **Branch:** `scope/final-tool-abstract`. The bundled c1+c2+c3 commit is
  the most recent. `git log --oneline -5` should show it on top of
  stage-2 commits (`8268673` README, `8e51dae` gitignore + drive smoke,
  `0ca8bc6` parse `--source`, `6a962d1` review verification, `2f73f92`
  shared sources).
- **Verify state with:**
  ```powershell
  $env:VIRTUAL_ENV = $null
  uv run pytest tests/_shared tests/jira_task_agent tests/file_to_jira `
      --deselect tests/file_to_jira/test_jira.py::test_payload_includes_summary_priority_labels
  # Expect: 402 passed, 24 deselected
  ```

### Environment quick-start (Windows + PowerShell)

```powershell
uv sync --extra dev
$env:VIRTUAL_ENV = $null   # required once per PowerShell session before `uv run`
                           # (Windows ships an inherited venv that confuses uv)
```

`.env` shape needed for live work — both flavors are honored:

| Variable | Used by | Notes |
|---|---|---|
| `JIRA_PAT` *or* `JIRA_TOKEN` | f2j / drive | Server/DC PAT for `jirasw.nvidia.com` |
| `JIRA_HOST=jirasw.nvidia.com` | drive | f2j reads `cfg.jira.url` from `f2j.yaml` instead |
| `JIRA_PROJECT_KEY=CENTPM` | drive | f2j reads `cfg.jira.project_key` |
| `JIRA_AUTH_MODE=bearer` | shared client | Default; set `basic` for Cloud (requires `JIRA_USER_EMAIL`) |
| `NVIDIA_LLM_API_KEY` *or* `NVIDIA_API_KEY` | f2j / drive | Same value, both names accepted |
| `NVIDIA_BASE_URL=https://inference-api.nvidia.com/v1/` | both | |
| `LLM_MODEL_CLASSIFY` / `LLM_MODEL_EXTRACT` / `LLM_MODEL_SUMMARIZE` | drive | Override per-task model — Sharon's NVIDIA key only allows `default-models`, set to `openai/openai/gpt-5.4-mini` |
| `FOLDER_ID` | drive Drive source | Only needed for `--source gdrive` |

If `.env` only has f2j-style names, use
[scripts/smoke_drive_capture.py](../scripts/smoke_drive_capture.py) — it
bridges `JIRA_PAT → JIRA_TOKEN`, `NVIDIA_LLM_API_KEY → NVIDIA_API_KEY`
in-process before invoking jira-task-agent.

### Sub-decisions locked in during c1-3 (not just plan, now reality)

1. **`_build_auth_header(token, auth_mode=None, user_email=None)`** —
   when `auth_mode is None`, falls back to the `JIRA_AUTH_MODE` env var
   (default `"bearer"`); when `user_email is None` and mode is `basic`,
   falls back to `JIRA_USER_EMAIL`. This preserves the legacy single-arg
   call site `_build_auth_header(token)` that drive's `from_env` uses,
   while letting f2j's CLI pass explicit kwargs and skip env mutation.
2. **`JiraClient` is a `@dataclass`** with fields `host`, `auth_header`,
   `auth_mode`, `verify_ssl: bool | str = True`. Instantiate directly
   (drive pattern) or via `from_env()` / `from_config()`.
3. **`JiraClient.from_config(*, url, pat, auth_mode="bearer",
   user_email=None, ca_bundle=None)`** — explicit-args constructor; no
   env reads. `ca_bundle` translates to `verify_ssl=ca_bundle if
   ca_bundle else True`.
4. **Retry decorator (`_retry_transient`)** is hand-rolled (no tenacity),
   applied to `get`/`put`/`post`. 4 attempts, 0.5/1/2/4s backoff. Only
   retries `requests.ConnectionError`, `requests.Timeout`, and
   `requests.HTTPError` with status in {429, 502, 503, 504}. Higher-level
   methods (`create_issue`, `update_issue`, etc.) inherit retry
   transparently.
5. **`get_issue` semantics** — `JiraSink.get_issue(key)` returns the
   **raw** Jira REST dict (`{key, fields: {summary, status: {name,...},
   assignee: {...}, ...}}`). `JiraSink.get_issue_normalized(key)` returns
   drive's **flat** shape (top-level `status`, `assignee_username`, etc.)
   — this is what `pipeline/reconciler.py:109` consumes today.
6. **`CapturingJiraSink` works by mutating its underlying client.** It
   overrides nothing on the sink itself; instead its `__init__` rewires
   `client.post` and `client.put` to recorder closures. Reads pass
   through naturally because `client.get` is untouched. Higher-level
   client methods (`create_issue` / `update_issue` / `post_comment` /
   `transition_issue`) inherit the diversion because they're built on
   `self.post` / `self.put`. **Don't share a client across a
   CapturingJiraSink and a real JiraSink** — the patching is in-place and
   irreversible for the client's lifetime.
7. **Topology hash is provider-agnostic.** `compute_project_topology_sha`
   in [src/jira_task_agent/pipeline/matcher.py:623–645](../src/jira_task_agent/pipeline/matcher.py#L623)
   hashes `json.dumps(tree, sort_keys=True)` over the dict returned by
   `fetch_project_tree`. The `sort_keys=True` protects dict-key order
   but **not list order** — preserve epic / child ordering exactly as
   `_normalize_min` produces them.

### Starting commit 4 — exact lines to edit

`src/jira_task_agent/pipeline/reconciler.py`:

| Line(s) | Current | Change to |
|---|---|---|
| 14 | `from ..jira.client import JiraClient, get_issue` | drop both — neither is used after this commit |
| 14 (insert) | — | `from _shared.io.sinks.jira import JiraSink` + `from _shared.io.sinks.base import AssigneeResolver` |
| 62-66 | `def build_plans_from_dirty(sections, *, client: JiraClient)` | `def build_plans_from_dirty(sections, *, sink: JiraSink, resolver: AssigneeResolver)` |
| 77 | `_build_epic_group(section, client)` | `_build_epic_group(section, sink, resolver)` |
| 81 | `def _build_epic_group(section, client)` | `def _build_epic_group(section, sink, resolver)` |
| 82, 90 | `_build_epic_action(section, client)` / `_build_task_actions(section, client, ...)` | thread `sink, resolver` |
| 94 | `def _build_epic_action(section, client)` | `def _build_epic_action(section, sink, resolver)` |
| 103-105, 139-141 | `_resolve_assignee(client, ...)` | `_resolve_assignee(resolver, ...)` |
| 109 | `get_issue(section.matched_jira_key, client=client).get("status")` | `sink.get_issue_normalized(section.matched_jira_key).get("status")` |
| 147-156 | `def _build_task_actions(section, client, epic_key)` | `def _build_task_actions(section, resolver, epic_key)` (no get_issue calls inside, so doesn't need sink) |
| 172-181 | `def _build_task_action(t, client, ...)` + `_resolve_assignee(client, ...)` | `def _build_task_action(t, resolver, ...)` + `_resolve_assignee(resolver, ...)` |
| 234-235 | `def _resolve_assignee(client: JiraClient, raw)` → `client.resolve_assignee_username(raw) if raw else None` | `def _resolve_assignee(resolver: AssigneeResolver, raw)` → `resolver.resolve(raw) if raw else None` |

`src/jira_task_agent/runner.py`:

| Line | Current | Change to |
|---|---|---|
| 301 | `jira = JiraClient.from_env()` | leave for now (c5 wraps it in a sink) |
| ~341 (insert) | — | `from _shared.io.sinks.jira import JiraSink`, `from _shared.io.sinks.jira.strategies import StaticMapStrategy` |
| ~341 (insert) | — | `_resolver = StaticMapStrategy()` ; `_sink = JiraSink(client=jira, project_key=project_key, assignee_resolver=_resolver, filter_components=False)` (the sink here is plan-build-only — c5 expands it to cover writes + replaces with `CapturingJiraSink` when `--capture`) |
| 342 | `plans = build_plans_from_dirty(dirty_sections, client=jira)` | `plans = build_plans_from_dirty(dirty_sections, sink=_sink, resolver=_resolver)` |

The runner's apply path (lines 485–545) and capture mode (302–304,
429–448) remain unchanged in c4 — that's c5's territory.

### Test gate command (use this for every offline check)

```powershell
$env:VIRTUAL_ENV = $null
uv run pytest tests/_shared tests/jira_task_agent tests/file_to_jira `
    --deselect tests/file_to_jira/test_jira.py::test_payload_includes_summary_priority_labels `
    --tb=short
```

The deselect handles a pre-existing failure inherited from the f2j branch
pre-merge — components are filtered against the project's live component
list; CENTPM has none, so the field is dropped. **Not a stage-3
regression, ignore.**

### Sharon-specific operating notes

- **Commit boundary**: Sharon commits himself. `git add` at the end of
  each commit's work, present what's staged, then **stop** — don't run
  `git commit`. (Memory: `feedback_commits.md`.)
- **Standing test epic for live runs**: `CENTPM-1253` — pass
  `--target-epic CENTPM-1253` when applying writes. (PM sweeps it
  periodically; the PAT can't delete tickets.)
- **No Google OAuth** on Sharon's box — `--source gdrive` won't work
  without his setting up `credentials.json` + `token.json`. Use
  `--source local` or the default `--source file` for f2j; for the
  drive body, smoke against `data/local_files/` if needed.
- **PowerShell venv quirk**: `$env:VIRTUAL_ENV = $null` once per session
  before any `uv run`. The shell inherits a Python 3.14 venv from
  upstream that confuses uv otherwise.

### Where to see the pre-stage-3 baseline

The 8 tests added in c3 are the only structural test-count change.
Pre-stage-3 baseline was 394 passed / 1 pre-existing failure / 23 live.
Post-c3 baseline is 402 passed / 1 pre-existing failure / 23 live.
Anything else moving needs investigation before declaring c4 done.

### Files modified in c1+c2+c3 (combined)

```
src/_shared/io/sinks/jira/__init__.py      (+11 / -2)   exports + CapturingJiraSink
src/_shared/io/sinks/jira/client.py        NEW (+704)   the unified client
src/_shared/io/sinks/jira/project_tree.py  NEW (+150)   moved from drive
src/_shared/io/sinks/jira/sink.py          (+102 / -1)  get_issue_normalized + fetch_project_tree + CapturingJiraSink
src/jira_task_agent/jira/client.py         (-501 + 13)  → 13-line shim (deleted in c6)
src/jira_task_agent/jira/project_tree.py   (-148 + 4)   → 4-line shim (deleted in c6)
src/file_to_jira/upload.py                 (+2 / -2)    import path
tests/_shared/io/sinks/test_jira_sink.py   (+212)       8 new tests
docs/stage_3_plan.md                       NEW          this doc
```
