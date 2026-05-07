# Cross-platform plan — Linux / macOS / Windows

Audit + remediation log for making this codebase cross-platform clean. The
project is operated on Windows (Sharon's primary box) and Linux (CI / cron),
and should also work on macOS without surprise.

## Status — at-a-glance

| Item | Status |
|---|---|
| `_shared/process_lock.py` Windows fallback (`msvcrt.locking`) | ✅ Landed in `93d8a0c "Windows Compatability fix"` |
| `.env.example` inline-comment trap | ✅ Fixed in this commit (comments moved above each variable; warning at top of file) |
| Skip POSIX-specific PID assertions in `test_process_lock.py` on Windows | ✅ Fixed in this commit |
| `encoding="utf-8"` on 5 token-file `read_text` / `write_text` sites | ✅ Fixed in this commit |
| Add Windows + macOS to CI matrix | 🔲 **Not yet** — see [Future work](#future-work) |

Offline test suite after these fixes (Windows): **530 passed, 2 skipped
(POSIX-only PID diagnostics), 24 deselected (live)**. Zero failures.

## Why this audit happened

While running `jira-task-agent` on Windows for the first time, two latent
bugs surfaced back-to-back:

1. **`process_lock.py` was POSIX-only** — `import fcntl` at module top
   level, no Windows fallback. Every `jira-task-agent run` on Windows died
   with `ModuleNotFoundError: No module named 'fcntl'`.
2. **`.env.example` inline comments corrupted empty values** — operators
   copying the template verbatim got `JIRA_CA_BUNDLE="# optional, custom
   CA cert path"` instead of an empty string, which then surfaced as
   `Could not find a suitable TLS CA certificate bundle, invalid path: #
   optional, custom CA cert path` on the first Jira request.

Neither would have shipped if the offline test matrix included Windows.
The audit below was triggered to find similar latent issues *before* the
next operator hits them.

## Audit findings

### Hard issues (fixed in this commit)

#### `tests/_shared/test_process_lock.py` — 2 POSIX-only PID assertions

`test_acquire_writes_pid` and `test_lock_message_includes_holder_pid`
both assume the lock file contains the holder PID. The Windows lock path
in [src/_shared/process_lock.py](../src/_shared/process_lock.py)
deliberately keeps a single sentinel byte at offset 0 so the
`msvcrt.locking` byte-range lock stays valid for the run's lifetime —
writing the PID into the file would invalidate that lock. This is a
documented trade-off; the lock semantics (busy-detection + release) work
correctly on Windows, only the PID-in-error diagnostic is missing.

**Fix:** mark both tests with `@pytest.mark.skipif(sys.platform ==
"win32", ...)` and a clear reason string. Lock semantics are still
covered by the other 4 tests in the file — `creates_parent_dirs`,
`release_after_context_exit`, `second_acquirer_raises_run_lock_busy`,
`second_acquirer_in_separate_process_raises` — all of which pass on
Windows.

#### 5 token-file reads / writes missing `encoding="utf-8"`

| File:line | Operation |
|---|---|
| [src/_shared/io/sinks/jira/client.py:45](../src/_shared/io/sinks/jira/client.py#L45) | `role_file.read_text()` (autodev token) |
| [src/_shared/io/sinks/jira/client.py:50](../src/_shared/io/sinks/jira/client.py#L50) | `legacy.read_text()` (autodev token, legacy path) |
| [src/_shared/config/settings.py:144](../src/_shared/config/settings.py#L144) | same role-file read |
| [src/_shared/config/settings.py:149](../src/_shared/config/settings.py#L149) | same legacy read |
| [src/_shared/io/sources/gdrive.py:89](../src/_shared/io/sources/gdrive.py#L89) | `token_path.write_text(creds.to_json())` (Google OAuth token) |

**Why it matters:** Windows `open()` / `read_text()` defaults to
`locale.getpreferredencoding()` — typically `cp1252`, not UTF-8. Tokens
are ASCII today, so unlikely to break, but a future operator with a
non-ASCII home-directory path (or a token rotated through a tool that
adds a UTF-8 BOM) would silently corrupt the value. The rest of the
codebase consistently passes `encoding="utf-8"` (22+ sites verified by
audit grep); these 5 were the only stragglers.

**Fix:** added `encoding="utf-8"` to all 5 sites. No semantic change for
ASCII tokens; closes the latent inconsistency.

### Audit clean (no fixes needed)

These categories were checked exhaustively and had no cross-platform
issues. Listed here so a future auditor can see the boundaries of the
sweep:

| Area | Result |
|---|---|
| **POSIX-only stdlib imports** (`fcntl`, `pwd`, `grp`, `termios`, `resource`, `os.fork`, `os.geteuid`) | Only `fcntl` in [src/_shared/process_lock.py:34](../src/_shared/process_lock.py#L34), properly gated by `if not _IS_WINDOWS`. No others anywhere in `src/`. |
| **`open()` / `read_text` / `write_text`** for content (non-token) files | All 22+ sites pass `encoding="utf-8"` explicitly. Binary-mode opens (`"rb"`, `"wb"`) — fine, encoding doesn't apply. |
| **Atomic writes** (tempfile + rename) | [`jira_task_agent/cache.py:245-254`](../src/jira_task_agent/cache.py#L245) and [`file_to_jira/state/store.py:112-116`](../src/file_to_jira/state/store.py#L112) both use `tempfile.NamedTemporaryFile` (or `with` block for `tmp_path.open`) followed by `os.replace`. The `with` block guarantees the file is closed before `os.replace`, which Windows requires (POSIX doesn't care). Cross-platform safe. |
| **Path handling** | `Path.home()` everywhere (cross-platform). Windows-specific `APPDATA` / `LOCALAPPDATA` env-var lookups with XDG fallback for POSIX in [`config.py:245-247`](../src/file_to_jira/config.py#L245) and [`repocache/manager.py:61-63`](../src/file_to_jira/repocache/manager.py#L61). No hardcoded `/tmp/`, `/var/`, `/usr/local/` anywhere in `src/`. |
| **Subprocess** | [`util/proc.py:75`](../src/file_to_jira/util/proc.py#L75) uses `subprocess.run(..., text=True, encoding="utf-8", errors="replace")`. No `shell=True` anywhere in `src/`. |
| **Repo-cache askpass helper** | [`repocache/manager.py:117-119`](../src/file_to_jira/repocache/manager.py#L117) writes a `.cmd` with `\r\n` line endings on Windows and a `.sh` with `\n` line endings on POSIX, switched on `os.name == "nt"`. Already cross-platform-aware. |
| **Hardcoded line endings in shipped files** | Only the askpass helper (above) writes platform-specific endings; everywhere else, file content is line-ending-agnostic (JSON, YAML, Markdown). |

### Reproducing this audit

The audit greps that produced these findings (run from repo root):

```powershell
# POSIX-only stdlib imports
uv run python -c "import subprocess; print(subprocess.check_output(['rg','-n','--','^(import|from)\s+(fcntl|pwd|grp|termios|resource)\b','src'], text=True))"

# Or via Grep tool (the search above was actually run via the Grep tool):
#   pattern: "import fcntl|import pwd|import grp|import termios|import resource|from fcntl|from pwd"
#   path: src

# open() calls without explicit encoding
#   pattern: "\bopen\("
#   path: src
# Then visually filter for entries that DON'T have `encoding=` nearby
# (binary-mode "rb"/"wb" opens are fine).

# read_text / write_text calls
#   pattern: "read_text\(|write_text\("
#   path: src
# Same visual filter for missing encoding.

# Path / filesystem assumptions
#   pattern: "os\.path\.expanduser|Path\.home\(|/tmp/|/var/|/usr/"
#   path: src

# Subprocess hygiene
#   pattern: "subprocess\.|os\.system|shell=True"
#   path: src
```

The full offline test sweep (after fixes):

```powershell
$env:VIRTUAL_ENV = $null
uv run pytest -m "not live" -q
# Expect: 530 passed, 2 skipped (Windows POSIX-only PID tests), 24 deselected (live)
```

## Future work

### CI matrix — add Windows + macOS jobs

The single highest-leverage cross-platform investment. Rationale:

- The two latent bugs that triggered this audit (`fcntl`-only
  `process_lock.py` and the `.env.example` inline-comment trap) BOTH
  shipped through to operators because nothing in CI ran on Windows.
- The offline test suite is fast (~16s on Windows) and has no external
  dependencies, so a matrix doubles or triples cost only by minutes.
- macOS is also worth including — it shares POSIX semantics with Linux
  but has different filesystem case-sensitivity defaults and different
  default shells, which can surface latent assumptions early.

**Proposed GitHub Actions matrix** (sketch — not yet committed):

```yaml
# .github/workflows/offline-tests.yml
name: offline-tests
on: [push, pull_request]
jobs:
  test:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest, macos-latest]
        python-version: ["3.11"]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          version: latest
      - run: uv sync --extra dev
      - run: uv run pytest -m "not live" -q
```

`live` tests stay opt-in (real LLM + Jira); they continue to run only on
demand or on a separate scheduled job, not on every PR.

**Estimated effort:** half a day, including verifying the workflow
actually green on all three OSes.

### Encoding policy as a lint rule

After the encoding sweep above, the codebase consistently uses
`encoding="utf-8"`. To prevent regression, add a lint check (ruff or a
`pytest --collect-only` style hook) that fails CI if any new
`open(...)` / `read_text(...)` / `write_text(...)` is added without an
explicit `encoding=` (or a binary mode). Low priority — the human
review path is sufficient until a regression actually happens.

### Documentation

- README "Quick start" should mention the PowerShell-on-Windows quirk:
  `$env:VIRTUAL_ENV = $null` once per session before `uv run` (the
  Windows shell inherits a Python 3.14 venv from upstream that confuses
  uv otherwise). Worth one line in the README.
- The `.env.example` warning about inline comments on empty values now
  lives in the file itself (top-of-file note in this commit). No
  separate doc needed.

## Cross-platform invariants — for future contributors

When adding new code, observe these to keep the codebase cross-platform
clean:

1. **Always pass `encoding="utf-8"`** on `open()`, `read_text()`,
   `write_text()` for text files. Binary mode (`"rb"`, `"wb"`) is fine
   without — encoding doesn't apply.
2. **Use `Path` over string paths.** Never concatenate `"/"` into path
   strings; always use `Path("a") / "b"` or `Path.joinpath`.
3. **Use `os.environ.get("APPDATA"|"LOCALAPPDATA")` with an XDG fallback
   on POSIX** for user-config and user-cache directories. Never hardcode
   `/home/`, `/Users/`, `~/AppData`, etc.
4. **Atomic-write idiom:** `with tmp.open("w", encoding="utf-8") as f: ... ;
   os.replace(tmp, target)`. The `with` block must exit before
   `os.replace` — Windows refuses to rename over an open file.
5. **POSIX-only stdlib (`fcntl`, `pwd`, `grp`, `termios`)** must be
   gated behind `if sys.platform != "win32":` with a Windows fallback or
   an explicit "not supported" error.
6. **Subprocess:** never `shell=True`. Always pass `argv` as a list.
   Pass `text=True, encoding="utf-8"` for stdout/stderr decoding.
7. **Line endings:** for files we generate, use `\n` unconditionally
   (JSON, YAML, Markdown, log files). Only the askpass helper writes
   platform-specific endings, and that's because it's a shell script
   the OS reads.
8. **Filesystem case sensitivity:** don't compare paths case-sensitively
   in cross-platform code. Use `Path.resolve()` and compare resolved
   values, or use `path.samefile(other)`.

If you add code that violates one of these, leave a comment explaining
why and the fallback for the other platform — see
[`src/_shared/process_lock.py`](../src/_shared/process_lock.py) for the
worked example.
