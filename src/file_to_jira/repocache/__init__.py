"""Shallow git-clone cache shared across enrichment sessions."""

from .manager import (
    CloneInfo,
    RepoCacheError,
    RepoCacheManager,
    UnknownAliasError,
    UnsupportedAuthError,
    default_cache_dir,
)

__all__ = [
    "CloneInfo",
    "RepoCacheError",
    "RepoCacheManager",
    "UnknownAliasError",
    "UnsupportedAuthError",
    "default_cache_dir",
]
