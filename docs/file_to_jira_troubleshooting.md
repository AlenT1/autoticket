# Troubleshooting

## Setup

### `uv run f2j` complains about a virtual environment

PowerShell symptom:

```
warning: `VIRTUAL_ENV=C:\Users\you\AppData\Local\Python\pythoncore-3.14-64`
does not match the project environment path `.venv` ...
```

A system-wide Python install left `VIRTUAL_ENV` set in your shell. Clear it:

```powershell
$env:VIRTUAL_ENV = $null
uv run f2j --help
```

Or add to your PowerShell profile so it's set whenever you `cd` into the
project.

### Console crashes with `UnicodeEncodeError: 'charmap' codec can't encode...`

The Windows default console is CP-1252; Rich emits UTF-8. The CLI auto-reconfigures
stdout/stderr to UTF-8 at start. If you see this in a custom shell or pipeline,
set `PYTHONIOENCODING=utf-8` explicitly.

## Parsing

### Parser warning: "bug X appears outside a stage subsection"

The bug heading was found inside a `## Module:` section but no
`### Fixed in this branch` / `### Still open` H3 came before it. The parser
treats it as open and warns. Either add the missing H3 to the source, or accept
the warning.

### Encoding errors when reading the input file

The parser tries UTF-8 → CP-1252 → Latin-1 and normalizes to UTF-8 NFC. If your
file has some other encoding (rare), the parser will fall back to Latin-1
which always succeeds. The result may have garbled non-ASCII characters; fix
by re-saving the file as UTF-8 in your editor.

## Agent (enrich step)

### `AuthenticationError: Error code: 401 ... key_model_access_denied`

Full message looks like:

```
key not allowed to access model. This key can only access models=['default-models'].
Tried to access claude-sonnet-4-6
```

Two layered fixes (you'll usually need both):

1. **Don't pass `--model` on the CLI** unless you intend to override config.
   The CLI default for `f2j enrich --model` is `None`, which lets the
   orchestrator fall through to `cfg.openai_compatible.model` from `f2j.yaml`.
   If you accidentally invoked an older copy with a hardcoded default like
   `claude-sonnet-4-6`, that's what tried to be reached.
2. **Confirm `f2j.yaml`'s `openai_compatible.model` is one your NVIDIA hub
   key permits.** The error message lists which models the key has access
   to. If your key only has `default-models`, set
   `openai_compatible.model: "default-models"` (or whatever specific name
   your hub portal exposes).

### `pathspec 'BRANCH@COMMIT' did not match` during `clone_repo`

Old-prompt-template bug: the initial prompt formatted inherited branch
and commit on one line as `branch @ commit_sha`, which the model
naively concatenated into a single ref string. Fixed by splitting onto
two lines (`src/file_to_jira/enrich/agent.py`). If you still see this,
you're on a stale checkout — `git pull` and re-run.

### `EnrichmentTruncated: agent ended turn without calling submit_enrichment`

The Claude session emitted a final text response instead of calling the
`submit_enrichment` tool. The system prompt explicitly demands a final tool
call; this happens occasionally on hard cases. Re-run with `--retry-failed`,
or with `--model claude-opus-4-7` for that bug.

### `EnrichmentTruncated: hit max_turns (20) without successful submit_enrichment`

The agent kept calling other tools and never settled on a submission. Either:

- Increase `--max-turns 40` for this run.
- Inspect the bug with `f2j inspect state.json --bug <id>` and check whether
  the bug body or repo aliases are unusable (e.g. wrong `repo_alias` in the
  inherited module → agent can't clone).

### `submit_enrichment` keeps returning validation errors

Each error names a field. Common ones:

| Error                                                          | Cause                                                                                |
|----------------------------------------------------------------|--------------------------------------------------------------------------------------|
| `summary: String should have at most 255 characters`           | Agent wrote a long summary; system prompt didn't constrain enough — usually self-fixes on retry. |
| `code_references[N]: file not found in repo 'X': '...'`        | Agent emitted a path that doesn't exist — by design, agent retries with a real path. |
| `code_references[N]: unknown repo_alias 'X'`                   | Agent invented a repo alias. Add the alias to `repo_aliases` in `f2j.yaml`.          |

If retries don't converge, try `--model claude-opus-4-7` — Sonnet sometimes
gets stuck on schema details.

### `ToolError: ripgrep ('rg') is not installed`

Should not happen — the toolkit falls back to a pure-Python search. If you
hit this, check that you're on the latest version (`git pull` + `uv sync`).

## Claude routing (Anthropic API / internal proxy / sandbox)

### `401 Unauthorized` mid-batch on enrich

Either your `ANTHROPIC_API_KEY` (or proxy bearer token) is invalid, or — more
commonly inside Docker Sandbox — the OAuth token the proxy injected has
expired during a long batch. Mitigation:

- Re-run `claude` (or whatever refreshes your auth) to get a fresh token.
- Then `f2j enrich state.json --retry-failed` — the failed bugs pick up
  with the new auth.

### `Connection refused` to `api.anthropic.com` from inside `sbx`

Docker Sandbox enforces a network egress allowlist. `api.anthropic.com`
should be on the central allowlist for any team using Claude Code; if it
isn't, request via `#docker-sandbox` Slack. After approval, restart the sbx
daemon: `sbx daemon stop && sbx daemon start -d`.

### Slow `read_file` / `git clone` inside `sbx`

The sandbox docs warn that host-mounted folder I/O is slower than the
sandbox's own `/home/agent/...`. For our tool, the heaviest I/O is the
agent's `read_file` calls against cloned repos. Redirect the cache:

```yaml
# in f2j.yaml when running inside sbx
repo_cache:
  dir: "/home/agent/file-to-jira/repos"
```

### `Mount policy denied` from `sbx create`

Your project path is outside the sbx allowlist of mountable paths. The
allowlist covers things like `/p4/...`, `/sw/gpgpu/...`, `/data/...`, and
home-directory subfolders, but not arbitrary roots. Either move the repo
into an allowed path (typical: `~/projects/file-to-jira-tickets`) or
request the path on `#docker-sandbox`.

### `validate-config` shows wrong `anthropic.base_url`

Precedence is: CLI flags > env vars > project `f2j.yaml` > user-global
`~/.config/f2j/config.yaml` > shipped `configs/default.yaml`. If the value
shown isn't what you expected, run with the layer you control set explicitly:
e.g., `$env:F2J_ANTHROPIC__BASE_URL = "https://..."` to force a one-off
override.

## Repo cloning (agent tools)

### `glab auth login` fails with `Set 'client_id' first ...`

NVIDIA's GitLab uses custom OAuth and `glab` doesn't ship a NVIDIA `client_id`,
so the OAuth web-login flow can't run. This blocks the `auth: glab` strategy.

**Fix: switch the affected aliases to `auth: https-token` with a GitLab PAT.**
This is what `f2j.yaml` ships with by default:

1. Generate a PAT:
   `https://gitlab-master.nvidia.com/-/user_settings/personal_access_tokens`
   with scopes `api`, `read_repository`, `read_user`.
2. Add to `.env`: `GIT_TOKEN_GITLAB_NVIDIA=glpat-xxxxx`.
3. In `f2j.yaml`:
   ```yaml
   repo_aliases:
     _core:
       url: "https://gitlab-master.nvidia.com/wwfo-professional-services-tools/central/core.git"
       auth: "https-token"
       token_env: "GIT_TOKEN_GITLAB_NVIDIA"
   ```

### `glab auth status` returns 401 even though a token is set

Confirmed on jirasw / gitlab-master.nvidia.com: `glab auth status` (and any
operation that internally hits `/api/v4/user`) returns 401, while the same
PAT works fine when called directly:

```powershell
Invoke-RestMethod -Uri "https://gitlab-master.nvidia.com/api/v4/user" `
  -Headers @{ "PRIVATE-TOKEN" = "<your-pat>" }
```

Likely a header-format mismatch between glab and NVIDIA's GitLab. Same fix as
above — switch to `auth: https-token`.

### `RepoCacheError: git clone failed for ... fatal: certificate verify failed`

NVIDIA internal CA cert isn't trusted by your system git. Set `git_auth.ca_bundle`
to the NVIDIA CA PEM file:

```yaml
git_auth:
  ca_bundle: "C:/Path/to/nvidia-ca-bundle.pem"
```

This sets `GIT_SSL_CAINFO` for git subprocess calls. If you don't have the
PEM file handy, ask IT (NVIDIA's internal CA bundle).

### `RepoCacheError: git clone failed: ... Filename too long`

Windows MAX_PATH limit hit. The cache dir + cloned-repo path must total under
260 characters. Move the cache to a shorter path:

```yaml
repo_cache:
  dir: "C:/f2j-cache"
```

Or enable Windows long-path support in the registry (admin required).

### `git fetch --unshallow` warnings during blame/log

Expected. Shallow clones don't have enough history for blame; the toolkit
auto-deepens on demand. The fetch is idempotent — repeat calls are no-ops.

## Jira upload

### `jira.url is not configured` / `jira.project_key is not configured`

Add them to `f2j.yaml`:

```yaml
jira:
  url: "https://jirasw.nvidia.com"
  project_key: "BUG"
```

### `JIRA_PAT env var is not set`

PAT must be in `.env`. Create one at:
https://jirasw.nvidia.com/plugins/servlet/de.resolution.apitokenauth/admin

NVIDIA SSO: create the PAT directly in your Jira profile (the `apitokenauth`
admin endpoint requires SSO login first).

### `createmeta lookup failed: ... 401 Unauthorized`

PAT is invalid or expired. Test with `f2j jira whoami`. Refresh the token at
the URL above.

### `field_map references unknown fields: severity=customfield_99999`

The custom field ID in your `f2j.yaml` doesn't exist on the project. Re-run
`f2j jira fields --project BUG` and copy the correct IDs.

### `priority: Cannot set priority of issue to "Highest"` / `Priority name 'P1 - Required' is not valid`

The configured `priority_values` don't match Jira's actual priority names.
Names are exact-match and case-sensitive, and CENTPM uses inconsistent casing
(`Must Have` capital H, but `Should have` lowercase h). Discover the truth:

```powershell
Invoke-RestMethod -Uri "https://jirasw.nvidia.com/rest/api/2/priority" `
  -Headers @{ "Authorization" = "Bearer $($env:JIRA_PAT)" } |
  Select-Object name | Format-Table -AutoSize
```

Then update `jira.priority_values` in `f2j.yaml` to use the exact strings.

### `Component name 'X' is not valid`

Either the agent invented a component name (e.g. it copied a directory path
like `apps/centarb_backend` thinking it was a component), or `module_to_component`
in `f2j.yaml` references a component that doesn't exist on the project. The
uploader filters merged components against the project's live component list
and drops invalid ones, BUT only if `f2j.yaml`'s `module_to_component` is empty;
explicitly-configured names that don't exist will still be sent and rejected.

Discover real components:

```powershell
Invoke-RestMethod -Uri "https://jirasw.nvidia.com/rest/api/2/project/CENTPM/components" `
  -Headers @{ "Authorization" = "Bearer $($env:JIRA_PAT)" } |
  Select-Object name | Format-Table -AutoSize
```

If the project has zero components (CENTPM does, as of 2026-05), set
`module_to_component: {}` in `f2j.yaml` and the field gets omitted entirely.

### `User 'Display Name' does not exist`

The user_resolver couldn't translate a display name to a Jira username, AND
the fallback chain returned the literal display name (which Jira rejects
because `assignee.name` must be a username, not a display name).

Two interlocking fixes are already in the code:

1. The resolver now tries `/rest/api/2/user/picker?query=<name>` first
   (more lenient than `/user/search` which often returns empty for
   display-name queries on jirasw).
2. The fallback for `unknown_assignee_policy: "default"` translates the
   `default_assignee` through `configs/user_map.yaml` before sending.

If you still see this, the picker also returned empty (rare). Look up the
username manually:

```powershell
Invoke-RestMethod -Uri "https://jirasw.nvidia.com/rest/api/2/user/picker?query=<Display%20Name>" `
  -Headers @{ "Authorization" = "Bearer $($env:JIRA_PAT)" } |
  Select-Object -ExpandProperty users |
  Select-Object name, displayName, emailAddress
```

Then add to `configs/user_map.yaml`:
```yaml
"Display Name": <username>
```

### `assignee: User 'Jane Doe' does not exist`

The assignee name in the bug doesn't resolve to a Jira username. Three
options:

1. Add an entry to `configs/user_map.yaml`:
   ```yaml
   "Jane Doe": jdoe
   ```
2. Set `jira.unknown_assignee_policy: "default"` and `jira.default_assignee:
   "triage"` in `f2j.yaml` (already the default).
3. Run with `--dry-run` first; the resolver will search Jira and cache the
   match into `user_map.yaml` for the live run.

## State file

### `StateFileCorruptError: ... invalid JSON at line N, column M`

Someone (or a crashed process) left the file in an inconsistent state.
Restore from a rolling backup:

```powershell
copy state.json.bak state.json
```

Backups are kept at `state.json.bak`, `state.json.bak1`, `state.json.bak2`.

### `StateFileLockedError: state file is locked by another process`

Another `f2j` invocation holds the lock. If you're sure none is running,
delete the lock file:

```powershell
del state.json.lock
```
