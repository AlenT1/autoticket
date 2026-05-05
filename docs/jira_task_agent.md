# Jira Task Agent

Doc-to-Jira sync agent. Mirrors planning docs in a Google Drive folder
and/or a local directory into the Jira project `CENTPM`: classifies
each doc, extracts `{epic, tasks}` via an LLM, matches the extracted
items against live Jira via a two-stage LLM matcher, then creates,
updates, or no-ops. Writes are gated behind `--apply`; dry-run is the
default and produces a human-readable plan for review.

## Quick start

```sh
# 1. clone, set up venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. configure .env (copy .env.example and fill in)
cp .env.example .env
$EDITOR .env

# 3. run dry-run (no Jira writes); review data/run_plan.json
.venv/bin/python -m jira_task_agent run

# 4. apply for real
.venv/bin/python -m jira_task_agent run --apply
```

Required `.env` keys: `FOLDER_ID`, `JIRA_HOST`, `JIRA_PROJECT_KEY`,
`JIRA_AUTH_MODE`, `JIRA_TOKEN`, `NVIDIA_API_KEY`, `NVIDIA_BASE_URL`,
`LLM_MODEL_CLASSIFY`, `LLM_MODEL_EXTRACT`, `LLM_MODEL_SUMMARIZE`.
Google OAuth files (`credentials.json`, `token.json`) are needed only
when reading from Drive.

## CLI

```sh
# default: read both Drive + data/local_files/, dry-run
python -m jira_task_agent run

# choose source: gdrive | local | both
python -m jira_task_agent run --source local
python -m jira_task_agent run --source gdrive

# narrow to one file
python -m jira_task_agent run --only V11_Dashboard_Tasks.md

# override the last_run_at cursor
python -m jira_task_agent run --since 2026-01-01

# write to Jira
python -m jira_task_agent run --apply

# capture intended writes to a JSON without sending
python -m jira_task_agent run --capture data/would_send.json

# bypass cache (force re-classify + re-extract)
python -m jira_task_agent run --no-cache
```

## Pipeline

```
Drive | local ──► classify ──► bundle root ──► extract (cold | diff | reuse)
                                                       │
Jira project ──fetch_project_tree (1 query)──► [tree]  │
                                                       ▼
                          run_matcher (Stage 1: epics, Stage 2: tasks)
                                                       │
                                          filter_dirty │
                                                       ▼
                          build_plans_from_dirty ──► ReconcilePlan
                                                       │
                                       ┌───────────────┴───────────────┐
                                     dry-run                       --apply
                              (run_plan.json + .md)             (Jira writes)
```

| Stage | Module |
|---|---|
| List + download (Drive + local) | `drive/client.py` |
| Classify | `pipeline/classifier.py` |
| Bundle root | `pipeline/context_bundler.py` |
| Extract (cold + multi + diff + targeted) | `pipeline/extractor.py` |
| Per-file extract w/ caching | `pipeline/file_extract.py` |
| Two-stage LLM matcher | `pipeline/matcher.py` |
| Per-file match w/ caching | `pipeline/file_match.py` |
| Filter to dirty changes | `pipeline/dirty_filter.py` |
| Reconcile (action emitter) | `pipeline/reconciler.py` |
| Apply / capture | `runner.py::_apply_plan` |
| Render run plan as MD | `pipeline/run_plan_md.py` |
| Orchestrate | `runner.py::run_once` |
| CLI | `__main__.py` |
| Cursor + cache | `state.py`, `cache.py` |

## Caching

`data/cache.json` (regenerated on every run, never committed):

- **Tier 1** classification cache, key `(file_id, modified_time)`.
- **Tier 2** extraction cache, key `(file_id, content_sha)`.
- **Tier 3** matcher cache, key `(file_id, content_sha,
  project_topology_sha, matcher_prompt_sha)`.

Warm runs with no doc changes cost ~0 LLM tokens. Bumping the matcher
prompt or the model id invalidates Tier 3 cache project-wide.

## Tests

Offline (default — fast, no API calls):

```sh
.venv/bin/pytest
```

Live (real LLM + Jira reads, opt-in):

```sh
.venv/bin/pytest -m live
```

Notable live tests:

- `test_may1_full_pipeline_live.py` — May1 doc with 3 mutations →
  exactly 7 captured Jira ops.
- `test_mixed_warm_and_new_live.py` — May1 warm + V11 cold in one run.
- `test_local_e2e_md_live.py` — local-folder E2E in capture mode,
  generates a human-readable run plan MD.

## Run plan output

Every run writes `data/run_plan.json` (machine) and (when used via the
live test path) `data/run_plan*.md` (human view). The MD is a pure
deterministic render of the JSON: same JSON in → same MD out, no LLM
calls, no extra Jira reads. Re-render at any time with:

```sh
.venv/bin/python -c "import json; \
from jira_task_agent.pipeline.run_plan_md import render_run_plan_md; \
print(render_run_plan_md(json.load(open('data/run_plan.json'))))"
```

## Conventions

- **Identification** is by the LLM matcher + Tier 3 cache, never by
  label or remote-link. `ai-generated` is a content marker only.
- **Markdown is the LLM lingua franca**; the Jira wrapper converts
  MD → wiki at the boundary (`_md_to_jira_wiki`). Don't pre-render
  wiki upstream.
- **Assignee resolution is static** via `team_mapping.json`.
- **The doc is the source of truth.** A doc edit that maps to an
  existing Jira issue triggers an update; the prior Jira body lives
  in Jira's edit history. The agent stamps every body it writes with
  `<!-- managed-by:jira-task-agent v1 -->` as a "last touched by
  agent" indicator.

## Layout

```
jira_task_agent/        # core package
  drive/                # Google Drive + local-folder readers
  jira/                 # Jira REST wrapper + project-tree fetch
  llm/                  # NVIDIA Inference (OpenAI-compatible) client
    prompts/            # markdown prompt templates
      extract/          # cold + diff + targeted body extraction
      match/            # epic + issue matchers
  pipeline/             # classify, extract, match, filter, reconcile, render
  runner.py             # orchestrator
  __main__.py           # CLI

scripts/                # stage-isolated dev scripts (list, classify, extract)
tests/                  # offline + live tests
data/                   # runtime artifacts (gitignored)
```
