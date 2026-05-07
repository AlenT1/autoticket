"""Workspace bootstrap + configuration health-check.

Two operator-facing helpers used by both CLIs (``f2j`` and
``jira-task-agent``):

- :func:`init_workspace` — first-time setup: copy ``.env.example`` to
  ``.env`` and create ``data/local_files/``. Idempotent and never
  overwrites an existing ``.env``.
- :func:`check_config` — read the current ``Settings`` and report which
  required keys are present, missing, or partial. Returns a list of
  :class:`Check` results that the CLI prints.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .settings import Settings, load_settings


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@dataclass
class InitResult:
    env_created: bool
    env_path: Path
    dirs_created: list[Path]
    dirs_existed: list[Path]


def init_workspace(
    *,
    root: Path | None = None,
    env_example: str = ".env.example",
    env_target: str = ".env",
    dirs: Iterable[str] = ("data/local_files",),
) -> InitResult:
    """Create the minimum local workspace needed to run the agents.

    Steps:
      1. Copy ``.env.example`` to ``.env`` if (and only if) ``.env``
         does not already exist. Never overwrites secrets.
      2. Create the user-input dirs (``data/local_files/`` by default).

    Args:
        root: Project root. Defaults to current working directory.
        env_example: Source filename for the env template.
        env_target: Destination filename for the env file.
        dirs: Directories to create relative to ``root``.

    Returns:
        :class:`InitResult` describing what changed.
    """
    base = Path(root) if root else Path.cwd()
    env_src = base / env_example
    env_dst = base / env_target

    env_created = False
    if not env_dst.exists():
        if not env_src.exists():
            raise FileNotFoundError(
                f"Cannot init: {env_src} is missing. The repo ships with "
                f".env.example — make sure you're running from the project root."
            )
        shutil.copy2(env_src, env_dst)
        env_created = True

    dirs_created: list[Path] = []
    dirs_existed: list[Path] = []
    for d in dirs:
        p = base / d
        if p.exists():
            dirs_existed.append(p)
        else:
            p.mkdir(parents=True, exist_ok=True)
            dirs_created.append(p)

    return InitResult(
        env_created=env_created,
        env_path=env_dst,
        dirs_created=dirs_created,
        dirs_existed=dirs_existed,
    )


def format_init_result(r: InitResult) -> str:
    """Human-readable summary of an :class:`InitResult` plus next-steps."""
    lines: list[str] = []
    if r.env_created:
        lines.append(f"Created {r.env_path} from .env.example")
    else:
        lines.append(f"{r.env_path} already exists — left untouched")
    for p in r.dirs_created:
        lines.append(f"Created {p}/")
    for p in r.dirs_existed:
        lines.append(f"{p}/ already exists")
    lines.append("")
    lines.append("Next steps:")
    lines.append(f"  1. Edit {r.env_path} and fill in:")
    lines.append("       JIRA_HOST  JIRA_PROJECT_KEY  JIRA_TOKEN  JIRA_AUTH_MODE")
    lines.append("       NVIDIA_API_KEY")
    lines.append("     Optional (only if you'll use Drive):")
    lines.append("       DRIVE_FOLDER_ID")
    lines.append("       GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET,")
    lines.append("       GOOGLE_OAUTH_REFRESH_TOKEN")
    lines.append("")
    lines.append("  2. Verify your config:    jira-task-agent doctor")
    lines.append("  3. Run a dry-run:         jira-task-agent run")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    status: str  # "ok" | "warn" | "error"
    detail: str = ""


def check_config(settings: Settings | None = None) -> list[Check]:
    """Run the configuration health checks. Pure function — does not exit."""
    s = settings if settings is not None else load_settings()
    checks: list[Check] = []

    # --- Jira (required) ---
    if s.jira_host:
        checks.append(Check("JIRA_HOST", "ok", s.jira_host))
    else:
        checks.append(Check("JIRA_HOST", "error", "missing"))

    if s.jira_project_key:
        checks.append(Check("JIRA_PROJECT_KEY", "ok", s.jira_project_key))
    else:
        checks.append(Check("JIRA_PROJECT_KEY", "error", "missing"))

    if s.jira_auth_mode in ("bearer", "basic"):
        checks.append(Check("JIRA_AUTH_MODE", "ok", s.jira_auth_mode))
    else:
        checks.append(
            Check(
                "JIRA_AUTH_MODE", "error",
                f"invalid value {s.jira_auth_mode!r}; expected 'bearer' or 'basic'",
            )
        )

    if s.jira_auth_mode == "basic" and not s.jira_user_email:
        checks.append(
            Check(
                "JIRA_USER_EMAIL", "error",
                "required when JIRA_AUTH_MODE=basic",
            )
        )

    if s.effective_jira_token():
        checks.append(Check("JIRA_TOKEN", "ok", "resolved"))
    else:
        checks.append(
            Check(
                "JIRA_TOKEN", "error",
                "no token resolvable; set JIRA_TOKEN, AUTODEV_TOKEN, "
                "or place a token at ~/.autodev/tokens/task-jira-<project>",
            )
        )

    # --- LLM (required) ---
    if s.nvidia_api_key:
        checks.append(Check("NVIDIA_API_KEY", "ok", "set"))
    else:
        checks.append(Check("NVIDIA_API_KEY", "error", "missing"))

    # --- Drive (optional but warn if half-configured) ---
    drive_keys = (
        s.drive_folder_id,
        s.google_oauth_client_id,
        s.google_oauth_client_secret,
        s.google_oauth_refresh_token,
    )
    drive_set = sum(1 for v in drive_keys if v)
    if drive_set == 0:
        checks.append(
            Check(
                "Drive", "warn",
                "no Drive credentials set — Drive source unavailable; "
                "--source local will still work",
            )
        )
    elif drive_set == 4:
        checks.append(Check("Drive", "ok", "all OAuth env vars set"))
    else:
        checks.append(
            Check(
                "Drive", "error",
                f"{drive_set}/4 Drive vars set — partial config will fail at "
                "first Drive call. Set all of DRIVE_FOLDER_ID, "
                "GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, "
                "GOOGLE_OAUTH_REFRESH_TOKEN",
            )
        )

    return checks


def format_check_results(checks: list[Check]) -> str:
    """Format checks for terminal output. Returns the full report string."""
    width_status = max((len(c.status) for c in checks), default=5)
    width_name = max((len(c.name) for c in checks), default=10)
    lines = ["Doctor: configuration check", ""]
    for c in checks:
        lines.append(
            f"[{c.status:<{width_status}}]  {c.name:<{width_name}}  {c.detail}"
        )
    n_err = sum(1 for c in checks if c.status == "error")
    n_warn = sum(1 for c in checks if c.status == "warn")
    lines.append("")
    if n_err:
        lines.append(f"Result: {n_err} error(s), {n_warn} warning(s)")
        lines.append("Fix the errors before running the agent.")
    elif n_warn:
        lines.append(f"Result: 0 errors, {n_warn} warning(s)")
        lines.append("Configuration is usable. Warnings are informational.")
    else:
        lines.append("Result: all checks passed.")
    return "\n".join(lines)


def doctor_exit_code(checks: list[Check]) -> int:
    """0 if no errors, 1 otherwise. Warnings do not fail."""
    return 1 if any(c.status == "error" for c in checks) else 0
