"""Tests for the agent toolkit (clone, search, read, list, blame, log)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from file_to_jira.config import RepoAlias
from file_to_jira.enrich.tools import ToolError, Toolkit
from file_to_jira.enrich.tools.toolkit import (
    _parse_blame_porcelain,
    _parse_rg_json,
)
from file_to_jira.repocache import RepoCacheManager
from tests.fixtures.git_repo import make_sample_repo


# ---------------------------------------------------------------------------
# Module-level helper tests (no subprocess needed)
# ---------------------------------------------------------------------------


def test_parse_rg_json_basic() -> None:
    # Synthesize a tiny rg --json stream by hand.
    stdout = (
        '{"type":"begin","data":{"path":{"text":"src/main.py"}}}\n'
        '{"type":"context","data":{"path":{"text":"src/main.py"},"lines":{"text":"def add(a, b):\\n"},"line_number":1}}\n'
        '{"type":"match","data":{"path":{"text":"src/main.py"},"lines":{"text":"    return a + b\\n"},"line_number":2}}\n'
        '{"type":"context","data":{"path":{"text":"src/main.py"},"lines":{"text":"\\n"},"line_number":3}}\n'
        '{"type":"end","data":{"path":{"text":"src/main.py"}}}\n'
    )
    result = _parse_rg_json(stdout, max_results=10)
    assert result["truncated"] is False
    assert len(result["matches"]) == 1
    m = result["matches"][0]
    assert m["file"] == "src/main.py"
    assert m["line"] == 2
    assert "return a + b" in m["text"]
    assert any("def add" in c["text"] for c in m["before"])
    assert m["after"]


def test_parse_rg_json_respects_max_results() -> None:
    stdout = "\n".join(
        f'{{"type":"match","data":{{"path":{{"text":"f.py"}},"lines":{{"text":"x\\n"}},"line_number":{i}}}}}'
        for i in range(1, 11)
    )
    result = _parse_rg_json(stdout, max_results=3)
    assert result["truncated"] is True
    assert len(result["matches"]) == 3


def test_parse_blame_porcelain() -> None:
    # Trimmed real porcelain output.
    text = (
        "abc1234567890abc1234567890abc1234567890a 1 1 1\n"
        "author Test Author\n"
        "author-mail <test@example.invalid>\n"
        "author-time 1700000000\n"
        "summary initial commit\n"
        "filename src/main.py\n"
        "\tdef add(a, b):\n"
    )
    entries = _parse_blame_porcelain(text)
    assert len(entries) == 1
    e = entries[0]
    assert e["commit_sha"] == "abc1234567890abc1234567890abc1234567890a"
    assert e["author"] == "Test Author"
    assert e["author_mail"] == "test@example.invalid"


# ---------------------------------------------------------------------------
# End-to-end tests against a real local fixture repo
# ---------------------------------------------------------------------------


@pytest.fixture
def upstream_repo(tmp_path: Path) -> Path:
    return make_sample_repo(tmp_path / "upstream")


@pytest.fixture
def toolkit(tmp_path: Path, upstream_repo: Path) -> Toolkit:
    cache = RepoCacheManager(
        cache_dir=tmp_path / "cache",
        aliases={
            "sample": RepoAlias(
                url=upstream_repo.resolve().as_uri(),
                auth="ssh-default",
                default_branch="main",
            ),
        },
    )
    return Toolkit(cache)


# ----- clone_repo -----------------------------------------------------------


def test_clone_repo_returns_alias_only(toolkit: Toolkit) -> None:
    info = toolkit.clone_repo("sample")
    assert info["repo_alias"] == "sample"
    assert "local_path" in info
    assert info["cached"] is False
    info2 = toolkit.clone_repo("sample")
    assert info2["cached"] is True


def test_clone_repo_unknown_alias_raises_tool_error(toolkit: Toolkit) -> None:
    with pytest.raises(ToolError) as ei:
        toolkit.clone_repo("nope")
    assert "nope" in str(ei.value).lower() or "unknown" in str(ei.value).lower()


# ----- read_file ------------------------------------------------------------


def test_read_file_full(toolkit: Toolkit) -> None:
    out = toolkit.read_file("sample", "src/main.py")
    assert out["file"] == "src/main.py"
    assert any("def add" in line for line in out["lines"])
    assert any("def multiply" in line for line in out["lines"])
    assert out["total_lines"] >= 7


def test_read_file_line_range(toolkit: Toolkit) -> None:
    out = toolkit.read_file("sample", "src/main.py", line_start=4, line_end=5)
    assert out["line_start"] == 4
    assert out["line_end"] == 5
    assert len(out["lines"]) == 2
    assert any("def subtract" in line for line in out["lines"])


def test_read_file_rejects_path_traversal(toolkit: Toolkit) -> None:
    with pytest.raises(ToolError):
        toolkit.read_file("sample", "../../etc/hosts")


def test_read_file_rejects_absolute_path(toolkit: Toolkit) -> None:
    with pytest.raises(ToolError):
        toolkit.read_file("sample", "/etc/hosts")


def test_read_file_missing(toolkit: Toolkit) -> None:
    with pytest.raises(ToolError) as ei:
        toolkit.read_file("sample", "src/does_not_exist.py")
    assert "not found" in str(ei.value)


# ----- list_dir -------------------------------------------------------------


def test_list_dir_root(toolkit: Toolkit) -> None:
    out = toolkit.list_dir("sample")
    names = {e["name"] for e in out["entries"]}
    assert "src" in names
    assert "README.md" in names
    assert ".git" not in names


def test_list_dir_subdir(toolkit: Toolkit) -> None:
    out = toolkit.list_dir("sample", "src")
    names = {e["name"] for e in out["entries"]}
    assert names == {"main.py", "auth.py"}
    assert all(e["type"] == "file" for e in out["entries"])


def test_list_dir_missing_raises(toolkit: Toolkit) -> None:
    with pytest.raises(ToolError):
        toolkit.list_dir("sample", "nonexistent")


# ----- search_code (skips if rg missing) -----------------------------------


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not on PATH")
def test_search_code_finds_function(toolkit: Toolkit) -> None:
    out = toolkit.search_code("sample", "def multiply")
    assert out["truncated"] is False
    assert len(out["matches"]) == 1
    assert "multiply" in out["matches"][0]["text"]
    assert out["matches"][0]["file"].replace("\\", "/").endswith("src/main.py")


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not on PATH")
def test_search_code_no_match(toolkit: Toolkit) -> None:
    out = toolkit.search_code("sample", "definitely_not_in_repo_xyzzy")
    assert out["matches"] == []


def test_search_code_falls_back_to_python_when_rg_missing(
    toolkit: Toolkit, monkeypatch
) -> None:
    """When ripgrep isn't installed the toolkit uses a pure-Python fallback."""
    import file_to_jira.enrich.tools.toolkit as tk_module

    monkeypatch.setattr(tk_module.shutil, "which", lambda _name: None)
    out = toolkit.search_code("sample", "def multiply")
    assert out["truncated"] is False
    assert len(out["matches"]) == 1
    assert "multiply" in out["matches"][0]["text"]
    assert out["matches"][0]["file"] == "src/main.py"


def test_search_code_python_fallback_regex(toolkit: Toolkit, monkeypatch) -> None:
    import file_to_jira.enrich.tools.toolkit as tk_module

    monkeypatch.setattr(tk_module.shutil, "which", lambda _name: None)
    out = toolkit.search_code("sample", r"def \w+\(", is_regex=True)
    # Three function definitions in src/main.py + one in src/auth.py.
    assert len(out["matches"]) >= 3


def test_search_code_python_fallback_file_glob(toolkit: Toolkit, monkeypatch) -> None:
    import file_to_jira.enrich.tools.toolkit as tk_module

    monkeypatch.setattr(tk_module.shutil, "which", lambda _name: None)
    out = toolkit.search_code("sample", "fixture", file_glob="*.md")
    files = {m["file"] for m in out["matches"]}
    assert all(f.endswith(".md") for f in files)


def test_search_code_empty_pattern_raises(toolkit: Toolkit) -> None:
    with pytest.raises(ToolError):
        toolkit.search_code("sample", "")


# ----- git_blame ------------------------------------------------------------


def test_git_blame_marks_multiply_as_second_commit(toolkit: Toolkit) -> None:
    """The `multiply` function landed in the second commit; blame on that line
    should report a different commit_sha than line 1."""
    line1 = toolkit.git_blame("sample", "src/main.py", 1, 1)["entries"][0]
    line7 = toolkit.git_blame("sample", "src/main.py", 7, 7)["entries"][0]
    assert line1["commit_sha"] != line7["commit_sha"]
    assert line7["author"] == "Test Author"


def test_git_blame_invalid_range(toolkit: Toolkit) -> None:
    with pytest.raises(ToolError):
        toolkit.git_blame("sample", "src/main.py", 0, 5)
    with pytest.raises(ToolError):
        toolkit.git_blame("sample", "src/main.py", 5, 1)


# ----- git_log_for_path -----------------------------------------------------


def test_git_log_returns_two_commits(toolkit: Toolkit) -> None:
    out = toolkit.git_log_for_path("sample", "src/main.py")
    assert len(out["commits"]) == 2
    subjects = [c["subject"] for c in out["commits"]]
    assert "add multiply" in subjects
    assert "initial commit" in subjects
    for c in out["commits"]:
        assert len(c["sha"]) == 40
        assert c["author"] == "Test Author"
