"""Subprocess helpers tailored for our git/ripgrep callouts.

All helpers set:
- ``GIT_TERMINAL_PROMPT=0`` so git never opens an interactive auth prompt.
- ``LANG=C.UTF-8`` (best effort) so git/rg output is locale-independent.
- A timeout (default 60s) so a stuck subprocess can't hang the orchestrator.

Output is decoded as UTF-8 with errors replaced — git/rg occasionally emit
non-UTF-8 bytes (e.g. blame on a file with mixed encoding).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 60.0


class CommandError(Exception):
    """Raised when a subprocess returns non-zero or times out."""

    def __init__(
        self,
        argv: list[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        head = stderr.strip().splitlines()[:3] if stderr.strip() else []
        super().__init__(
            f"command failed: {' '.join(argv)!r} (exit={returncode})\n"
            + "\n".join(head)
        )


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


def _build_env(extra: dict[str, str] | None) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("LANG", "C.UTF-8")
    if extra:
        env.update(extra)
    return env


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    check: bool = True,
    input_text: str | None = None,
) -> CommandResult:
    """Run a subprocess, capturing UTF-8 decoded output.

    Raises CommandError on timeout or non-zero exit (when check=True).
    """
    env = _build_env(env_extra)
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            input=input_text,
            capture_output=True,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        raise CommandError(
            argv=argv,
            returncode=-1,
            stdout=(e.stdout or b"").decode("utf-8", errors="replace")
            if isinstance(e.stdout, bytes)
            else (e.stdout or ""),
            stderr=f"timed out after {timeout}s",
        ) from e
    if check and completed.returncode != 0:
        raise CommandError(
            argv=argv,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    return CommandResult(
        argv=argv,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
