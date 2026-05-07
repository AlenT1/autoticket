# User Guide

Two tools that turn human-written documents into Jira tickets, with a manual approval step before anything is written.

| Tool | Input | Output |
|---|---|---|
| **f2j** | A markdown list of bugs | Jira **Bug** tickets, one per item |
| **jira-task-agent** | Planning docs from Google Drive, a local folder, or both | Jira **Epics + Tasks**, structured per workstream |

Both default to dry-run. Nothing reaches Jira unless you pass `--apply` AND approve the plan.

---

## First-time setup (5 minutes)

```sh
git clone https://github.com/AlenT1/autoticket.git
cd autoticket
uv sync --extra dev

uv run jira-task-agent init      # copies .env.example → .env, creates data/local_files/
# edit .env: fill in JIRA_HOST, JIRA_PROJECT_KEY, JIRA_TOKEN, NVIDIA_API_KEY
#            (DRIVE_FOLDER_ID + Google OAuth values only if using Drive)

uv run jira-task-agent doctor    # confirms every required key resolves
```

If `doctor` reports `Result: all checks passed.` you're ready to run. If it reports errors, the message tells you exactly which key is missing and how to fix it.

`init` is idempotent — running it again won't overwrite an existing `.env` or recreate dirs. `f2j init` and `f2j doctor` do the same thing for the bug-tickets tool (both tools share `.env`).

---

## Tool 1 — `f2j`: bugs from markdown

Use it when you have a triage list (QA, support, customer feedback) and want each bug as its own Jira ticket with code context already filled in.

### What you give it

A markdown file where each bug is a top-level heading:

```markdown
## Login button doesn't work on Safari
Steps to reproduce: ...
Expected: ...
Actual: ...

## Profile page 500s when avatar missing
...
```

Each `##` becomes one Bug ticket.

### What you get

For every bug, an AI agent does the homework:
- Clones the relevant repo
- Reads code, runs `git blame`, identifies probable root-cause file
- Drafts a ticket body with reproduction steps, suspected location, and supporting code references
- Hands you the draft to approve

### How to run

```sh
f2j parse my-bugs.md            # show what it parsed (no LLM cost)
f2j enrich my-bugs.md            # add code context (LLM cost here)
f2j upload my-bugs.md --verify   # create in Jira after you approve
```

### What to check at the verify step

- The "probable file" reference is a real file in your repo
- Reproduction steps came from your input, not invented
- The Jira project + component look right

---

## Tool 2 — `jira-task-agent`: epics + tasks from planning docs

Use it when you're planning a release or vertical and want the plan reflected in Jira automatically.

### What you give it

Planning docs, from one or both of these sources:

- **Google Drive folder** — drop markdown or Google Docs into the configured Drive folder. The agent picks up everything in there.
- **Local folder** — the `data/local_files/` directory in the repo. Drop markdown files in here for quick local iteration without uploading anything to Drive.

By default the agent reads both. You can scope a run with `--source gdrive` (Drive only), `--source local` (local only — also skips Google auth entirely), or `--source both` (default).

The agent reads everything and decides what each file is:

- **single-epic** — one feature plan → produces 1 Epic + N Tasks
- **multi-epic** — release plan bundling many workstreams → produces N Epics, each with their child Tasks
- **root** — background context (architecture overviews, changelogs) — used to enrich descriptions but doesn't become tickets

Conventions in your doc that the agent honors:
- An "Owner:" line at the top sets the epic's assignee
- An "Owner" column in a task table sets per-task assignees
- Tasks without an explicit owner inherit the doc's Owner
- Identifier-like markers (deadlines, ticket IDs, version codes, quantitative targets like `>60s` or `50 blocks per message`) are copied byte-for-byte from your doc into the ticket body

### What you get

Every Task ticket has a fixed, reviewable shape:

- **Comprehensive context** — what the work is and why
- **Goal** — the observable end-state
- **Implementation steps** — numbered, each with a file path and a "Done when" criterion
- **Definition of Done** — a task-specific shipping checklist
- **Source** — which doc this came from, and who last edited it

The Epic ticket carries the same shape minus implementation steps.

### How to run

```sh
jira-task-agent run                                            # dry-run (read-only)
jira-task-agent run --apply --verify                           # full run with manual approval
jira-task-agent run --apply --verify --only my-plan.md         # process one file
jira-task-agent run --apply --verify --target-epic <KEY-NNN>   # park created items under a test epic
jira-task-agent run --apply --verify --source local            # local folder only, no Drive
jira-task-agent run --apply --verify --source gdrive           # Drive folder only, skip local
```

### What you see at the verify step

A markdown file at `data/run_plan.md` listing every Jira action the agent wants to take, numbered. You enter the numbers you approve (or `all`, or `cancel`). Only approved actions are written to Jira.

---

## Safety rails (both tools)

| Rail | What it does |
|---|---|
| **Default dry-run** | Without `--apply`, no Jira writes happen — you can experiment freely |
| **`--verify` gate** | Renders the full plan as markdown and waits for your action-by-action approval |
| **`--target-epic <KEY>`** | Routes everything created in this run under a single Jira epic you nominate (typically a standing test epic) — easy to clean up |
| **Diff-aware re-runs** | If you re-run after editing the source doc, only the changed bullets re-process. Existing Jira tickets get updates with a changelog comment, not duplicates |
| **Identifier preservation** | Deadlines, ticket IDs, version tags, quantitative targets are copied byte-for-byte. Nothing operationally meaningful is paraphrased away |

---

## Re-running on the same content

The agent caches every decision it makes. A second run on unchanged content produces zero Jira writes. When the source doc changes, only the changed bullets re-process — existing tickets are updated with a changelog comment, and unchanged tickets are left alone. The source doc is the source of truth: if a human edited a ticket body in Jira and the doc was later edited too, the regenerated body will match the doc again, and the prior text is preserved in Jira's edit history.

## When NOT to use it

- The source doc has wrong file paths or made-up details — the agent will copy them. Fix the doc first.
- You haven't read the plan. Never approve `all` blindly on a first run for a new doc.
