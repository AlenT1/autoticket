"""Unified Settings — flat fields, one canonical env-var name each.

Each field's name maps 1:1 to its env var via ``case_sensitive=False``:
``nvidia_api_key`` ↔ ``NVIDIA_API_KEY``. No aliases — if the operator
puts ``NVIDIA_LLM_API_KEY`` in ``.env`` it is ignored. The single
canonical name is enforced.

Each shared resource pulls what it needs via ``from_settings(s)``.
YAML at ``configs/shared.yaml`` (lowercase keys matching field names)
layers under env values.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# `.env` is loaded once at `_shared` package import time (see
# `_shared/__init__.py`), not here — settings.py is imported lazily by
# call sites, which would run AFTER any test's monkeypatch.delenv() and
# silently restore deleted values.


# ---------------------------------------------------------------------------
# YAML source
# ---------------------------------------------------------------------------

def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Settings YAML at {path} must be a mapping at the root.")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _default_yaml_paths() -> list[Path]:
    """Single shipped YAML for shared (non-secret) defaults."""
    return [Path.cwd() / "configs" / "shared.yaml"]


class _YamlConfigSettingsSource(PydanticBaseSettingsSource):
    """Reads layered YAML files into a flat dict pydantic-settings can consume."""

    def __init__(self, settings_cls, yaml_paths: list[Path]) -> None:
        super().__init__(settings_cls)
        merged: dict[str, Any] = {}
        for p in yaml_paths:
            merged = _deep_merge(merged, _read_yaml(p))
        # Keep only top-level keys that are actual model fields. Other keys
        # (like ``f2j:`` / ``jira_task_agent:`` namespaced sections that
        # belong in agent-specific YAMLs) are ignored here.
        field_names = set(settings_cls.model_fields.keys())
        self._data = {k: v for k, v in merged.items() if k in field_names}

    def get_field_value(self, field, field_name):  # pragma: no cover - unused
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._data)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    """All shared secrets / endpoints / paths in one object.

    Field defaults are ``None`` so callers can detect "not set"; the
    consuming resource decides whether the missing value is fatal.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # ----- LLM (NVIDIA Inference / OpenAI-compat) -----
    nvidia_api_key: str | None = None
    nvidia_base_url: str | None = None

    # ----- Anthropic (used by f2j's tool-use agent path) -----
    anthropic_api_key: str | None = None
    anthropic_base_url: str | None = None

    # ----- Jira -----
    jira_host: str | None = None
    jira_project_key: str | None = None
    jira_auth_mode: str = "bearer"
    jira_user_email: str | None = None
    jira_token: str | None = None
    autodev_token: str | None = None  # fallback path for autodev integration
    jira_ca_bundle: str | None = None

    # ----- Google Drive -----
    drive_folder_id: str | None = None
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_refresh_token: str | None = None
    drive_credentials_path: Path = Path("credentials.json")
    drive_token_path: Path = Path("token.json")
    drive_download_dir: Path = Path("data/gdrive_files")

    # ----- Git (f2j repo-clone path) -----
    gitlab_token: str | None = None

    # ----- Effective resolvers (small, deterministic helpers) -----

    def effective_jira_token(self) -> str | None:
        """Walk the autodev token chain.

        1. ``jira_token`` (env: ``JIRA_TOKEN``)
        2. ``~/.autodev/tokens/task-jira-${jira_project_key}``
        3. ``~/.autodev/tokens/${REPO_OWNER}-${REPO_NAME}`` (legacy)
        4. ``autodev_token`` (env: ``AUTODEV_TOKEN``)
        """
        if self.jira_token:
            return self.jira_token.strip()
        project = self.jira_project_key or os.environ.get("REPO_NAME") or "unknown"
        role_file = Path.home() / ".autodev" / "tokens" / f"task-jira-{project}"
        if role_file.exists():
            return role_file.read_text(encoding="utf-8").strip()
        repo_owner = os.environ.get("REPO_OWNER", "unknown")
        repo_name = os.environ.get("REPO_NAME", "unknown")
        legacy = Path.home() / ".autodev" / "tokens" / f"{repo_owner}-{repo_name}"
        if legacy.exists():
            return legacy.read_text(encoding="utf-8").strip()
        if self.autodev_token:
            return self.autodev_token.strip()
        return None


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_settings(
    *,
    yaml_paths: list[Path] | None = None,
    env_file: str | Path | None = None,
    **overrides: Any,
) -> Settings:
    """Construct a :class:`Settings` honoring layered YAML + env + overrides.

    Args:
        yaml_paths: Override the default YAML search paths. Pass ``[]`` to
            skip YAML entirely (handy in tests). Default reads
            ``configs/shared.yaml``.
        env_file: Path to a ``.env`` file. ``None`` (default) means "rely
            on the one-shot ``.env`` load done at ``_shared`` package
            import — no extra reading here." Pass an explicit path to
            load that file in addition (used by tests with custom
            ``.env`` contents).
        **overrides: Keyword overrides passed to ``Settings(**overrides)``;
            highest precedence.

    Layered (later wins): YAML → environment → overrides.
    (``.env`` was loaded into ``os.environ`` once at package import.)

    Tests calling ``monkeypatch.delenv(...)`` / ``monkeypatch.setenv(...)``
    after package import are honored — there is no later silent reload.
    """
    if env_file is not None:
        env_path = Path(env_file)
        if env_path.exists():
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)

    yaml_paths_resolved = (
        _default_yaml_paths() if yaml_paths is None else list(yaml_paths)
    )

    # Build the subclass with a fresh model_config — env_file is always
    # disabled here because the load happened once at module import.
    class _Loader(Settings):
        model_config = SettingsConfigDict(
            env_file=None,
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
            populate_by_name=True,
        )

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        ):
            yaml_source = _YamlConfigSettingsSource(settings_cls, yaml_paths_resolved)
            return (
                init_settings,
                env_settings,
                yaml_source,
                file_secret_settings,
            )

    return _Loader(**overrides)
