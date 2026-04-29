# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Doc → Jira sync agent. Mirrors planning docs in a Google Drive folder into Jira project `CENTPM`: classifies each doc, extracts `{epic, tasks}` via LLM, matches against live Jira via a two-stage LLM matcher, then creates / updates / no-ops. Writes are gated behind `--apply`; dry-run is the default. See `JIRA_TASK_AGENT_OVERVIEW.md` for the goal, `JIRA_TASK_AGENT_IMPLEMENTATION_PLAN.md` for the canonical plan, `JIRA_TASK_AGENT_DEMO.md` for runnable commands.

## Common commands

All commands run from the project root with the venv at `.venv/`.

```sh
# tests — offline by default (`live` marker excluded via pytest.ini)
.venv/bin/pytest                                    # 70+ unit/logical tests, ~1s
.venv/bin/pytest -m live                            # opt-in: real NVIDIA LLM calls (~30s, ~$0.01)
.venv/bin/pytest tests/test_reconciler_logical.py   # single file
.venv/bin/pytest tests/test_matcher_unit.py::test_name -k pattern

# end-to-end pipeline (CLI)
.venv/bin/python -m jira_task_agent run                          # dry-run: read Drive + Jira, write nothing
.venv/bin/python -m jira_task_agent run --apply                  # writes to Jira
.venv/bin/python -m jira_task_agent run --since 2026-01-01       # override last_run_at cursor
.venv/bin/python -m jira_task_agent run --only V11_Dashboard_Tasks.md  # narrow to one Drive file
.venv/bin/python -m jira_task_agent run --target-epic CENTPM-1253      # route all created tasks under standing test epic
.venv/bin/python -m jira_task_agent run --capture data/would_send.json # implies --apply, but records payloads instead of sending
.venv/bin/python -m jira_task_agent run --no-cache                     # force re-classify and re-extract

# stage-isolated scripts (operate on the same wrapper code as the runner)
.venv/bin/python scripts/list_files.py [--today|--days N|--clean]
.venv/bin/python scripts/classify_files.py
.venv/bin/python scripts/extract_one.py <file_id_or_name>
.venv/bin/python scripts/list_epics.py
.venv/bin/python scripts/list_epic_tree.py CENTPM-1162
.venv/bin/python scripts/list_project_tree.py
```

## Architecture (big picture)

### Stage / module mapping

```
Drive folder ──list──► classify ──► bundle root ──► extract ──┐
                                                              ▼
                                              [extracted: epic + tasks]
                                                              │
Jira project ──fetch_project_tree (1 query)──► [project_tree] ▼
                                                              │
                              run_matcher (Stage 1: epics, Stage 2: grouped tasks)
                                                              │
                                                              ▼
                              build_plans_from_match ──► ReconcilePlan
                                                              │
                                                ┌─────────────┴─────────────┐
                                              dry-run                  --apply
                                          (run_plan.json)         (Jira writes)
```

Code → stage:

| Stage | Module |
|---|---|
| Drive list/download/dedupe | `jira_task_agent/drive/client.py`, `pipeline/dedupe.py` |
| Classify (LLM) | `pipeline/classifier.py` — three roles: `single_epic` / `multi_epic` / `root` |
| Bundle root | `pipeline/context_bundler.py` |
| Extract (LLM) | `pipeline/extractor.py` (single + multi); enforces DoD section, composite-owner `Co-owners:` injection |
| Fetch Jira | `jira/client.py` (low-level), `jira/project_tree.py` (single paginated `/search` returning epics + children) |
| Match (LLM, two-stage) | `pipeline/matcher.py` — `match` + `match_grouped` + `run_matcher` |
| Reconcile | `pipeline/reconciler.py` — pure logic, no LLM calls |
| Apply / capture | `runner.py::_apply_plan` |
| Orchestrate | `runner.py::run_once` |
| CLI | `__main__.py` |
| Cursor + cache | `state.py` (`data/state.json`), `cache.py` (`data/cache.json`) |

### The matcher is LLM-based, not fuzzy

There is no `token_set_ratio`, `rapidfuzz`, or string heuristic in the matching path (rapidfuzz is still in `requirements.txt` but only used by `pipeline/dedupe.py` for filename de-twinning). Any reference in older docs to "fuzzy match" / "token-set ratio" describes the LLM matcher.

- **Stage 1 — epic match.** One LLM call pairs *all* extracted epics across all files in this run vs *all* CENTPM project epics.
- **Stage 2 — task match.** Grouped per matched epic, batched 4 per LLM call, parallel up to 3 workers (`batch_size=4, max_workers=3`). Items in group A only match group A's candidates (scoping enforced in the prompt). The same candidate key may be cited by multiple extracted tasks — reconciler turns that into `covered_by_rollup`, never duplicates.
- Confidence floor `_MIN_CONFIDENCE = 0.70`. Below → treated as no match → `create_*`.
- Description preview to the matcher is **3000 chars** (`_DESCRIPTION_PREVIEW_CHARS`), required to spot rollup-style epic descriptions.

### Reconciler is pure logic

`build_plans_from_match` only emits actions; it never calls the LLM. Action `kind` values: `create_epic`, `update_epic`, `noop`, `skip_completed_epic`, `create_task`, `update_task`, `covered_by_rollup`, `orphan`.

One guard lives here:
- **Status guard** — `_COMPLETED_EPIC_STATUSES = {In Staging, In Review, Done, Closed, Resolved, Cancelled, Won't Do, Won't Fix}`. Active statuses: `Backlog`, `In Progress`.

The doc is the source of truth: a doc edit that maps to an existing Jira issue triggers `update_*` regardless of whether the live description was previously written by a human or by the agent. The changelog comment posted alongside the update notifies the human reviewer; their prior body remains in Jira's edit history. The agent stamps `<!-- managed-by:jira-task-agent v1 -->` on every description it writes — useful as a "last-touched-by-agent" indicator but not consulted as a write gate.

### Boundary conventions

- **Markdown stays the LLM's lingua franca.** The Jira wrapper converts MD → wiki at the boundary (`_md_to_jira_wiki` in `jira/client.py`), applied to descriptions in `create_issue`/`update_issue` and to `post_comment` bodies. Don't pre-render wiki anywhere upstream.
- **Description equality** must compare like-for-like: `_descriptions_equal` runs the extracted markdown through `_md_to_jira_wiki` first, then compares against the live wiki body. Skipping that step makes `noop` impossible to reach.
- **Identification is by remote-link**, never by label. Every agent-created issue carries `ai-generated` as a content marker (set automatically in `JiraClient.create_issue`); identification is via `GET /issue/<key>/remotelink` matching the source `webViewLink`.
- **Assignee resolution is static-only**: `team_mapping.json` maps display name → Jira username. No API guesswork. Composite owners (`Lior + Aviv`) → first name becomes assignee, others appended as `Co-owners:` line in the description (handled in `pipeline/extractor.py`).

### Persistence

- `data/state.json` — single cursor: `{last_run_at, last_run_status}`.
- `data/cache.json` — Tier 1 classify cache keyed by `(file_id, modified_time)`; Tier 2 extract cache keyed by `(file_id, content_sha)`; **Tier 3 matcher cache** keyed by `(file_id, content_sha, project_topology_sha, matcher_prompt_sha)`. Atomic writes via tempfile+rename. mtime change drops stale extraction payload; fresh extraction drops stale matcher payload. Bumping `matcher.txt` / `matcher_grouped.txt` / `LLM_MODEL_CLASSIFY` / the kind-aware confidence floors invalidates every cached match (via `compute_matcher_prompt_sha`). Any structural change in CENTPM (epic added/removed/renamed, child summary or status change, description rewrites) invalidates via `compute_project_topology_sha`. Cache version is 2 — old (v1) caches are dropped on load. Section-granular caching for multi_epic files (Tier 4) is the obvious next step but not yet implemented.
- `data/snapshots/` — debug artifacts written by scripts (`files.json`, `classifications.json`, `extraction.json`, `epics.json`, `epic_tree.json`, `project_tree.json`).
- `data/gdrive_files/` — local copies of Drive files (Google Docs exported to markdown).
- `data/run_plan.json` — per-run intended actions (always written, even on dry-run).

## Test layout

`pytest.ini` sets `addopts = -m "not live"`, so `pytest` runs only offline unit/logical tests. Live tests:

- `test_classifier_integration.py`, `test_extractor_integration.py`, `test_matcher_integration.py` — hit real LLM with hardcoded inputs.
- `test_matcher_real_data.py`, `test_matcher_real_data_orchestrator.py` — load real artifacts from `data/snapshots/` (extraction.json, project_tree.json, epic_tree.json, epics.json) and run the matcher against them. Assert known-correct pairings (e.g. `"Production Security Hardening" → CENTPM-1162`, plus 6 known task pairings under it). They `pytest.skip` if `NVIDIA_API_KEY` is unset or the snapshot files are missing.

The `live` mark is the only marker. Add it via `pytestmark = [pytest.mark.live]` at module level.

## Project conventions

- **Standing test epic: `CENTPM-1253`** — all dev/smoke-test issues go under it (the PAT can't delete; PM sweeps periodically). Use `--target-epic CENTPM-1253` whenever applying writes during development.
- **Secrets in `.env`** at the project root, not `~/.autodev`. Required keys: `FOLDER_ID`, `JIRA_HOST`, `JIRA_PROJECT_KEY`, `JIRA_AUTH_MODE`, `JIRA_TOKEN`, `NVIDIA_API_KEY`, `NVIDIA_BASE_URL`, `LLM_MODEL_CLASSIFY`, `LLM_MODEL_EXTRACT`, `LLM_MODEL_SUMMARIZE`. Google OAuth files: `credentials.json` + `token.json` (already present).
- **No Jira labels for identification.** `ai-generated` is a content marker only.
- **LLM prompts** live in `jira_task_agent/llm/prompts/` as `.txt` files and are loaded via `render_prompt(template, **vars)` (targeted `replace("{name}", value)`, *not* `str.format()` — JSON examples in prompts contain literal `{}` that break `.format`).
- **NVIDIA Inference is OpenAI-compatible** — the LLM client is the `openai` SDK pointed at `NVIDIA_BASE_URL`. JSON mode is used for every call with Pydantic schema validation + retry-on-invalid-JSON.
