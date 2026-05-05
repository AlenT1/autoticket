"""Tests for RepoCacheManager (clone strategies + cache behavior)."""

from __future__ import annotations

from pathlib import Path

import pytest

from file_to_jira.config import RepoAlias
from file_to_jira.repocache import (
    RepoCacheManager,
    UnknownAliasError,
    UnsupportedAuthError,
)
from file_to_jira.repocache.manager import (
    _extract_glab_slug,
    _short_repo_name,
)
from tests.file_to_jira.fixtures.git_repo import make_sample_repo


@pytest.fixture
def upstream_repo(tmp_path: Path) -> Path:
    return make_sample_repo(tmp_path / "upstream")


@pytest.fixture
def cache_manager(tmp_path: Path, upstream_repo: Path) -> RepoCacheManager:
    return RepoCacheManager(
        cache_dir=tmp_path / "cache",
        aliases={
            "sample": RepoAlias(
                # `file://` form so git's --depth/--filter aren't ignored on Windows.
                url=upstream_repo.resolve().as_uri(),
                auth="ssh-default",
                default_branch="main",
            ),
        },
    )


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://gitlab.example.com/team/repo.git", "repo"),
        ("git@gitlab.example.com:team/repo.git", "repo"),
        ("https://github.com/org/sub-org/myrepo", "myrepo"),
        ("file:///C:/some/local/path/upstream", "upstream"),
    ],
)
def test_short_repo_name(url: str, expected: str) -> None:
    assert _short_repo_name(url) == expected


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://gitlab-master.nvidia.com/team/myrepo.git", "team/myrepo"),
        ("git@gitlab-master.nvidia.com:team/myrepo.git", "team/myrepo"),
        ("https://gitlab-master.nvidia.com/group/sub/myrepo", "group/sub/myrepo"),
    ],
)
def test_extract_glab_slug(url: str, expected: str) -> None:
    assert _extract_glab_slug(url) == expected


# ---------------------------------------------------------------------------
# Manager behavior
# ---------------------------------------------------------------------------


def test_unknown_alias_raises(cache_manager: RepoCacheManager) -> None:
    with pytest.raises(UnknownAliasError) as ei:
        cache_manager.ensure_clone("does-not-exist")
    assert "does-not-exist" in str(ei.value)


def test_clone_then_reuse(cache_manager: RepoCacheManager) -> None:
    info1 = cache_manager.ensure_clone("sample")
    assert info1.was_cached is False
    assert (info1.local_path / ".git").exists()
    assert (info1.local_path / "src" / "main.py").exists()
    assert info1.commit_sha  # 40-hex string
    assert len(info1.commit_sha) == 40

    # Second call returns cached.
    info2 = cache_manager.ensure_clone("sample")
    assert info2.was_cached is True
    assert info2.local_path == info1.local_path
    assert info2.commit_sha == info1.commit_sha


def test_clone_destination_is_under_cache_dir(
    cache_manager: RepoCacheManager, tmp_path: Path
) -> None:
    info = cache_manager.ensure_clone("sample")
    assert (tmp_path / "cache") in info.local_path.parents


def test_unsupported_auth_strategy(tmp_path: Path) -> None:
    cache = RepoCacheManager(
        cache_dir=tmp_path / "c",
        aliases={
            "x": RepoAlias(url="https://example.com/x.git", auth="bogus", default_branch="main"),
        },
    )
    with pytest.raises(UnsupportedAuthError):
        cache.ensure_clone("x")


def test_https_token_missing_env_raises(tmp_path: Path) -> None:
    cache = RepoCacheManager(
        cache_dir=tmp_path / "c",
        aliases={
            "x": RepoAlias(
                url="https://example.com/x.git",
                auth="https-token",
                token_env="DEFINITELY_NOT_SET_ENV_VAR",
            ),
        },
    )
    from file_to_jira.repocache import RepoCacheError

    with pytest.raises(RepoCacheError) as ei:
        cache.ensure_clone("x")
    assert "DEFINITELY_NOT_SET_ENV_VAR" in str(ei.value)
