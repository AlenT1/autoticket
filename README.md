# file-to-jira-tickets

Two complementary tools for getting work into Jira, sharing one repo and one
set of abstractions:

| Tool | Input | Output | Pattern |
|---|---|---|---|
| **`f2j`** ([file_to_jira](docs/file_to_jira.md)) | A markdown bug-list file | Jira **Bug** tickets | One LLM tool-use agent per bug — clones the source repo, browses code, runs `git blame`, validates file paths, then submits |
| **`jira-task-agent`** ([jira_task_agent](docs/jira_task_agent.md)) | A Google Drive folder (or local docs dir) | Jira **Epics + Tasks** | Pipeline of structured LLM calls: classify → extract → 2-stage match → reconcile, with 3-tier cache + diff-aware extraction |

The bodies stay distinct (each pattern fits its problem), but they share
the **edges** — input source plumbing, LLM provider, and Jira output sink.

## Quick start

```sh
uv sync --extra dev          # install everything
cp .env.example .env         # fill in JIRA_TOKEN, NVIDIA_API_KEY, FOLDER_ID, ...
uv run f2j --help            # CLI 1
uv run jira-task-agent --help # CLI 2
```

Per-tool walkthroughs:
- [docs/file_to_jira.md](docs/file_to_jira.md) — operator's quick-start for `f2j` (parse → enrich → upload)
- [docs/jira_task_agent.md](docs/jira_task_agent.md) — operator's quick-start for `jira-task-agent` (Drive → Jira sync)

## Architecture — sandwich

```
┌─ Shared INPUT layer (src/_shared/io/sources/) ──────────────────────┐
│   Source protocol — yields RawDocument(id, name, content, mtime, …) │
│   Impls: GDriveSource, LocalFolderSource, SingleFileSource          │
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
│   Impls: JiraSink (today). Future: MondaySink, LinearSink.           │
│   Strategies (Jira-flavored):                                        │
│     IdentificationStrategy — LabelSearch (f2j) / CacheTrust (drive)  │
│     AssigneeResolver       — PickerWithCache (f2j) / StaticMap (drv) │
│     EpicRouter             — DeterministicChain (f2j) / NoOp (drive) │
└──────────────────────────────────────────────────────────────────────┘
```

## Layout

```
file-to-jira-tickets/
├── pyproject.toml                  # uv-managed; both packages, both CLIs
├── .env / .env.example
├── README.md                       # this file
├── CLAUDE.md                       # repo-level guidance for Claude Code
├── docs/
│   ├── file_to_jira.md             # f2j operator docs
│   ├── file_to_jira_troubleshooting.md
│   ├── file_to_jira_project_summary.md
│   └── jira_task_agent.md          # drive operator docs
│
├── src/
│   ├── _shared/                    # shared abstractions (the edges)
│   │   ├── io/sources/             # Source + RawDocument + GDrive/Local/SingleFile
│   │   ├── io/sinks/               # TicketSink + Ticket + JiraSink + strategies
│   │   └── llm/                    # LLMProvider + OpenAICompat + Anthropic
│   ├── file_to_jira/               # f2j body (agentic enrichment)
│   │   ├── parse/                  # bug-list markdown parser
│   │   ├── enrich/                 # tool-use agent + toolkit + linter
│   │   ├── repocache/              # shallow clone cache
│   │   ├── input.py                # F2JFileSource (encoding-aware)
│   │   ├── upload.py               # JiraSink-based upload orchestrator
│   │   └── cli.py                  # `f2j` CLI (Typer)
│   └── jira_task_agent/            # drive body (pipelined sync)
│       ├── pipeline/               # classify, extract, match, reconcile, ...
│       ├── llm/prompts/            # 8 markdown prompt templates
│       ├── cache.py                # 3-tier matcher cache
│       ├── runner.py               # orchestrator
│       └── __main__.py             # `jira-task-agent` CLI (argparse)
│
├── prompts/file_to_jira/           # f2j system prompt (versioned)
├── configs/                        # f2j layered YAML configs
├── examples/                       # f2j sample bug-list inputs (gitignored)
├── data/                           # drive runtime artifacts (gitignored)
├── scripts/                        # both projects' helper scripts
└── tests/
    ├── _shared/                    # shared-layer contract tests
    ├── file_to_jira/               # f2j unit tests
    └── jira_task_agent/            # drive offline + live tests
```

## Tests

```sh
uv run pytest -m "not live" -q                 # offline, ~20s, ~400 tests
uv run pytest -m live -q                       # live LLM/Jira (opt-in, real $)
uv run pytest tests/_shared -q                 # shared-layer contracts only
uv run pytest tests/file_to_jira -q            # f2j only
uv run pytest tests/jira_task_agent -q         # drive only (offline default)
```

## Library use (autodev / embedding)

```python
# f2j: bug list → Jira bugs
import file_to_jira as f2j
cfg = f2j.load_config()
f2j.parse_markdown(decoded_text, source_sha256=sha)
f2j.run_enrich(state_file=path, cfg=cfg)
f2j.upload_state(state_file=path, cfg=cfg)

# jira_task_agent: Drive folder → Jira epics+tasks
from jira_task_agent import run_once, RunReport
report: RunReport = run_once(apply=True, source="both")

# Shared abstractions (for stage 4 autodev consumption)
from _shared.llm import LLMProvider, OpenAICompatProvider
from _shared.io.sources import RawDocument, GDriveSource, SingleFileSource
from _shared.io.sinks import Ticket, TicketSink
from _shared.io.sinks.jira import JiraSink
```
