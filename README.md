# file-to-jira-tickets

Turn a markdown bug list into Jira tickets, enriched with code context by an AI agent.

```
input.md  →  parse  →  state.json  →  enrich (LLM)  →  upload (Jira)
```


1. **Parse** a markdown file containing bug entries into structured records.
2. **Enrich** each bug by spawning an LLM tool-use agent that browses the
   referenced source repo, locates the offending code, copies snippets, runs
   `git blame`, and writes a factual report (no fix proposals — that's
   enforced).
3. **Upload** the enriched bugs to Jira as real tickets, idempotent and
   resumable.

---

## Quick start (current operator: Sharon)

If you're picking this up mid-stream and just want to run it, jump to
[Run a single bug end-to-end](#run-a-single-bug-end-to-end). The configuration
below is already filled in for the NVIDIA `CENTPM` project.

---

## Install

```powershell
uv sync
```

That's it. Python 3.11 + all dependencies are managed by `uv` into a local
`.venv`. The package is self-contained — only **git** is required as an
external runtime tool.

### Optional accelerators

These are not required but make things faster:

| Tool      | Why                                                              | Install                                      |
|-----------|------------------------------------------------------------------|----------------------------------------------|
| ripgrep   | 50–100× faster code search than the Python fallback.             | `winget install BurntSushi.ripgrep.MSVC`     |
| jira CLI  | Cross-check for `validate-config`. Optional.                     | https://github.com/ankitpokhrel/jira-cli/releases |

The package works without these — `search_code` falls back to a pure-Python
implementation; `validate-config` skips CLI cross-checks if they're not on PATH.

> **Why no `glab` recommendation?** We attempted the `auth: glab` strategy
> against `gitlab-master.nvidia.com` and hit OAuth client-id and `/api/v4/user`
> issues that aren't easily resolvable. `auth: https-token` with a GitLab PAT
> works reliably. See "Repo auth — NVIDIA-specific gotcha" below.

---

## What's already configured

### `f2j.yaml` (project config — gitignored)

```yaml
enrichment:
  provider: "openai_compatible"

openai_compatible:
  base_url_env: "NVIDIA_BASE_URL"     # read URL from .env
  api_key_env:  "NVIDIA_LLM_API_KEY"  # read key from .env
  model: "openai/openai/gpt-5.4-mini"

jira:
  url: "https://jirasw.nvidia.com"
  project_key: "CENTPM"               # ← from https://jirasw.nvidia.com/browse/CENTPM-1231
  issue_type: "Bug"
  module_to_assignee:
    _core: "Yair Sadan"               # _core bugs → Yair
  default_assignee: "Guy Keinan"      # everything else → Guy

  # CENTPM has zero Jira components (verified via /rest/api/2/project/CENTPM/components),
  # so we send no `components` field. The uploader filters anything the agent
  # invents against the live project component list and drops invalid values.
  module_to_component: {}

  # Priority names exactly as CENTPM accepts them. Note: /rest/api/2/priority
  # may show different casing (e.g. "Must Have") but uploads only succeed
  # with the values below — verified empirically. Always test on one bug
  # of each priority class before bulk-running.
  priority_values:
    P0: "P0 - Must have"
    P1: "P1 - Should have"
    P2: "P2 - Nice to have"
    P3: "P3 - Low"

  # Epic Link routing — see "Epic Link routing" section below for full details.
  epic_link_field: "customfield_10005"
  default_epic: "CENTPM-1184"
  external_id_prefix_to_epic:
    "X-OBS-": "CENTPM-1179"
    "X-SEC-": "CENTPM-1162"
    "X-PERF-": "CENTPM-1184"
    "CORE-MOBILE-": "CENTPM-1235"
    "ARB-": "CENTPM-1197"
  module_to_epic:
    _core: "CENTPM-1184"
    vibe_coding/centarb: "CENTPM-1197"
```

### `.env` (secrets — gitignored)

```
NVIDIA_BASE_URL=https://inference-api.nvidia.com/v1/
NVIDIA_LLM_API_KEY=sk-...                # from https://<your-NVIDIA-LLM-portal>/keys
JIRA_PAT=<your jira token>               # from https://jirasw.nvidia.com/plugins/servlet/de.resolution.apitokenauth/admin
```

### `configs/user_map.yaml` (display name → Jira username)

Starts empty — the resolver auto-populates on first use by hitting
`/rest/api/2/user/picker?query=<display name>` (we found `/user/search`
returns empty for many display-name queries on jirasw, so the resolver
falls through to picker which is more lenient). Successful lookups cache
back into this file. Manual seed example:

```yaml
"Yair Sadan": ysadan
"Guy Keinan": gkeinan
```

### Repo auth — NVIDIA-specific gotcha

The default `auth: glab` strategy DOES NOT WORK on jirasw / gitlab-master.nvidia.com
out of the box, because:

1. `glab auth login` errors with `Set 'client_id' first ...` — NVIDIA's
   GitLab uses custom OAuth and `glab` doesn't ship a NVIDIA client_id.
2. Even after seeding glab with a Personal Access Token via
   `glab auth login --token --stdin`, `/api/v4/user` returns 401 to glab
   (while the same token works fine via direct REST).

**Working path: `auth: https-token` with a GitLab PAT.** This is what
`f2j.yaml` ships with for every alias. To set up:

1. Create a PAT at
   https://gitlab-master.nvidia.com/-/user_settings/personal_access_tokens
   with scopes `api`, `read_repository`, `read_user`.
2. Add to `.env`:
   ```
   GIT_TOKEN_GITLAB_NVIDIA=glpat-xxxxx
   ```
3. Each `repo_aliases.<alias>` block uses `auth: https-token` and
   `token_env: GIT_TOKEN_GITLAB_NVIDIA`. Already set — no edits needed.

### What's still TODO in `f2j.yaml`

- `jira.field_map.bug.severity` and `jira.field_map.bug.found_in_build` —
  fill in **after** running `f2j jira fields --project CENTPM` (step 2 below).
  Currently CENTPM seems to need only the standard fields (summary, description,
  priority); add custom fields here if your project requires more.

### Epic Link routing

Each Jira ticket can be linked to an Epic via `customfield_10005`. f2j picks
the Epic Link key per bug using a **deterministic priority chain at upload
time** (no enrichment required). Order of resolution:

1. **`external_id_prefix_to_epic`** — most specific. Longest matching prefix
   wins. Example mappings in `f2j.yaml`:
   - `X-OBS-` → `CENTPM-1179` (Monitoring & Observability)
   - `X-SEC-` → `CENTPM-1162` (Security Hardening)
   - `CORE-MOBILE-` → `CENTPM-1235` (UI Fixes for Production)
2. **`module_to_epic`** — fallback by repo alias. Example:
   - `_core` → `CENTPM-1184` (Testing & Quality)
   - `vibe_coding/centarb` → `CENTPM-1197` (CentArb & CentProjects Production Readiness)
3. **`enriched.epic_key`** — the LLM's pick from `available_epics`, used only
   when no rule above matched. The agent sees `available_epics` in its prompt
   and emits an `epic_key` in `submit_enrichment`. The uploader honors it
   only if it's in the curated list.
4. **`default_epic`** — final safety net. Defaults to `CENTPM-1184`.

To preview which epic *will* be sent for a given bug (vs which one the LLM
picked), use `f2j inspect state.json --bug <id>`. The output shows two
lines: `Epic (LLM pick)` (whatever the agent suggested at enrich time) and
`Epic (will use)` (the deterministic-chain result that will be uploaded).

To discover available epics in a Jira project:

```powershell
Invoke-RestMethod -Uri "https://jirasw.nvidia.com/rest/api/2/search?jql=project%3DCENTPM+AND+issuetype%3DEpic&fields=summary&maxResults=50" `
  -Headers @{ "Authorization" = "Bearer $($env:JIRA_PAT)" } |
  Select-Object -ExpandProperty issues |
  ForEach-Object { [pscustomobject]@{ key = $_.key; summary = $_.fields.summary } } |
  Format-Table -AutoSize
```

---

## The three commands you'll use most

```powershell
# Always clear the inherited VIRTUAL_ENV first (Windows-only quirk)
$env:VIRTUAL_ENV = $null

uv run f2j parse <input.md>            # → state.json
uv run f2j enrich <state.json>         # → enriches in place via NVIDIA hub
uv run f2j upload <state.json>         # → creates tickets in Jira
```

Each command is **resumable** — re-running picks up where the last one stopped.
Each is **filterable** with `--only <bug_id_or_external_id>` for single-bug runs.

---

## Run a single bug end-to-end

This is the recommended first run. Tests the entire pipeline against ONE bug
(`ARB-AUTH-001` — clean real-product-gap with a single repo and a `What's needed:`
field that exercises the fix-language stripper).

### Step 0 — clear the inherited venv

Once per shell session:

```powershell
$env:VIRTUAL_ENV = $null
```

If you skip this, `uv run` will complain about the Python 3.14 system install
overriding the project's `.venv` (Python 3.11). Symptom:
`error: Project virtual environment directory ... cannot be used`.

### Step 1 — confirm Jira auth works

```powershell
uv run f2j jira whoami
```

**Expected:** `ok: <your display name> (<your sso name>)`

If you get `JIRA_PAT env var is not set`, check `.env`. If you get
`401 Unauthorized`, regenerate the PAT.

### Step 2 — discover Jira custom-field IDs for CENTPM

```powershell
uv run f2j jira fields --project CENTPM --issue-type Bug
```

This prints a table of every field on `CENTPM` Bug issues with their
`customfield_NNNNN` IDs. Copy the IDs you care about into `f2j.yaml` under
`jira.field_map.bug`. At minimum:

```yaml
jira:
  field_map:
    bug:
      summary: "summary"               # already correct
      description: "description"       # already correct
      priority: "priority"             # already correct
      # Add more if your project requires them, e.g.:
      # severity: "customfield_12345"
      # found_in_build: "customfield_12347"
```

If your project doesn't have extra required custom fields, the three lines
above are enough.

### Step 3 — sanity-check the readiness table

```powershell
uv run f2j validate-config
```

**Expected at the bottom:**

```
Phase 1 readiness
  ready  parse
  ready  enrich
  ready  upload
```

If `enrich` says "not ready", check `NVIDIA_LLM_API_KEY` is set in `.env`.
If `upload` says "not ready", read the `(needs ...)` hint — usually a missing
field in `f2j.yaml`.

### Step 4 — parse the bug list

```powershell
uv run f2j parse examples\Bugs_For_Dev_Review_2026-05-03.md --out state.json --force
```

**Expected:** `parsed 20 bug entries / selected 20 (open=20) / wrote state.json`

The 20 open bugs from `_core` (14), `vibe_coding/centarb` (2), and Cross-cutting
(4) are now in `state.json`. The 250 closed bug references (in "Fixed in this
branch" sections) are skipped by default.

### Step 5 — enrich ONE bug

This is the moment of truth — first call to NVIDIA's Inference Hub.

```powershell
uv run f2j enrich state.json --only ARB-AUTH-001 --concurrency 1
```

**Expected:** `ok    4b7b6e... ARB-AUTH-001` plus a cost summary at the end.

Possible failures and fixes:

| Symptom                                               | Fix |
|-------------------------------------------------------|-----|
| `key_model_access_denied` / `key not allowed to access model. This key can only access models=['default-models']` | Your NVIDIA hub key is restricted. Either request access to the configured model, or change `f2j.yaml` → `openai_compatible.model:` to one of the models the key permits. |
| `Model not found` or generic 4xx error                | Edit `f2j.yaml` → `openai_compatible.model: "openai/gpt-5.4-mini"` (drop one segment), retry. |
| Agent times out / never calls `submit_enrichment`     | Model probably doesn't support tool calling, or the bug is cross-cutting with no `inherited_module.repo_alias` (agent wanders without a starting repo). For the latter, edit state.json to set `inherited_module.repo_alias` to the most likely repo, then retry. |
| `401 Unauthorized`                                    | Regenerate `NVIDIA_LLM_API_KEY` from the portal's Keys tab. |
| `pathspec 'BRANCH@COMMIT' did not match` during clone | Old prompt-template bug; resolved by splitting `branch` and `commit` onto separate lines (`src/file_to_jira/enrich/agent.py`). If you see this, you're on a stale copy — `git pull`. |
| `truncated: agent ended turn without calling submit_enrichment` or `hit max_turns (20)` | Re-run with `--retry-failed`, optionally `--max-turns 30` for cross-cutting bugs that need to discover the right repo. |

### Step 6 — inspect what the agent produced

```powershell
uv run f2j inspect state.json --bug ARB-AUTH-001 --show-stripped
```

Look for:
- **Summary** — concise, ≤255 chars, descriptive.
- **Description** — should NOT contain phrases like "should be", "wrap with",
  "the fix is", "we should". The post-hoc linter strips these. If anything
  prescriptive snuck through, the linter will report it as "fix-proposal lines stripped".
- **Code references** — every `file_path` must exist in the cloned repo
  (validated by `submit_enrichment` before the agent's output is accepted).
- **Removed fix-proposal text** (under `--show-stripped`) — should contain the
  `What's needed:` block from the source markdown that we explicitly stripped.

### Step 7 — dry-run upload

```powershell
uv run f2j upload state.json --only ARB-AUTH-001 --dry-run
```

This prints the **exact REST payload** that would be POSTed to Jira, but
doesn't actually create the ticket. Verify:
- `assignee.name` — should be Guy Keinan's resolved SSO short name (since
  ARB-AUTH-001's module is `vibe_coding/centarb`, not `_core`).
- `priority.name` — `Highest` (P0).
- `components` — `["CentARB"]`.
- `labels` — includes `upstream:ARB-AUTH-001` (the idempotency marker —
  human-readable, drives the JQL "already-uploaded?" check), `from-md`,
  `auto-created`, plus the parsed status labels (`real-product-gap`).
  The earlier `f2j-id:<hex-hash>` label was removed; idempotency now
  uses the existing `upstream:<external_id>` label, which is cleaner and
  more useful for searching tickets in Jira.
- `summary` — what the agent wrote.
- `description` — clean, no fix proposals.

If the assignee shows as `null` or empty, open `configs/user_map.yaml` and
add the manual mapping (see [Step 9](#step-9--what-to-do-if-jira-cant-resolve-an-assignee)).

### Step 8 — live upload

```powershell
uv run f2j upload state.json --only ARB-AUTH-001
```

**Expected:** `ok    <bug_id>  -> CENTPM-NNNN`

Open `https://jirasw.nvidia.com/browse/CENTPM-NNNN` to eyeball the result.
The state.json now records `upload.jira_key = "CENTPM-NNNN"` so a re-run won't
duplicate the ticket (idempotency by label).

### Step 9 — what to do if Jira can't resolve an assignee

If the dry-run shows `assignee` is missing or wrong:

1. Open `configs/user_map.yaml`.
2. Add explicit mappings:
   ```yaml
   "Yair Sadan": <his real Jira short name>
   "Guy Keinan": <his real Jira short name>
   ```
3. Re-run the dry-run.

You can find someone's Jira short name by looking at any ticket they've worked
on — their name appears as a hyperlink, and the URL contains the short name:
`https://jirasw.nvidia.com/secure/ViewProfile.jspa?name=<shortname>`.

---

## Run all 20 bugs

Once the single-bug walkthrough works, drop the `--only` flag:

```powershell
$env:VIRTUAL_ENV = $null

uv run f2j parse examples\Bugs_For_Dev_Review_2026-05-03.md --out state.json --force
uv run f2j enrich state.json --concurrency 4              # 4 parallel agent sessions
uv run f2j upload state.json --concurrency 4              # 4 parallel uploads (dry-run first if you're paranoid)
```

**Expected costs:** ~$0.05–$0.30 per bug on Sonnet-class models, so $1–6 for
all 20. The `enrichment.max_budget_usd` knob in `f2j.yaml` lets you cap this.

**Expected wall-clock:** 2–5 minutes for the full pipeline at concurrency=4.

### One-shot run

If you'd rather chain everything:

```powershell
uv run f2j run examples\Bugs_For_Dev_Review_2026-05-03.md --concurrency 4 --force
```

Equivalent to `parse → enrich → upload`. Use `--no-upload` to skip the live
upload step if you want to inspect first; `--dry-run-upload` to print payloads
without creating tickets.

---

## Useful inspection commands

```powershell
uv run f2j inspect state.json                          # summary table of all bugs
uv run f2j inspect state.json --stage failed           # only failed bugs
uv run f2j inspect state.json --bug CORE-CHAT-031      # one bug, full detail
uv run f2j inspect state.json --bug ARB-AUTH-001 --show-stripped   # + the stripped fix-proposal text
```

---

## Iteration loop — what to do when something looks wrong

The pipeline is **resumable**, so iterate fearlessly:

| Problem | Fix |
|---|---|
| Bad description on one bug | Re-run `f2j enrich state.json --only <bug_id> --retry-failed` after editing the system prompt at [prompts/enrichment_system.md](prompts/enrichment_system.md). |
| Wrong Jira fields | Re-run `f2j jira fields --project CENTPM`, edit `f2j.yaml`, re-run `f2j upload --dry-run`. No re-enrich needed. |
| Wrong priority mapping (e.g. project doesn't have "Highest") | Look at what `f2j jira fields` reports under Priority's `allowedValues`, update `jira.priority_values` in `f2j.yaml`. |
| Need to retry only failed bugs | `f2j enrich state.json --retry-failed` (or `f2j upload state.json --retry-failed`). |
| Crashed mid-run | Just re-run the same command. State persists per bug. |

---

## Hard guarantees

- **No fix proposals** in the Jira description. Defended at three layers:
  the system prompt explicitly forbids them; the parser strips `What's needed:`
  and `What was changed:` fields from the source; a post-hoc linter scans the
  enriched description for prescriptive phrases (`should be`, `wrap with`,
  `the fix is`, …) and removes them.
- **No hallucinated file paths.** The `submit_enrichment` tool validates every
  `code_references[].file_path` against the actual cloned repo before
  accepting the agent's output. Invalid refs are returned as a tool error so
  the agent self-corrects.
- **Idempotent upload.** Each ticket gets an `upstream:<external_id>` label
  (e.g. `upstream:CORE-CHAT-026`). Before creating, the uploader does a JQL
  search by that label; if found, it records the existing key without creating
  a duplicate. Safe to re-run. The label's external_id form means it's also
  a useful trail in Jira's UI search.
- **Markdown → Jira wiki conversion.** Descriptions are written by the agent
  as Markdown, then converted to Jira Server/DC wiki markup at upload time
  (`h2.` headings, `*` bullets, `{code:python}...{code}` fences, `{{...}}`
  monospace). This avoids the `{type_id}`-in-path macro mangling that hits
  raw-Markdown uploads to Jira DC.
- **Component & epic guarding.** Both fields are validated/routed at upload
  time: components are filtered against the project's live component list
  (so the agent inventing `apps/centarb_backend` as a component gets dropped
  silently); epics route via a deterministic chain (external_id prefix →
  module → LLM pick → default), overriding any wrong LLM picks.
- **Resumable.** State is persisted after every per-bug stage transition with
  atomic writes + rolling backups. A `Ctrl-C` mid-run leaves a `state.json`
  that picks up where it left off.

---

## Per-operator portability

The project repo (`f2j.yaml`, source code, prompts) is shared. Each operator
keeps their **own** `.env` with their own credentials. To onboard a new
operator (e.g. someone other than Sharon):

1. They `git clone` the repo.
2. They `uv sync`.
3. They copy `.env.example` → `.env` and fill in *their* `NVIDIA_LLM_API_KEY` and `JIRA_PAT`.
4. (Optional) They override `jira.default_assignee` and `module_to_assignee` in
   `~/.config/f2j/config.yaml` if their team's routing differs from Sharon's.

The shared `f2j.yaml` is a starting point, not a constraint.

---

## Library use (for embedding into another project)

```python
import file_to_jira as f2j

cfg = f2j.load_config()
# Parse:
f2j.parse_markdown(decoded_text, source_sha256=sha)
# Enrich + upload:
f2j.run_enrich(state_file=path, cfg=cfg)
f2j.upload_state(state_file=path, cfg=cfg)
```

See [src/file_to_jira/__init__.py](src/file_to_jira/__init__.py) for the
public API surface.

---

## Tests

```powershell
$env:VIRTUAL_ENV = $null
uv sync --extra dev
uv run pytest -q
```

199 tests, ~14 s on a recent laptop. Coverage:

- Parser tests against the real sample `examples/Bugs_For_Dev_Review_2026-05-03.md`.
- Anthropic agent loop with a scripted fake Anthropic client (back-compat path).
- OpenAI-compatible agent loop with a scripted fake OpenAI client (the path
  this operator uses).
- Jira upload with a fake atlassian client.
- Toolkit tests (clone/search/read/blame) against a real local fixture git
  repo using `file://` URLs.
- Failure classification + cost estimation + linter pattern matching.

---

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for symptom-driven fixes,
covering: setup glitches (VIRTUAL_ENV inheritance, console encoding),
parser warnings, agent failures, Jira connection issues (CA cert, PAT
expiry, custom field surprises), repo-clone issues (NVIDIA internal CA,
Windows MAX_PATH), and state-file corruption recovery.
