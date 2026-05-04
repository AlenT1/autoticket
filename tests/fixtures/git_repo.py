"""Build a tiny on-disk git repo for tool tests. No network. Pure subprocess."""

from __future__ import annotations

import os
from pathlib import Path

from file_to_jira.util.proc import run

_AUTHOR_ENV = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def make_sample_repo(root: Path) -> Path:
    """Create a small but realistic git repo at ``root``. Returns the same path."""
    root.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q", "-b", "main", str(root)])
    # Force-disable any inherited GPG signing.
    run(["git", "-C", str(root), "config", "commit.gpgsign", "false"])
    run(["git", "-C", str(root), "config", "user.name", "Test Author"])
    run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"])

    (root / "src").mkdir()
    (root / "src" / "main.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def subtract(a, b):\n"
        "    return a - b\n",
        encoding="utf-8",
    )
    (root / "src" / "auth.py").write_text(
        "def login(username, password):\n"
        "    # TODO: validate credentials\n"
        "    return True\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "# sample-repo\n\nFixture repo for tool tests.\n",
        encoding="utf-8",
    )
    run(["git", "-C", str(root), "add", "."], env_extra=_AUTHOR_ENV)
    run(
        ["git", "-C", str(root), "commit", "-q", "-m", "initial commit"],
        env_extra=_AUTHOR_ENV,
    )

    # Add a second commit so blame and log have something to differentiate.
    (root / "src" / "main.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def subtract(a, b):\n"
        "    return a - b\n"
        "\n"
        "def multiply(a, b):\n"
        "    return a * b\n",
        encoding="utf-8",
    )
    run(["git", "-C", str(root), "add", "src/main.py"], env_extra=_AUTHOR_ENV)
    run(
        ["git", "-C", str(root), "commit", "-q", "-m", "add multiply"],
        env_extra=_AUTHOR_ENV,
    )
    return root
