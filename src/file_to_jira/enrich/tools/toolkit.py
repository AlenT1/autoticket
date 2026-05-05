"""The six agent-callable tools, grouped on a single ``Toolkit`` class.

Tools take an alias (e.g. ``_core``) — never a URL — and return JSON-friendly
dicts. The Claude Agent SDK in Step 5 binds these methods as tool callables.

Each tool:
- Validates inputs (no path traversal, sane line ranges, etc.).
- Routes through ``RepoCacheManager.ensure_clone`` so the clone exists.
- Spawns subprocesses via ``util.proc.run`` (timeout + clean stderr surfacing).
- Raises ``ToolError`` with a message that's safe to show to the agent.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from ...repocache import CloneInfo, RepoCacheError, RepoCacheManager
from ...util.proc import CommandError, run

DEFAULT_MAX_FILE_BYTES = 32_000
DEFAULT_MAX_SEARCH_RESULTS = 50
DEFAULT_LIST_MAX_ENTRIES = 200


class ToolError(Exception):
    """Raised when a tool can't complete; the message is shown to the agent."""


class Toolkit:
    def __init__(self, cache: RepoCacheManager) -> None:
        self.cache = cache

    # ----- 1. clone_repo --------------------------------------------------

    def clone_repo(self, repo_alias: str, ref: str | None = None) -> dict[str, Any]:
        """Ensure a shallow clone of ``repo_alias`` exists; return its info."""
        info = self._ensure(repo_alias, ref)
        return _info_to_dict(info)

    # ----- 2. search_code -------------------------------------------------

    def search_code(
        self,
        repo_alias: str,
        pattern: str,
        *,
        is_regex: bool = False,
        file_glob: str | None = None,
        case_insensitive: bool = False,
        max_results: int = DEFAULT_MAX_SEARCH_RESULTS,
        context_lines: int = 2,
    ) -> dict[str, Any]:
        """Search the cached repo via ripgrep, or a Python fallback if rg is missing.

        The fallback uses a recursive walk + per-line regex match. It's much
        slower on large repos but means the package works without external
        binaries.
        """
        if not pattern:
            raise ToolError("search_code requires a non-empty pattern")
        info = self._ensure(repo_alias)

        rg = shutil.which("rg")
        if rg is None:
            return _python_search_fallback(
                info.local_path,
                pattern,
                is_regex=is_regex,
                file_glob=file_glob,
                case_insensitive=case_insensitive,
                max_results=max_results,
                context_lines=context_lines,
            )

        argv = [rg, "--json", "--with-filename", "--line-number"]
        if not is_regex:
            argv.append("--fixed-strings")
        if case_insensitive:
            argv.append("--ignore-case")
        if context_lines > 0:
            argv.extend(["--context", str(context_lines)])
        if file_glob:
            argv.extend(["--glob", file_glob])
        argv.extend(["--", pattern, "."])

        try:
            result = run(argv, cwd=info.local_path, check=False, timeout=60)
        except CommandError as e:
            raise ToolError(f"ripgrep failed: {e.stderr.strip()[:500]}") from e
        # rg exit codes: 0 = matches, 1 = no matches, 2+ = error.
        if result.returncode > 1:
            raise ToolError(
                f"ripgrep failed (exit {result.returncode}): "
                f"{result.stderr.strip()[:500]}"
            )

        return _parse_rg_json(result.stdout, max_results=max_results)

    # ----- 3. read_file ---------------------------------------------------

    def read_file(
        self,
        repo_alias: str,
        file_path: str,
        *,
        line_start: int | None = None,
        line_end: int | None = None,
        max_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> dict[str, Any]:
        info = self._ensure(repo_alias)
        full = self._safe_join(info.local_path, file_path)
        if not full.is_file():
            raise ToolError(f"file not found: {file_path}")
        return _read_file_slice(full, file_path, line_start, line_end, max_bytes)

    # ----- 4. list_dir ----------------------------------------------------

    def list_dir(
        self,
        repo_alias: str,
        dir_path: str = ".",
        max_entries: int = DEFAULT_LIST_MAX_ENTRIES,
    ) -> dict[str, Any]:
        info = self._ensure(repo_alias)
        full = self._safe_join(info.local_path, dir_path)
        if not full.is_dir():
            raise ToolError(f"directory not found: {dir_path}")
        entries: list[dict[str, str]] = []
        truncated = False
        for child in sorted(full.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name == ".git":
                continue
            if len(entries) >= max_entries:
                truncated = True
                break
            entries.append(
                {
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file",
                }
            )
        return {"dir_path": dir_path, "entries": entries, "truncated": truncated}

    # ----- 5. git_blame ---------------------------------------------------

    def git_blame(
        self,
        repo_alias: str,
        file_path: str,
        line_start: int,
        line_end: int,
    ) -> dict[str, Any]:
        if line_start < 1 or line_end < line_start:
            raise ToolError(
                f"invalid line range {line_start}-{line_end} (start must be >=1 and <= end)"
            )
        info = self._ensure(repo_alias)
        # Shallow clones make blame report the wrong commit for older lines.
        self.cache.ensure_full_history(info.local_path)
        rel = self._safe_join(info.local_path, file_path).relative_to(info.local_path)
        argv = [
            "git", "-C", str(info.local_path),
            "blame",
            "--line-porcelain",
            "-L", f"{line_start},{line_end}",
            "--", str(PurePosixPath(rel)),
        ]
        try:
            result = run(argv, timeout=30)
        except CommandError as e:
            raise ToolError(f"git blame failed: {e.stderr.strip()[:300]}") from e
        return {"file": file_path, "entries": _parse_blame_porcelain(result.stdout)}

    # ----- 6. git_log_for_path -------------------------------------------

    def git_log_for_path(
        self,
        repo_alias: str,
        file_path: str,
        max_entries: int = 10,
    ) -> dict[str, Any]:
        info = self._ensure(repo_alias)
        self.cache.ensure_full_history(info.local_path)
        rel = self._safe_join(info.local_path, file_path).relative_to(info.local_path)
        # Field separators chosen to be unlikely in real metadata.
        fmt = "%H%x1f%an%x1f%ae%x1f%aI%x1f%s"
        argv = [
            "git", "-C", str(info.local_path),
            "log",
            f"-n{max_entries}",
            f"--pretty=format:{fmt}",
            "--", str(PurePosixPath(rel)),
        ]
        try:
            result = run(argv, timeout=30)
        except CommandError as e:
            raise ToolError(f"git log failed: {e.stderr.strip()[:300]}") from e
        commits: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 5:
                continue
            sha, author, email, date, subject = parts
            commits.append(
                {
                    "sha": sha,
                    "author": author,
                    "email": email,
                    "date": date,
                    "subject": subject,
                }
            )
        return {"file": file_path, "commits": commits}

    # ----- shared helpers -------------------------------------------------

    def _ensure(self, repo_alias: str, ref: str | None = None) -> CloneInfo:
        try:
            return self.cache.ensure_clone(repo_alias, ref)
        except RepoCacheError as e:
            raise ToolError(str(e)) from e

    @staticmethod
    def _safe_join(repo_root: Path, rel_path: str) -> Path:
        """Join a repo-relative path safely; reject traversal and absolute paths."""
        if not rel_path or rel_path == ".":
            return repo_root.resolve()
        normalized = rel_path.replace("\\", "/")
        if normalized.startswith("/") or ":" in normalized.split("/", 1)[0]:
            raise ToolError(f"absolute paths are not allowed: {rel_path!r}")
        candidate = (repo_root / normalized).resolve()
        repo_resolved = repo_root.resolve()
        try:
            candidate.relative_to(repo_resolved)
        except ValueError as e:
            raise ToolError(f"path escapes repo root: {rel_path!r}") from e
        return candidate


# ---------------------------------------------------------------------------
# Module-level helpers (kept out of the class to keep methods focused)
# ---------------------------------------------------------------------------


def _info_to_dict(info: CloneInfo) -> dict[str, Any]:
    return {
        "repo_alias": info.repo_alias,
        "local_path": str(info.local_path),
        "resolved_url": info.resolved_url,
        "commit_sha": info.commit_sha,
        "default_branch": info.default_branch,
        "cached": info.was_cached,
    }


def _read_file_slice(
    full: Path,
    rel_path: str,
    line_start: int | None,
    line_end: int | None,
    max_bytes: int,
) -> dict[str, Any]:
    raw = full.read_bytes()
    truncated_bytes = False
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
        truncated_bytes = True
    text = raw.decode("utf-8", errors="replace")
    all_lines = text.splitlines()
    total_lines = len(all_lines)

    start = max(line_start or 1, 1)
    end = min(line_end or total_lines, total_lines) if total_lines else 0
    if line_end is None and line_start is not None:
        # No explicit end: read to end of file.
        end = total_lines
    if start > total_lines:
        lines: list[str] = []
    else:
        lines = all_lines[start - 1 : end]

    return {
        "file": rel_path,
        "line_start": start,
        "line_end": min(end, total_lines),
        "lines": lines,
        "truncated_bytes": truncated_bytes,
        "total_lines": total_lines,
    }


def _rg_text(data: dict[str, Any]) -> str:
    return (data.get("lines") or {}).get("text", "").rstrip("\n")


def _rg_path(data: dict[str, Any]) -> str:
    return (data.get("path") or {}).get("text", "")


def _iter_rg_events(stdout: str):
    """Yield (kind, data) for each parseable line in rg --json output."""
    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        yield event.get("type"), (event.get("data") or {})


def _parse_rg_json(stdout: str, *, max_results: int) -> dict[str, Any]:
    """Parse `rg --json` output (one JSON object per line)."""
    matches: list[dict[str, Any]] = []
    pending_before: list[dict[str, Any]] = []
    truncated = False

    def emit_match(data: dict[str, Any]) -> bool:
        nonlocal pending_before
        matches.append(
            {
                "file": _rg_path(data),
                "line": data.get("line_number"),
                "text": _rg_text(data),
                "before": pending_before,
                "after": [],
            }
        )
        pending_before = []
        return len(matches) >= max_results

    def emit_context(data: dict[str, Any]) -> None:
        entry = {"line": data.get("line_number"), "text": _rg_text(data)}
        if matches:
            matches[-1]["after"].append(entry)
        else:
            pending_before.append(entry)

    for kind, data in _iter_rg_events(stdout):
        if kind == "match":
            if emit_match(data):
                truncated = True
                break
        elif kind == "context":
            emit_context(data)
        elif kind == "begin":
            pending_before = []
    return {"matches": matches, "truncated": truncated}


_BLAME_FIELD_MAP = {
    "author": ("author", lambda v: v),
    "author-mail": ("author_mail", lambda v: v.strip("<>")),
    "author-time": ("author_time", lambda v: v),
    "summary": ("summary", lambda v: v),
}


def _is_sha(s: str) -> bool:
    return len(s) == 40 and all(c in "0123456789abcdef" for c in s)


def _apply_blame_header(current: dict[str, str], head: str, value: str) -> dict[str, str]:
    """Apply a single header line to ``current`` (creates a new dict if SHA)."""
    if _is_sha(head):
        return {"commit_sha": head}
    mapping = _BLAME_FIELD_MAP.get(head)
    if mapping is not None:
        key, transform = mapping
        current[key] = transform(value)
    return current


# ---------------------------------------------------------------------------
# Python-native search fallback (used when ripgrep is not installed)
# ---------------------------------------------------------------------------

# Files we skip in the fallback walker. ripgrep would skip these via gitignore;
# we approximate with a fixed deny-list to avoid a gitignore parser dep.
_FALLBACK_SKIP_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", "dist", "build"}
)
_FALLBACK_SKIP_EXTS = frozenset(
    {
        ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".class",
        ".jar", ".war", ".ear", ".bin", ".o", ".a", ".lib",
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".pdf",
        ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".woff", ".woff2",
    }
)
_FALLBACK_MAX_FILE_BYTES = 1_000_000  # don't search files larger than ~1 MB


def _fallback_iter_files(root: Path, file_glob: str | None):
    """Walk ``root`` and yield candidate file paths."""
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(root)
        if any(part in _FALLBACK_SKIP_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() in _FALLBACK_SKIP_EXTS:
            continue
        if file_glob and not fnmatch.fnmatch(path.name, file_glob):
            continue
        yield path


def _compile_search_regex(pattern: str, *, is_regex: bool, case_insensitive: bool) -> re.Pattern:
    flags = re.IGNORECASE if case_insensitive else 0
    if is_regex:
        try:
            return re.compile(pattern, flags)
        except re.error as e:
            raise ToolError(f"invalid regex {pattern!r}: {e}") from e
    return re.compile(re.escape(pattern), flags)


def _read_file_for_search(path: Path) -> list[str] | None:
    """Read a file's lines for searching. Returns None if it should be skipped."""
    try:
        if path.stat().st_size > _FALLBACK_MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def _search_one_file(
    rel_posix: str,
    lines: list[str],
    regex: re.Pattern,
    context_lines: int,
    remaining: int,
) -> list[dict[str, Any]]:
    """Search a single file's lines and return up to `remaining` matches."""
    out: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        if not regex.search(line):
            continue
        before = [
            {"line": j + 1, "text": lines[j]}
            for j in range(max(0, i - context_lines), i)
        ]
        after = [
            {"line": j + 1, "text": lines[j]}
            for j in range(i + 1, min(len(lines), i + 1 + context_lines))
        ]
        out.append(
            {
                "file": rel_posix,
                "line": i + 1,
                "text": line,
                "before": before,
                "after": after,
            }
        )
        if len(out) >= remaining:
            break
    return out


def _python_search_fallback(
    repo_root: Path,
    pattern: str,
    *,
    is_regex: bool,
    file_glob: str | None,
    case_insensitive: bool,
    max_results: int,
    context_lines: int,
) -> dict[str, Any]:
    """Pure-Python equivalent of ``rg --json``."""
    regex = _compile_search_regex(
        pattern, is_regex=is_regex, case_insensitive=case_insensitive
    )
    matches: list[dict[str, Any]] = []
    for path in _fallback_iter_files(repo_root, file_glob):
        lines = _read_file_for_search(path)
        if lines is None:
            continue
        rel_posix = path.relative_to(repo_root).as_posix()
        remaining = max_results - len(matches)
        if remaining <= 0:
            break
        matches.extend(
            _search_one_file(rel_posix, lines, regex, context_lines, remaining)
        )
    truncated = len(matches) >= max_results
    return {"matches": matches[:max_results], "truncated": truncated}


# ---------------------------------------------------------------------------
# Blame parsing
# ---------------------------------------------------------------------------


def _parse_blame_porcelain(text: str) -> list[dict[str, str]]:
    """Parse ``git blame --line-porcelain`` output into per-line entries."""
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw:
            continue
        if raw.startswith("\t"):
            # Source line: commits the in-progress record and resets.
            if current:
                entries.append(current)
                current = {}
            continue
        if " " not in raw:
            continue
        head, _, value = raw.partition(" ")
        current = _apply_blame_header(current, head, value)
    if current:
        entries.append(current)
    return entries
