# file-to-jira-tickets — Project Summary

A focused write-up of what this project does, how it's built, and what we accomplished during the bring-up session. Intended to onboard a new operator (or re-orient future-you) without re-reading the whole conversation history.

---

## What it is

`file-to-jira-tickets` (CLI: `f2j`) turns a markdown bug review document into structured, enriched Jira tickets. Instead of a developer copy-pasting bug entries by hand, an AI agent reads each entry, browses the referenced source repo for context (running `git blame`, locating the offending code, reading nearby tests), and produces a factual Jira ticket with file references, code snippets, and reproduction steps. It uploads to Jira idempotently and resumably so you can iterate fearlessly.

```
input.md  →  parse  →  state.json  →  enrich (LLM agent)  →  upload (Jira)
```

## Motivation

NVIDIA's Central platform team produces periodic bug review docs (`Bugs_For_Dev_Review_<date>.md`) — a single markdown file with 20–250 bug entries, each containing a title, hypothesis, hinted file paths, and a free-text description. The existing flow was:

- A human reads the doc.
- For each bug, they manually create a Jira ticket: copy the title, write a description, set priority, assign someone, link an epic.
- That's tedious, error-prone, and the resulting tickets often lack the code-context that would let a dev jump straight to the fix.

The goal: automate ticket creation while keeping ticket quality high. Specifically:

- **No fix proposals in tickets** — descriptions describe *what* is broken and *where*, not *how* to fix. Three layers enforce this: the system prompt forbids prescriptive language, the parser strips `What's needed:` blocks from the source, and a post-hoc linter scans output for "should be" / "the fix is" / etc.
- **No hallucinated file paths** — the `submit_enrichment` tool validates every `code_references[].file_path` against the actual cloned repo before accepting the agent's output. Invalid paths come back as a tool error so the agent self-corrects.
- **Idempotent re-runs** — re-uploading finds existing tickets via JQL on a stable label and skips creation, no duplicates.
- **Resumable** — every per-bug stage transition is persisted with atomic writes + rolling backups. Ctrl-C mid-run, then re-run, and you pick up where you left off.

## Pipeline overview

| Stage | Output | Notes |
|---|---|---|
| **parse** (`f2j parse`) | `state.json` of `ParsedBug` records | State-machine over markdown headings; extracts external_id, priority, hinted files, inherited module/branch/commit, fix-proposal text (stripped & preserved separately). |
| **enrich** (`f2j enrich`) | `state.json` with `EnrichedBug` per record | One LLM tool-use session per bug. Agent clones the repo (cached), searches for the referenced code, reads file slices, runs `git blame` on suspicious lines, writes a structured report via `submit_enrichment`. |
| **upload** (`f2j upload`) | Jira tickets created; `UploadResult.jira_key` written back to state | Builds Jira REST payload, resolves assignee/epic/labels via deterministic chains, applies markdown → Jira wiki conversion, posts. JQL search by `upstream:<external_id>` label provides idempotency. Subtasks (configurable) are created beneath each new parent. |

## Technology stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Pydantic v2, modern typing |
| Package mgmt | `uv` | Fast, lockfile-based, handles venv |
| Config | Pydantic-Settings + YAML layering | Project YAML, user-global YAML, env vars, CLI flags merge in documented precedence |
| Models | Pydantic v2 BaseModel | Used for both runtime data (BugRecord, EnrichedBug) and config schemas (JiraConfig, EpicEntry, SubtaskTemplate) |
| CLI | Typer + Rich | Subcommands, colored tables, structured help |
| Logging | structlog | Per-stage structured logs (run_id, stage, bug_id) |
| Markdown parsing | regex state-machine | Heading-driven; markdown-it-py is in deps but not used directly — line-by-line regex is simpler for this format |
| State persistence | atomic write + rolling .bak files + filelock | Per-bug state survives crashes, no race between parallel runs |
| Concurrency | `concurrent.futures.ThreadPoolExecutor` | Both LLM calls and Jira REST calls are I/O-bound; threads + bounded concurrency are simpler than asyncio when subprocess (git) is involved |
| HTTP/Jira | `atlassian-python-api` + tenacity for retries | Wraps requests with bearer (Server/DC) or basic (Cloud) auth dispatch; forces `Accept-Encoding: identity` to dodge corporate proxy gzip mangling |
| Git | shell out to `git` via `subprocess` (`util.proc.run`) | Clones use `--depth=1 --filter=blob:none`, blame auto-deepens shallow clones on demand |
| Repo cloning | `https-token` strategy with GIT_ASKPASS helper, plus `glab`/`ssh-default` strategies | https-token works reliably on NVIDIA's gitlab-master; `glab` strategy was attempted but doesn't authenticate (see "Hard-won insights") |
| AI providers | Anthropic SDK + OpenAI SDK | Two backends, chosen via `enrichment.provider` config: `"anthropic"` (Anthropic Messages) or `"openai_compatible"` (any OpenAI-compatible endpoint, including NVIDIA Inference Hub) |

## AI / Agent design

The enrichment stage runs **one tool-use agent session per bug**. We drive the SDK directly rather than depending on a higher-level agent framework, because the validation/retry loop, prompt-caching boundaries, and token accounting are easier to control at the primitive layer.

### Tools the agent sees

| Tool | Purpose |
|---|---|
| `clone_repo` | Ensure a shallow clone of the named repo exists. |
| `search_code` | Search the cached repo (ripgrep, with a pure-Python fallback). |
| `read_file` | Read a file or line range. Up to 32 KB per call. |
| `list_dir` | List directory contents. |
| `git_blame` | Blame a line range (auto-deepens shallow clones first). |
| `git_log_for_path` | Show commit history for a file. |
| `submit_enrichment` | **Final action.** Submits the structured report. Validates every `code_references[].file_path` against the cloned repo; returns `{ok:false, errors:[...]}` if anything's off so the agent can self-correct. |

### Two parallel agent implementations

Both speak the same toolkit and the same submit schema, but the request/response shapes differ:

- `agent.py` — Anthropic Messages API (the original path; supports prompt caching via `cache_control: ephemeral`).
- `agent_openai.py` — OpenAI Chat Completions protocol (used against NVIDIA Inference Hub via `enrichment.provider: openai_compatible`).

The system prompt (`prompts/enrichment_system.md`) is shared verbatim across both paths.

### Curated context, not raw access

The agent doesn't get arbitrary internet/Jira access. It sees:
- The bug's parsed metadata (external_id, hinted_priority, hinted_files, inherited_module).
- The bug's raw body, verbatim from the source markdown.
- A curated list of available epics (operator picks which epics the agent can choose from in `f2j.yaml`).
- The toolkit listed above, locked to the configured repo aliases.

### Deterministic safeguards on top of LLM output

LLMs make mistakes. Several layers catch those before they hit Jira:

| Risk | Safeguard |
|---|---|
| Hallucinated file paths | `submit_enrichment` validates each path against the cloned repo. |
| Fix-proposal language sneaking through | Post-hoc linter scans description for forbidden phrases (`should be`, `the fix is`, `wrap with`, etc.) and strips them. |
| Bogus components (agent invents a directory path as a "component") | Uploader filters merged components against the project's live component list. |
| Wrong epic picks (model semantic-matches "render headings" → "UI Fixes for Production" for a backend bug) | Deterministic epic priority chain at upload: `external_id_prefix_to_epic` → `module_to_epic` → LLM pick → `default_epic`. |
| Display name vs username confusion | `user_resolver` queries `/user/picker` (not `/user/search`), matches against `displayName`, falls back through `user_map.yaml`. |

## Configuration

Layered, later-wins:
1. `configs/default.yaml` (shipped with the package)
2. `~/.config/f2j/config.yaml` or `%APPDATA%\f2j\config.yaml` (per-operator)
3. `./f2j.yaml` (project-local; gitignored)
4. `--config <path>` CLI override
5. `F2J_*` env vars (nested via `__`)

Secrets always come from env (`.env` loaded via python-dotenv) — never YAML. Only the env-var *name* lives in config (e.g. `openai_compatible.api_key_env: NVIDIA_LLM_API_KEY`).

Notable config knobs:
- `jira.module_to_assignee` / `default_assignee` — assignee routing.
- `jira.module_to_epic` / `external_id_prefix_to_epic` / `available_epics` — epic routing chain.
- `jira.subtasks` — auto-created subtask templates per parent ticket.
- `jira.priority_values` — bug-list `P0..P3` → Jira's exact priority strings (case-sensitive).
- `repo_aliases` — name → `{url, auth, default_branch, token_env}`.
- `enrichment.fix_proposals: strip|reframe|keep` — what to do with prescriptive language found in output.
- `enrichment.max_budget_usd` — abort run after a given LLM spend.

## What we delivered in this session

Got the pipeline running end-to-end against NVIDIA's CENTPM Jira project + Inference Hub.

| Item | Count / value |
|---|---|
| Source bug review doc | `examples/bugs_for_dev_review_2026_05_04.md` (20 open bugs) |
| Tickets created in CENTPM | 20 (CENTPM-1291 through ~1310) |
| Subtasks per ticket | 2 (`[QA] - Testing` → Noam Lederman, `[DEV] - Development` → bug's assignee) — auto-created on the live upload pass for new parents |
| Total LLM spend | ~$10 (initial enrich + one re-enrich after epic feature; cheaper on incremental retries) |
| Epics linked | Routed deterministically by `X-OBS-*` / `X-SEC-*` / `X-PERF-*` / `CORE-MOBILE-*` / `ARB-*` prefixes and `_core` / `centarb` modules |
| Wall-clock | ~30 min for parse + enrich + upload on the full 18-bug bulk run at concurrency 4 (excluding debug iterations) |

## Hard-won insights from the session

The bring-up surfaced a handful of issues that are now fixed in the codebase. Documenting them here so future operators know what to expect.

| Issue | Fix |
|---|---|
| CLI's `--model` default was hardcoded `claude-sonnet-4-6`, which silently overrode `f2j.yaml`'s `openai_compatible.model` because `if model:` evaluated truthy on the string default. | Changed CLI default to `None` (`cli.py`); orchestrator now correctly falls through to `cfg.openai_compatible.model`. |
| `glab` doesn't work against NVIDIA's gitlab-master. `glab auth login` errors with `Set 'client_id' first`; even after seeding via `--token --stdin`, `/api/v4/user` returns 401 to glab while the same PAT works fine via direct REST. | Switched all `repo_aliases` to `auth: https-token` with a `GIT_TOKEN_GITLAB_NVIDIA` PAT in `.env`. The README pushes operators to this path. |
| Initial prompt template formatted branch/commit on one line as `{branch} @ {commit_sha}`, and the model concatenated them into a single ref `2026-05-03_Auto_Fixes@7cfb1e0` which `git checkout` rejected. | Split onto two lines: `inherited branch:` and `inherited commit:`. |
| `_checkout_ref` crashed if the parsed bug's branch had been deleted on the remote (the `--single-branch` clone refspec doesn't track other branches; the fallback fetch raised). | Made `_checkout_ref` tolerant — fetches as `<ref>:<ref>` to create a local branch ref, and silently leaves the repo on the default branch if the ref doesn't exist remotely. |
| `--dry-run` upload was persisting a synthesized `UploadResult { jira_key: "DRY-RUN" }` to state.json. Subsequent live uploads short-circuited via the idempotency check (`if record.upload and record.upload.jira_key`) and never sent anything. | Added `_UploadOutcome.dry_run: bool` flag; dry-run path returns `upload=None, dry_run=True` so state stays untouched. |
| Jira Server/DC renders descriptions as **wiki markup**, not Markdown. Raw markdown uploads get `## Headings` shown as plain text, `- bullets` ignored, and `{type_id}` parsed as a malformed macro. | Added `markdown_to_jira_wiki()` converter applied in `_compose_description`. Headings → `h2.`, bullets → `*`, fenced code → `{code:python}...{code}`, inline code → `{{...}}`. |
| Agent invents components from directory paths (`apps/centarb_backend`); configured components in `f2j.yaml` may also reference non-existent values (`CentARB`). Both crash the upload (`Component name 'X' is not valid`). | Uploader fetches the project's actual component list once, filters merged components against it. CENTPM has zero components, so the field gets omitted entirely. |
| `user_resolver`'s exact-match comparison was `h.get("name").lower() == name.lower()`, but `name` is the input display name like "Guy Keinan" while Jira's `name` field is the username `gkeinan`. Match never fired for display-name input. | Match against `displayName` first, then `name` as fallback for SSO-shortname callers. |
| `/rest/api/2/user/search` returns empty for many display-name queries on jirasw (returns nothing for `keinan`, etc.) | `search_user` now hits `/rest/api/2/user/picker` first (more lenient), with `/user/search` as backup. |
| `_fallback` returned `username=default_assignee` literally — but `default_assignee` is a display name like "Guy Keinan", not a Jira username. Jira rejected `assignee.name = "Guy Keinan"`. | Fallback now translates `default_assignee` through the `user_map.yaml` before returning it. |
| Tickets had a noisy `f2j-id:725e5d7c35e3b3bd` hex-hash label as the idempotency marker. | Switched the marker to `upstream:<external_id>` (`upstream:CORE-CHAT-026`), which is also human-readable and useful for Jira UI search. The hash form remains as a fallback for bugs with no `external_id`. |
| LLM produced multi-function code snippets with `...` ellipses inside one fenced block instead of quoting verbatim. | Tightened system prompt: "one contiguous 5–30 line range from a single file, copied verbatim. **No ellipsis, no merged functions, no editorial omissions.**" Persistent imperfection: long functions still get over-quoted; mitigated by line-range references in "Where". |
| LLM's epic picks were unreliable for ambiguous bugs (CORE-CHAT-027 picked "UI Fixes for Production" because it mentioned "rendering headings"). | Added a deterministic priority chain at upload time: external_id-prefix rule → module rule → LLM pick → default. The LLM's pick is preserved in state.json but only used when no rule matched. |
| `priority_values` initially had `P1: "P1 - Required"` (a guess from the README). The Jira priority API listed names like "P0 - Must Have", but the API only accepts lowercase "Must have" on create. | Updated to empirically-validated strings. The TROUBLESHOOTING entry warns that `/priority` listing differs from what `create_issue` accepts; always test one bug per priority class first. |

## File map

```
file-to-jira-tickets/
├── pyproject.toml                  # uv project + dependencies
├── f2j.yaml                        # project-local config (gitignored)
├── .env                            # secrets (gitignored): NVIDIA_LLM_API_KEY, JIRA_PAT, GIT_TOKEN_GITLAB_NVIDIA
├── README.md                       # operator's quick-start
├── TROUBLESHOOTING.md              # symptom → fix table
├── PROJECT_SUMMARY.md              # this file
├── configs/
│   ├── default.yaml                # shipped baseline config
│   └── user_map.yaml               # display name → Jira username (auto-cached, manually editable)
├── prompts/
│   └── enrichment_system.md        # versioned LLM system prompt
├── examples/
│   └── bugs_for_dev_review_2026_05_04.md
├── state.json                      # persistent run state (parsed → enriched → uploaded)
├── state.json.bak / .bak1 / .bak2  # rolling backups
└── src/file_to_jira/
    ├── cli.py                      # Typer CLI (parse, enrich, upload, run, inspect, jira fields/whoami, validate-config)
    ├── config.py                   # AppConfig (Pydantic-Settings) + EpicEntry + SubtaskTemplate
    ├── models/                     # ParsedBug, EnrichedBug, BugRecord, IntermediateFile
    ├── parse/                      # markdown_parser.py — regex state machine
    ├── state/                      # atomic write + filelock + rolling backups
    ├── repocache/                  # shallow-clone cache, glab/https-token/ssh-default strategies
    ├── enrich/
    │   ├── orchestrator.py         # bounded-concurrency dispatch, per-bug result commit
    │   ├── agent.py                # Anthropic agent loop + shared tool-use plumbing
    │   ├── agent_openai.py         # OpenAI-compatible agent loop
    │   ├── linter.py               # post-hoc fix-language linter
    │   ├── failure_class.py        # rate_limit / overload / context_limit / unknown taxonomy
    │   ├── cost.py                 # token → USD estimator
    │   └── tools/                  # toolkit (clone, search, read, blame), submit_enrichment closure
    ├── jira/
    │   ├── uploader.py             # payload builder + create_issue + idempotency JQL + subtasks + epic chain
    │   ├── client.py               # atlassian-python-api wrapper, retry + bearer/basic dispatch
    │   ├── field_map.py            # createmeta / per-issue field discovery
    │   └── user_resolver.py        # display-name → username via picker + cache
    └── inspect_view.py             # rich-rendered detail and summary views
```

## Operating notes

- **Run order**: `parse → enrich → upload`. Each is filterable with `--only <id>` and resumable with `--retry-failed`. `f2j run` chains all three.
- **Don't pass `--model` on the CLI** unless intentionally overriding; the default `None` lets `f2j.yaml`'s `openai_compatible.model` win.
- **Cache is small and self-managing**. Located at `%LOCALAPPDATA%\file-to-jira\repos\<name>@<hash>`. Reused on subsequent runs; safe to delete manually.
- **Updated bug list workflow**: parse to a new state file (e.g. `state-2026-05-07.json`), enrich, upload. Existing tickets are recognized via JQL and skipped (not updated). New external_ids become new tickets.
- **No ticket-update mechanism** for content changes. If a bug body changes materially in an updated MD, the existing ticket isn't re-synced — easiest workflow is to manually edit the description in Jira UI, or delete and recreate.
