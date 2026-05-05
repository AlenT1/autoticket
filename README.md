# file-to-jira-tickets

Get planning + bug-tracking work into Jira automatically. Two complementary
tools live in this repo and share one set of abstractions, so the same input
sources, LLM provider, and Jira sink power both:

| Tool | Input | Output | How it works |
|---|---|---|---|
| **`f2j`** | A markdown bug-list file (or Drive folder, or local dir) | Jira **Bug** tickets | One LLM tool-use agent per bug — clones the source repo, browses code, runs `git blame`, validates file paths, submits a structured report |
| **`jira-task-agent`** | A Google Drive folder (or local dir) | Jira **Epics + Tasks** | Pipeline of structured LLM calls: classify → extract → 2-stage match → reconcile, with 3-tier cache + diff-aware extraction |

Both write Markdown descriptions; the shared sink converts to Jira wiki at the
boundary. Both honor the autodev token convention. Both are dry-run-by-default
and idempotent on re-run.

---

## Quick start

```sh
uv sync --extra dev                         # install (uv handles Python 3.11 + .venv)
cp .env.example .env                        # fill in tokens (see "Secrets" below)
uv run f2j --help                           # CLI 1: bug list → Jira bugs
uv run jira-task-agent --help               # CLI 2: drive folder → Jira epics+tasks
```

Pre-flight self-check:

```sh
uv run f2j validate-config                  # readiness table for parse / enrich / upload
uv run f2j jira whoami                      # confirms JIRA_PAT works
```

---

## f2j — the bug-list flow, end to end

f2j turns a markdown bug review document into Jira tickets, enriched with code
context by an AI agent. **Three commands you'll actually use:**

```sh
uv run f2j parse <input>            # → state.json
uv run f2j enrich state.json        # → enriches in place via NVIDIA Inference
uv run f2j upload state.json        # → creates tickets in Jira (dry-run-able)
```

Every command is **resumable** — re-running picks up where it left off. Every
command is **filterable** with `--only <bug_id>` for single-bug runs. State
lives in `state.json` (atomic write + rolling backups) and is `.gitignore`d.

### Step 1 — parse (pick your input source)

f2j inherits drive's `Source` protocol post-merge, so the parse command
accepts three input modes:

```sh
# (default) — local markdown file
uv run f2j parse examples/bugs_for_dev_review_2026_05_04.md --out state.json --force

# from a folder of .md / .html / .txt files (drive-style)
uv run f2j parse --source local --only bugs_for_dev_review_2026_05_04.md \
    --local-dir data/local_files --out state.json --force

# from Google Drive (needs FOLDER_ID + credentials.json + token.json)
uv run f2j parse --source gdrive --only V11_Bug_Review.md --out state.json --force
```

Behind the scenes:
- `--source file` (default) routes through `F2JFileSource` — does encoding
  detection (UTF-8 → CP1252 → Latin-1 fallback), strips BOM, normalizes line
  endings. Best for arbitrary local markdown.
- `--source local` routes through `LocalFolderSource` (the same one
  `jira-task-agent` uses). Plain UTF-8.
- `--source gdrive` routes through `GDriveSource`. Reads the Drive folder
  named by `FOLDER_ID` (or `--folder-id`); requires Google OAuth setup
  (`credentials.json` + `token.json` in the repo root, generated on first
  use).

`--only <filename>` is required for `gdrive` and `local` modes (they enumerate
multiple files; `--only` says which one is the bug list).

The output `state.json` records:
- Parsed bugs with their hinted module / branch / commit
- A `source_file` label that identifies the input mode used
- The content sha (for cache lookups later)

### Step 2 — enrich (one LLM agent session per bug)

```sh
# Enrich one bug — recommended for first runs (~$0.10, ~30s)
uv run f2j enrich state.json --only ARB-AUTH-001 --concurrency 1

# Enrich everything, parallelized
uv run f2j enrich state.json --concurrency 4

# Retry only previously-failed bugs
uv run f2j enrich state.json --retry-failed
```

Each bug gets one tool-use agent session. The agent sees:
- The bug's parsed metadata (external_id, hinted_priority, hinted_files, ...)
- The bug's raw body verbatim
- A curated list of available epics (from `f2j.yaml`)
- A toolkit: `clone_repo`, `search_code`, `read_file`, `list_dir`, `git_blame`,
  `git_log_for_path`, `submit_enrichment` (final action)

The agent investigates, then calls `submit_enrichment` with a structured
report. The submit tool **validates every `code_references[].file_path`
against the actual cloned repo** before accepting — invalid paths come back
as a tool error so the agent self-corrects.

Three layers prevent fix-proposal language from leaking into Jira tickets:
1. The system prompt forbids prescriptive language
2. The parser strips `What's needed:` / `What was changed:` sections from the
   source markdown before the agent sees it
3. A post-hoc linter scans the enriched description for phrases like
   "should be" / "the fix is" / "wrap with" and removes them

### Step 3 — inspect what the agent produced

```sh
# Summary table
uv run f2j inspect state.json

# One bug, full detail
uv run f2j inspect state.json --bug ARB-AUTH-001

# Plus the stripped fix-proposal text (so you can verify nothing important got cut)
uv run f2j inspect state.json --bug ARB-AUTH-001 --show-stripped
```

Notice the inspect view shows two epic lines:
- `Epic (LLM pick)` — what the agent suggested
- `Epic (will use)` — what the deterministic priority chain at upload time
  will actually pick. These can differ; the chain wins.

### Step 4 — upload (dry-run first, then live)

```sh
# DRY-RUN: builds the Jira REST payload but does NOT POST. Free.
uv run f2j upload state.json --only ARB-AUTH-001 --dry-run

# Live upload (creates a real ticket)
uv run f2j upload state.json --only ARB-AUTH-001
```

What the upload path does, in order:

1. **Idempotency check** — JQL search for `labels = "upstream:<external_id>"`.
   If found, records the existing key without creating a duplicate. Re-runs
   are safe.
2. **Build a Ticket** (the unified shape from `_shared/io/sinks/`). The
   markdown description is held as-is here; conversion to Jira wiki happens
   at the sink boundary.
3. **Resolve the assignee** — walks
   `enriched.assignee_hint → parsed.hinted_assignee → module_to_assignee[<repo_alias>] → default_assignee`.
   The `PickerWithCacheStrategy` then translates the chosen display name to a
   Jira username via `configs/user_map.yaml`, falling back to `/user/picker`
   for misses. Successful picker hits cache back to the YAML.
4. **Route the epic link** — `DeterministicChainStrategy` walks
   `external_id_prefix_to_epic[<longest match>] → module_to_epic[<repo_alias>] → enriched.epic_key (if curated) → default_epic`.
5. **Filter components** against the project's live component list. CENTPM
   has zero components, so the field is omitted entirely; if you have
   components, agent-invented values get silently dropped.
6. **POST the issue** to Jira (or skip if `--dry-run`).
7. **Create configured subtasks** under the new parent (per
   `f2j.yaml.subtasks`).
8. **Persist `UploadResult` to state.json** so re-runs short-circuit.

### Common workflows

```sh
# End-to-end, one shot, parallel
uv run f2j run examples/bugs_for_dev_review_2026_05_04.md --concurrency 4 --force

# Same but skip the live upload step (review state.json first)
uv run f2j run input.md --no-upload

# Same but dry-run upload (build payloads, don't post)
uv run f2j run input.md --dry-run-upload
```

### `validate-config` — pre-flight readiness

```sh
uv run f2j validate-config
```

Prints a readiness table — `ready` / `not ready` per stage with hints on
what's missing. Run this after editing `f2j.yaml` or `.env`.

### `jira` diagnostics

```sh
# Verify your JIRA_PAT works
uv run f2j jira whoami

# Discover Jira custom-field IDs for your project
uv run f2j jira fields --project CENTPM --issue-type Bug

# Or discover from an existing ticket (works even when createmeta is restricted)
uv run f2j jira fields --from-issue CENTPM-1285
```

Use the field IDs to fill in `f2j.yaml.jira.field_map.bug.<custom_field>`.

---

## jira-task-agent — the doc-sync flow

Mirrors planning docs in a Drive folder (or local dir) into a Jira project's
epic+task tree.

```sh
# Dry-run: read everything, write nothing. Always produces data/run_plan.json.
uv run jira-task-agent run

# Capture intended Jira writes to a file (useful for review)
uv run jira-task-agent run --capture data/would_send.json --target-epic CENTPM-1253

# Live writes
uv run jira-task-agent run --apply

# Just one file, with all created tasks scoped to a standing test epic
uv run jira-task-agent run --apply --only V11_Dashboard_Tasks.md --target-epic CENTPM-1253
```

The pipeline classifies each doc (`single_epic | multi_epic | root`),
extracts `{epic, tasks}` via LLM, matches against the live Jira project tree
using a 2-stage LLM matcher, and emits actions
(`create_epic / update_epic / noop / create_task / update_task / orphan`).

Warm runs with no doc changes cost ≈ 0 LLM tokens thanks to the 3-tier cache:
- Tier 1 — classification (keyed by file_id + modified_time)
- Tier 2 — extraction (keyed by file_id + content_sha) with diff-aware re-extract
- Tier 3 — matcher (keyed by file_id + content_sha + project_topology_sha + matcher_prompt_sha)

Full operator walkthrough: [docs/jira_task_agent.md](docs/jira_task_agent.md).

---

## Architecture — sandwich

The merge result. Bodies stay distinct (each pattern fits its problem), but
they share the **edges** — input sources, LLM provider, Jira output sink.
Stage 3+ adds new providers/sinks/sources by implementing the same protocols.

```
┌─ Shared INPUT layer (src/_shared/io/sources/) ──────────────────────┐
│   Source protocol — yields RawDocument(id, name, content, mtime, …) │
│   Impls: GDriveSource, LocalFolderSource, SingleFileSource          │
│   f2j adds: F2JFileSource (encoding-aware) in src/file_to_jira/input.py │
└──────────────────────────────────────────────────────────────────────┘
            ↓
┌─ f2j BODY (agentic) ─────┐   ┌─ jira_task_agent BODY (pipelined) ───┐
│   parse bug list         │   │   classify → extract (cold/diff/     │
│   tool-use agent loop    │   │     reuse, 3-tier cache) → 2-stage   │
│   (clone, grep, blame)   │   │     LLM matcher → reconcile          │
│   linter strips fix-     │   │   action plan: create/update/noop    │
│     proposal language    │   │                                       │
└───────────────────────────┘   └───────────────────────────────────────┘
            ↓                                          ↓
┌─ Shared LLM provider layer (src/_shared/llm/) ──────────────────────┐
│   LLMProvider ABC                                                    │
│     chat(messages, response_format=…)         (JSON-mode pipelines)  │
│     chat_with_tools(messages, tools, …)       (agentic loops)        │
│   Impls: OpenAICompatProvider (NVIDIA Inference, ...),               │
│          AnthropicProvider                                           │
└──────────────────────────────────────────────────────────────────────┘
            ↓
┌─ Shared OUTPUT layer (src/_shared/io/sinks/) ───────────────────────┐
│   TicketSink protocol + Ticket shape (tracker-agnostic)              │
│   Impl: JiraSink (today). Future: MondaySink, LinearSink (stage 3+). │
│   Strategies (Jira-flavored), pluggable per tool:                    │
│     IdentificationStrategy — LabelSearch (f2j) / CacheTrust (drive)  │
│     AssigneeResolver       — PickerWithCache (f2j) / StaticMap (drv) │
│     EpicRouter             — DeterministicChain (f2j) / NoOp (drive) │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Configuration

Layered, later-wins (per [src/file_to_jira/config.py](src/file_to_jira/config.py)):

1. `configs/default.yaml` (shipped baseline)
2. `~/.config/f2j/config.yaml` or `%APPDATA%\f2j\config.yaml` (per-operator)
3. `./f2j.yaml` (project-local; gitignored)
4. `--config <path>` CLI override
5. `F2J_*` env vars (nested via `__`)
6. CLI flags

Secrets live in `.env`, never in YAML. Only the env-var **name** lives in
config (e.g. `openai_compatible.api_key_env: NVIDIA_LLM_API_KEY`).

### `f2j.yaml` essentials

```yaml
enrichment:
  provider: "openai_compatible"

openai_compatible:
  base_url_env: "NVIDIA_BASE_URL"
  api_key_env:  "NVIDIA_LLM_API_KEY"
  model: "openai/openai/gpt-5.4-mini"

jira:
  url: "https://jirasw.nvidia.com"
  project_key: "CENTPM"
  issue_type: "Bug"
  default_assignee: "Guy Keinan"
  module_to_assignee:
    _core: "Yair Sadan"

  priority_values:
    P0: "P0 - Must have"
    P1: "P1 - Should have"
    P2: "P2 - Nice to have"
    P3: "P3 - Low"

  epic_link_field: "customfield_10005"
  default_epic: "CENTPM-1184"
  external_id_prefix_to_epic:
    "X-OBS-": "CENTPM-1179"
    "X-SEC-": "CENTPM-1162"
    "ARB-":   "CENTPM-1197"
  module_to_epic:
    _core: "CENTPM-1184"
    vibe_coding/centarb: "CENTPM-1197"
```

### Secrets (`.env`)

```ini
# Jira
JIRA_PAT=...                                   # Server / DC PAT, OR
JIRA_TOKEN=...                                 # autodev-style alias
JIRA_HOST=jirasw.nvidia.com                    # used by jira-task-agent
JIRA_PROJECT_KEY=CENTPM                        # used by jira-task-agent

# LLM (NVIDIA Inference, OpenAI-compatible)
NVIDIA_LLM_API_KEY=...                         # used by f2j
NVIDIA_API_KEY=...                             # used by jira-task-agent (same value)
NVIDIA_BASE_URL=https://inference-api.nvidia.com/v1/

# Optional: pin per-task models (jira-task-agent only)
LLM_MODEL_CLASSIFY=openai/openai/gpt-5.4-mini
LLM_MODEL_EXTRACT=openai/openai/gpt-5.4-mini
LLM_MODEL_SUMMARIZE=openai/openai/gpt-5.4-mini

# Repo cloning (f2j only, when repo_aliases use auth: https-token)
GIT_TOKEN_GITLAB_NVIDIA=glpat-...

# Drive source (jira-task-agent, or f2j with --source gdrive)
FOLDER_ID=<google-drive-folder-uuid>
LOCAL_AUTHOR_NAME="Sharon Gordon"
```

The autodev token chain is honored at the shared Jira client: `JIRA_TOKEN` →
`~/.autodev/tokens/task-jira-${JIRA_PROJECT_KEY}` → `AUTODEV_TOKEN`.

---

## Library use (embedding / autodev consumption)

```python
# f2j body
import file_to_jira as f2j
cfg = f2j.load_config()
f2j.parse_markdown(decoded_text, source_sha256=sha)
f2j.run_enrich(state_file=path, cfg=cfg)
f2j.upload_state(state_file=path, cfg=cfg)

# jira-task-agent body
from jira_task_agent import run_once, RunReport
report: RunReport = run_once(apply=True, source="both")

# Shared abstractions (autodev surface)
from _shared.llm import LLMProvider, OpenAICompatProvider, get_provider
from _shared.io.sources import RawDocument, GDriveSource, SingleFileSource
from _shared.io.sinks import Ticket, TicketSink
from _shared.io.sinks.jira import JiraSink
from _shared.io.sinks.jira.strategies import (
    LabelSearchStrategy, CacheTrustStrategy,
    PickerWithCacheStrategy, StaticMapStrategy,
    DeterministicChainStrategy, NoOpStrategy,
)
```

`from file_to_jira import __version__` and `from jira_task_agent import
__version__` both return `"0.1.0"`.

---

## Hard guarantees (preserved through the merge)

- **No fix proposals** in the Jira description (3 enforcement layers).
- **No hallucinated file paths** — `submit_enrichment` validates every
  `code_references[].file_path` against the actual cloned repo.
- **Idempotent upload** — JQL on `upstream:<external_id>` label.
- **Markdown → Jira wiki** at the sink boundary; bodies don't pre-render.
- **Component & epic guarding** — components filtered against live project
  list; epics route via deterministic chain.
- **Resumable** — atomic writes + rolling `.bak` files + filelock.
- **Doc-as-truth (jira-task-agent)** — only changed items get re-processed
  on warm runs (3-tier cache + diff-aware extract).

---

## Tests

```sh
uv run pytest -m "not live" -q                 # ~400 offline tests, ~20s
uv run pytest -m live -q                       # opt-in live tests (real $)
uv run pytest tests/_shared -q                 # shared-layer contracts only
uv run pytest tests/file_to_jira -q            # f2j only
uv run pytest tests/jira_task_agent -q         # jira_task_agent only
```

Coverage spans the parser, both agent loops (Anthropic + OpenAI-compat with
fake clients), the upload path, the LLM provider layer, the input sources,
and the Jira sink with its strategies. Live tests hit real NVIDIA Inference
+ Jira reads; default `pytest` skips them.

---

## Layout

```
file-to-jira-tickets/
├── pyproject.toml              # uv-managed; all packages, both CLIs
├── .env / .env.example         # secrets (gitignored)
├── README.md                   # this file
├── CLAUDE.md                   # repo-level guidance for Claude Code
├── docs/
│   ├── file_to_jira.md         # f2j operator deep dive
│   ├── file_to_jira_troubleshooting.md
│   ├── file_to_jira_project_summary.md
│   └── jira_task_agent.md      # jira-task-agent operator deep dive
│
├── src/
│   ├── _shared/                # shared abstractions (the edges)
│   │   ├── io/sources/         # Source + RawDocument + GDrive/Local/SingleFile
│   │   ├── io/sinks/           # TicketSink + Ticket + JiraSink + 6 strategies
│   │   └── llm/                # LLMProvider + OpenAICompat + Anthropic + registry
│   ├── file_to_jira/           # f2j body
│   │   ├── parse/              # bug-list markdown parser
│   │   ├── enrich/             # tool-use agent + toolkit + linter
│   │   ├── repocache/          # shallow clone cache
│   │   ├── input.py            # F2JFileSource (encoding-aware)
│   │   ├── upload.py           # JiraSink-based upload orchestrator
│   │   └── cli.py              # `f2j` CLI (Typer)
│   └── jira_task_agent/        # jira-task-agent body
│       ├── pipeline/           # classify, extract, match, reconcile, ...
│       ├── llm/prompts/        # 8 markdown prompt templates
│       ├── cache.py            # 3-tier matcher cache
│       ├── runner.py           # orchestrator
│       └── __main__.py         # `jira-task-agent` CLI (argparse)
│
├── prompts/file_to_jira/       # f2j system prompt (versioned)
├── configs/                    # f2j layered YAML configs
├── examples/                   # f2j sample bug-list inputs (gitignored)
├── data/                       # jira-task-agent runtime artifacts (gitignored)
├── scripts/                    # both projects' helper scripts +
│                               #   smoke_drive_capture.py (env-bridge wrapper)
└── tests/
    ├── _shared/                # shared-layer contract tests
    ├── file_to_jira/           # f2j unit tests
    └── jira_task_agent/        # jira-task-agent offline + live tests
```

---

## Troubleshooting

See [docs/file_to_jira_troubleshooting.md](docs/file_to_jira_troubleshooting.md)
for the symptom-driven playbook (setup glitches, parser warnings, agent
failures, Jira auth issues, repo-clone issues, state-file recovery).

Quick gotchas:

- **Windows + `uv`**: clear inherited venv once per session: `$env:VIRTUAL_ENV = $null`.
- **NVIDIA key 401 `key_model_access_denied`**: your key only allows
  `default-models`. Either request access to the configured model, or
  edit `f2j.yaml` (or set `LLM_MODEL_*` env vars for `jira-task-agent`)
  to a model your key permits, e.g. `openai/openai/gpt-5.4-mini`.
- **`jira-task-agent` from f2j-style `.env`**: f2j uses `JIRA_PAT` /
  `NVIDIA_LLM_API_KEY`; drive expects `JIRA_TOKEN` / `NVIDIA_API_KEY` /
  `JIRA_HOST` / `JIRA_PROJECT_KEY`. Either add the drive-style names to
  `.env`, or use `scripts/smoke_drive_capture.py` which bridges them
  in-process.
- **`f2j --source gdrive`**: needs `credentials.json` + `token.json` from
  Google Cloud Console + a first-time OAuth flow. Without those, use
  `--source local` or the default `--source file`.
