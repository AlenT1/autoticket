"""Shallow-clone cache for source repos referenced by bugs.

Design:
- Configured aliases (e.g. ``_core``) are resolved to ``RepoAlias`` specs holding
  a URL + auth strategy. The agent only ever sees the alias string; URL/token
  resolution happens here.
- First request for an alias triggers ``git clone --depth=1`` into a per-URL
  directory under the cache dir. Subsequent requests return the cached path.
- A ``filelock`` per destination prevents concurrent processes (or threads)
  racing the same clone.
- Auth strategies:
    * ``ssh-default``: rely on the user's SSH agent (no special env).
    * ``https-token``: inject a generated GIT_ASKPASS script that prints the
      token from ``token_env``. The token never appears in URLs or logs.
    * ``glab``: shell out to ``glab repo clone <slug>`` for NVIDIA GitLab,
      offloading internal CA / SSO entirely to ``glab``.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from filelock import FileLock

from ..config import GitAuthConfig, RepoAlias
from ..util.proc import CommandError, run


class RepoCacheError(Exception):
    """Base class for repo-cache failures."""


class UnknownAliasError(RepoCacheError):
    """Raised when an alias is not configured."""


class UnsupportedAuthError(RepoCacheError):
    """Raised when an alias references an unknown auth strategy."""


@dataclass(frozen=True)
class CloneInfo:
    repo_alias: str
    local_path: Path
    resolved_url: str
    commit_sha: str
    default_branch: str
    was_cached: bool


def default_cache_dir() -> Path:
    """OS-appropriate default cache root."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return base / "file-to-jira" / "repos"


# Sanitize a single path segment.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._\-]+")


def _short_repo_name(url: str) -> str:
    """Extract just the trailing repo name from a clone URL.

    Used for cache-destination naming. Kept short to stay well under Windows
    MAX_PATH (260 chars) when combined with deep tmp paths.
    """
    if url.startswith("git@"):
        # git@host:org/repo.git → take "repo" from "org/repo.git"
        path = url.split(":", 1)[1]
    else:
        parsed = urlparse(url)
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    name = path.rsplit("/", 1)[-1] or "repo"
    name = _UNSAFE_NAME_CHARS.sub("-", name)
    return name[:40] or "repo"


def _extract_glab_slug(url: str) -> str:
    """Extract the ``group/project`` slug from a GitLab URL for ``glab repo clone``."""
    if url.startswith("git@"):
        _, path = url.split(":", 1)
    else:
        parsed = urlparse(url)
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path


# ---------------------------------------------------------------------------
# GIT_ASKPASS helper for https-token auth
# ---------------------------------------------------------------------------

def _write_askpass_helper(token: str) -> Path:
    """Write a one-shot askpass helper that echoes ``token``.

    Uses a sandboxed temp dir so the file lifetime is scoped to the process.
    The helper has 0o700 perms (POSIX); on Windows ACLs default to per-user.
    """
    fd, path = tempfile.mkstemp(prefix="f2j-askpass-", suffix=".cmd" if os.name == "nt" else ".sh")
    os.close(fd)
    p = Path(path)
    if os.name == "nt":
        # Windows batch: just echo the token.
        p.write_text(f"@echo off\r\necho {token}\r\n", encoding="utf-8")
    else:
        p.write_text(f"#!/bin/sh\necho '{token}'\n", encoding="utf-8")
        p.chmod(stat.S_IRWXU)
    return p


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class RepoCacheManager:
    def __init__(
        self,
        cache_dir: Path,
        aliases: dict[str, RepoAlias],
        *,
        git_auth: GitAuthConfig | None = None,
        clone_depth: int = 1,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.aliases = dict(aliases)
        self.git_auth = git_auth or GitAuthConfig()
        self.clone_depth = clone_depth
        self._cache_dir_ready = False

    # ----- public API -----

    def resolve(self, repo_alias: str) -> RepoAlias:
        if repo_alias not in self.aliases:
            raise UnknownAliasError(
                f"Unknown repo alias: {repo_alias!r}. "
                f"Configured: {sorted(self.aliases)}"
            )
        return self.aliases[repo_alias]

    def ensure_clone(self, repo_alias: str, ref: str | None = None) -> CloneInfo:
        spec = self.resolve(repo_alias)
        dest = self._destination(spec)
        self._ensure_cache_dir()
        lock = FileLock(str(dest) + ".lock", timeout=120)
        with lock:
            if (dest / ".git").exists():
                if ref:
                    self._checkout_ref(dest, ref)
                sha = self._head_sha(dest)
                return CloneInfo(
                    repo_alias=repo_alias,
                    local_path=dest,
                    resolved_url=spec.url,
                    commit_sha=sha,
                    default_branch=spec.default_branch,
                    was_cached=True,
                )
            self._do_clone(spec, dest, ref)
            sha = self._head_sha(dest)
            return CloneInfo(
                repo_alias=repo_alias,
                local_path=dest,
                resolved_url=spec.url,
                commit_sha=sha,
                default_branch=spec.default_branch,
                was_cached=False,
            )

    # ----- internal helpers -----

    def _ensure_cache_dir(self) -> None:
        if not self._cache_dir_ready:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_dir_ready = True

    def _destination(self, spec: RepoAlias) -> Path:
        # Two aliases pointing at the same URL share a clone (via the URL hash).
        # Naming is `<repo-name>@<url-hash>` to stay short on Windows.
        name = _short_repo_name(spec.url)
        url_hash = hashlib.sha256(spec.url.encode("utf-8")).hexdigest()[:12]
        return self.cache_dir / f"{name}@{url_hash}"

    def _head_sha(self, repo: Path) -> str:
        result = run(["git", "-C", str(repo), "rev-parse", "HEAD"])
        return result.stdout.strip()

    def _checkout_ref(self, repo: Path, ref: str) -> None:
        # Best-effort fetch + checkout. The clone is `--single-branch --branch <default>`,
        # so a non-default branch isn't tracked yet — fetch it as a local branch
        # via `<ref>:<ref>` so plain checkout finds it. If the ref doesn't exist
        # on origin (deleted/renamed branch in stale input), leave the repo on
        # the default branch rather than crashing the agent run.
        try:
            run(["git", "-C", str(repo), "checkout", "--quiet", ref])
            return
        except CommandError:
            pass
        fetch = run(
            ["git", "-C", str(repo), "fetch", "--depth=50", "origin", f"{ref}:{ref}"],
            check=False,
        )
        if fetch.returncode == 0:
            try:
                run(["git", "-C", str(repo), "checkout", "--quiet", ref])
                return
            except CommandError:
                pass
        # Ref unresolvable. Fall through silently; the repo stays on its default
        # branch and the agent can still browse code (likely the same paths).

    def ensure_full_history(self, repo: Path) -> None:
        """If ``repo`` is a shallow clone, fetch the full history (idempotent).

        Called by git_blame / git_log_for_path in the toolkit, since shallow
        clones report misleading results for older lines/commits.
        """
        if not (repo / ".git" / "shallow").exists():
            return
        run(
            ["git", "-C", str(repo), "fetch", "--unshallow"],
            check=False,
            timeout=120,
        )

    # ----- clone strategies -----

    def _do_clone(self, spec: RepoAlias, dest: Path, ref: str | None) -> None:
        if spec.auth == "ssh-default":
            self._clone_basic(spec, dest, ref, env_extra=None)
        elif spec.auth == "https-token":
            self._clone_https_token(spec, dest, ref)
        elif spec.auth == "glab":
            self._clone_glab(spec, dest, ref)
        else:
            raise UnsupportedAuthError(
                f"Unsupported auth strategy {spec.auth!r}. "
                "Use ssh-default, https-token, or glab."
            )

    def _clone_basic(
        self,
        spec: RepoAlias,
        dest: Path,
        ref: str | None,
        env_extra: dict[str, str] | None,
    ) -> None:
        argv = ["git", "clone"]
        # clone_depth=0 means "no shallow flag" → full history. Useful for tests
        # and for repos where blame/log on older lines is needed up-front.
        if self.clone_depth > 0:
            argv.extend([f"--depth={self.clone_depth}", "--filter=blob:none"])
        argv.extend(
            [
                "--single-branch",
                "--branch", ref or spec.default_branch,
                spec.url,
                str(dest),
            ]
        )
        env = dict(env_extra) if env_extra else {}
        if self.git_auth.ca_bundle:
            env["GIT_SSL_CAINFO"] = self.git_auth.ca_bundle
        try:
            run(argv, env_extra=env, timeout=180)
        except CommandError as e:
            raise RepoCacheError(
                f"git clone failed for {spec.url}: {e.stderr.strip().splitlines()[:3]}"
            ) from e

    def _clone_https_token(self, spec: RepoAlias, dest: Path, ref: str | None) -> None:
        token_env = spec.token_env
        if not token_env:
            raise RepoCacheError(
                "alias uses https-token auth but no token_env specified"
            )
        token = os.environ.get(token_env)
        if not token:
            raise RepoCacheError(
                f"https-token auth requires env var {token_env}, which is not set"
            )
        helper = _write_askpass_helper(token)
        try:
            self._clone_basic(
                spec,
                dest,
                ref,
                env_extra={
                    "GIT_ASKPASS": str(helper),
                    "GIT_TERMINAL_PROMPT": "0",
                    "GCM_INTERACTIVE": "Never",
                },
            )
        finally:
            try:
                helper.unlink(missing_ok=True)
            except OSError:
                pass

    def _clone_glab(self, spec: RepoAlias, dest: Path, ref: str | None) -> None:
        glab = _which_or_raise("glab", "Install glab: https://gitlab.com/gitlab-org/cli")
        slug = _extract_glab_slug(spec.url)
        argv = [glab, "repo", "clone", slug, str(dest), "--", f"--depth={self.clone_depth}"]
        if ref:
            # glab's clone passthrough varies; do a separate checkout after.
            pass
        try:
            run(argv, timeout=180)
        except CommandError as e:
            raise RepoCacheError(
                f"glab repo clone failed for {slug}: {e.stderr.strip()[:300]}"
            ) from e
        if ref:
            self._checkout_ref(dest, ref)


def _which_or_raise(binary: str, install_hint: str) -> str:
    import shutil

    path = shutil.which(binary)
    if not path:
        raise RepoCacheError(f"{binary!r} not found on PATH. {install_hint}")
    return path
