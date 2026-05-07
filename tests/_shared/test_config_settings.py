"""Offline tests for `_shared.config.Settings` + `load_settings`.

Coverage:
- Canonical env names populate fields (one canonical name per field).
- Legacy names (NVIDIA_LLM_API_KEY, JIRA_PAT, OPENAI_API_KEY, ...) are
  intentionally NOT honored — single canonical name only.
- YAML layer wins over defaults but loses to env.
- Explicit kwargs win over env.
- effective_jira_token() walks the autodev chain.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from _shared.config import load_settings


# ----------------------------------------------------------------------
# Fixtures: a clean env with nothing leaking in
# ----------------------------------------------------------------------

_ENV_NAMES = (
    "NVIDIA_API_KEY",
    "NVIDIA_LLM_API_KEY",
    "OPENAI_API_KEY",
    "NVIDIA_BASE_URL",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "JIRA_HOST",
    "JIRA_PROJECT_KEY",
    "JIRA_AUTH_MODE",
    "JIRA_USER_EMAIL",
    "JIRA_TOKEN",
    "JIRA_PAT",
    "AUTODEV_TOKEN",
    "JIRA_CA_BUNDLE",
    "DRIVE_FOLDER_ID",
    "FOLDER_ID",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_REFRESH_TOKEN",
    "GIT_TOKEN_GITLAB_NVIDIA",
    "GITLAB_TOKEN",
    "REPO_OWNER",
    "REPO_NAME",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ----------------------------------------------------------------------
# Canonical env-driven loading
# ----------------------------------------------------------------------

def test_canonical_env_names_populate_fields(clean_env):
    clean_env.setenv("NVIDIA_API_KEY", "k1")
    clean_env.setenv("NVIDIA_BASE_URL", "https://example/v1")
    clean_env.setenv("JIRA_HOST", "jira.example.com")
    clean_env.setenv("JIRA_TOKEN", "tok")
    clean_env.setenv("DRIVE_FOLDER_ID", "drive-folder-uuid")
    clean_env.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    clean_env.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "csec")
    clean_env.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "rtok")

    s = load_settings(yaml_paths=[], env_file=None)

    assert s.nvidia_api_key == "k1"
    assert s.nvidia_base_url == "https://example/v1"
    assert s.jira_host == "jira.example.com"
    assert s.jira_token == "tok"
    assert s.drive_folder_id == "drive-folder-uuid"
    assert s.google_oauth_client_id == "cid"
    assert s.google_oauth_client_secret == "csec"
    assert s.google_oauth_refresh_token == "rtok"


def test_legacy_names_are_ignored(clean_env):
    """Single-canonical-name policy: legacy alternatives are silently dropped."""
    clean_env.setenv("NVIDIA_LLM_API_KEY", "legacy-llm")
    clean_env.setenv("OPENAI_API_KEY", "legacy-openai")
    clean_env.setenv("JIRA_PAT", "legacy-pat")
    clean_env.setenv("FOLDER_ID", "legacy-folder")

    s = load_settings(yaml_paths=[], env_file=None)

    assert s.nvidia_api_key is None
    assert s.jira_token is None
    assert s.drive_folder_id is None


def test_unset_fields_are_none(clean_env):
    s = load_settings(yaml_paths=[], env_file=None)
    assert s.nvidia_api_key is None
    assert s.jira_token is None
    assert s.drive_folder_id is None
    assert s.google_oauth_client_id is None
    assert s.jira_auth_mode == "bearer"  # only field with non-None default


# ----------------------------------------------------------------------
# YAML layering
# ----------------------------------------------------------------------

def test_yaml_populates_when_env_unset(clean_env, tmp_path):
    yaml_file = tmp_path / "shared.yaml"
    yaml_file.write_text(
        "nvidia_api_key: yaml-key\n"
        "jira_host: jira.example.com\n"
        "jira_project_key: CENTPM\n"
    )

    s = load_settings(yaml_paths=[yaml_file], env_file=None)

    assert s.nvidia_api_key == "yaml-key"
    assert s.jira_host == "jira.example.com"
    assert s.jira_project_key == "CENTPM"


def test_env_wins_over_yaml(clean_env, tmp_path):
    yaml_file = tmp_path / "shared.yaml"
    yaml_file.write_text("nvidia_api_key: yaml-key\n")
    clean_env.setenv("NVIDIA_API_KEY", "env-key")

    s = load_settings(yaml_paths=[yaml_file], env_file=None)

    assert s.nvidia_api_key == "env-key"


def test_overrides_win_over_env(clean_env):
    clean_env.setenv("NVIDIA_API_KEY", "env-key")

    s = load_settings(yaml_paths=[], env_file=None, nvidia_api_key="override")

    assert s.nvidia_api_key == "override"


def test_yaml_layered_later_wins(clean_env, tmp_path):
    base = tmp_path / "base.yaml"
    base.write_text("nvidia_api_key: from-base\njira_host: a.example.com\n")
    project = tmp_path / "project.yaml"
    project.write_text("jira_host: b.example.com\n")

    s = load_settings(yaml_paths=[base, project], env_file=None)

    assert s.nvidia_api_key == "from-base"  # only base set it
    assert s.jira_host == "b.example.com"  # project overrode


def test_yaml_ignores_unknown_fields(clean_env, tmp_path):
    """Agent-namespaced sections (f2j:, jira_task_agent:) at the top level
    must not raise — they belong in agent-specific YAMLs."""
    yaml_file = tmp_path / "shared.yaml"
    yaml_file.write_text(
        "nvidia_api_key: ok\n"
        "f2j:\n"
        "  jira:\n"
        "    issue_type: Bug\n"
        "jira_task_agent:\n"
        "  models:\n"
        "    classify: foo\n"
    )

    s = load_settings(yaml_paths=[yaml_file], env_file=None)

    assert s.nvidia_api_key == "ok"


# ----------------------------------------------------------------------
# .env file loading
# ----------------------------------------------------------------------

def test_env_file_is_read(clean_env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("NVIDIA_API_KEY=from-dotenv\nJIRA_TOKEN=dot-token\n")

    s = load_settings(yaml_paths=[], env_file=env_file)

    assert s.nvidia_api_key == "from-dotenv"
    assert s.jira_token == "dot-token"


def test_actual_env_wins_over_dotenv(clean_env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("NVIDIA_API_KEY=from-dotenv\n")
    clean_env.setenv("NVIDIA_API_KEY", "from-real-env")

    s = load_settings(yaml_paths=[], env_file=env_file)

    assert s.nvidia_api_key == "from-real-env"


# ----------------------------------------------------------------------
# Autodev token chain
# ----------------------------------------------------------------------

def test_effective_jira_token_prefers_explicit_field(clean_env):
    s = load_settings(
        yaml_paths=[], env_file=None,
        jira_token="explicit", autodev_token="should-not-be-used",
    )
    assert s.effective_jira_token() == "explicit"


def test_effective_jira_token_falls_back_to_autodev_env(clean_env):
    s = load_settings(yaml_paths=[], env_file=None, autodev_token="autodev-fallback")
    assert s.effective_jira_token() == "autodev-fallback"


def test_effective_jira_token_returns_none_when_unset(clean_env):
    s = load_settings(yaml_paths=[], env_file=None)
    assert s.effective_jira_token() is None


def test_effective_jira_token_reads_role_file(clean_env, tmp_path, monkeypatch):
    """Token at ~/.autodev/tokens/task-jira-<project> wins over AUTODEV_TOKEN."""
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    role_dir = fake_home / ".autodev" / "tokens"
    role_dir.mkdir(parents=True)
    (role_dir / "task-jira-CENTPM").write_text("from-role-file\n")

    s = load_settings(
        yaml_paths=[], env_file=None,
        jira_project_key="CENTPM", autodev_token="loses",
    )

    assert s.effective_jira_token() == "from-role-file"
