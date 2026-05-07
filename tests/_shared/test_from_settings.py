"""Offline tests for the unified ``from_settings(...)`` constructors.

Verifies that each shared resource consumes the unified ``Settings``
object correctly — happy path + missing-required-field error path.
"""
from __future__ import annotations

import pytest

from _shared.config import load_settings
from _shared.io.sinks.jira.client import JiraClient
from _shared.llm import AnthropicProvider, OpenAICompatProvider


_ENV_NAMES = (
    "NVIDIA_API_KEY", "NVIDIA_LLM_API_KEY", "OPENAI_API_KEY",
    "NVIDIA_BASE_URL", "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "JIRA_HOST", "JIRA_PROJECT_KEY", "JIRA_AUTH_MODE", "JIRA_USER_EMAIL",
    "JIRA_TOKEN", "JIRA_PAT", "AUTODEV_TOKEN", "JIRA_CA_BUNDLE",
    "DRIVE_FOLDER_ID", "FOLDER_ID",
    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_REFRESH_TOKEN",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ----------------------------------------------------------------------
# JiraClient.from_settings
# ----------------------------------------------------------------------

def test_jira_client_from_settings_bearer_happy_path(clean_env):
    s = load_settings(
        yaml_paths=[], env_file=None,
        jira_host="jira.example.com",
        jira_token="tok-123",
    )
    client = JiraClient.from_settings(s)
    assert client.host == "jira.example.com"
    assert client.auth_header == "Bearer tok-123"
    assert client.auth_mode == "bearer"


def test_jira_client_from_settings_basic_cloud(clean_env):
    s = load_settings(
        yaml_paths=[], env_file=None,
        jira_host="https://acme.atlassian.net",
        jira_token="api-token",
        jira_auth_mode="basic",
        jira_user_email="me@acme.com",
    )
    client = JiraClient.from_settings(s)
    # basic encodes b64(email:token)
    assert client.auth_header.startswith("Basic ")
    assert client.auth_mode == "basic"
    assert client.host == "acme.atlassian.net"


def test_jira_client_from_settings_strips_url_scheme(clean_env):
    s = load_settings(
        yaml_paths=[], env_file=None,
        jira_host="https://jira.example.com/",
        jira_token="t",
    )
    client = JiraClient.from_settings(s)
    assert client.host == "jira.example.com"


def test_jira_client_from_settings_missing_host(clean_env):
    s = load_settings(yaml_paths=[], env_file=None, jira_token="t")
    with pytest.raises(RuntimeError, match="jira_host"):
        JiraClient.from_settings(s)


def test_jira_client_from_settings_missing_token(clean_env):
    s = load_settings(
        yaml_paths=[], env_file=None,
        jira_host="jira.example.com",
    )
    with pytest.raises(RuntimeError, match="No Jira token"):
        JiraClient.from_settings(s)


def test_jira_client_from_settings_ignores_legacy_jira_pat(clean_env):
    """Single-canonical-name policy: JIRA_PAT must NOT populate jira_token."""
    clean_env.setenv("JIRA_HOST", "jira.example.com")
    clean_env.setenv("JIRA_PAT", "legacy-pat")
    s = load_settings(yaml_paths=[], env_file=None)
    with pytest.raises(RuntimeError, match="No Jira token"):
        JiraClient.from_settings(s)


# ----------------------------------------------------------------------
# OpenAICompatProvider.from_settings
# ----------------------------------------------------------------------

def test_openai_compat_from_settings_happy_path(clean_env):
    s = load_settings(
        yaml_paths=[], env_file=None,
        nvidia_api_key="k",
        nvidia_base_url="https://example/v1",
    )
    provider = OpenAICompatProvider.from_settings(s)
    # The openai SDK exposes the values via the underlying client.
    assert provider._client.api_key == "k"
    assert str(provider._client.base_url).startswith("https://example/v1")


def test_openai_compat_from_settings_missing_key(clean_env):
    s = load_settings(yaml_paths=[], env_file=None)
    with pytest.raises(RuntimeError, match="No LLM API key"):
        OpenAICompatProvider.from_settings(s)


def test_openai_compat_from_settings_ignores_legacy_alias(clean_env):
    """Single-canonical-name policy: NVIDIA_LLM_API_KEY must NOT populate
    nvidia_api_key."""
    clean_env.setenv("NVIDIA_LLM_API_KEY", "legacy-llm")
    s = load_settings(yaml_paths=[], env_file=None)
    with pytest.raises(RuntimeError, match="No LLM API key"):
        OpenAICompatProvider.from_settings(s)


# ----------------------------------------------------------------------
# AnthropicProvider.from_settings
# ----------------------------------------------------------------------

def test_anthropic_from_settings_happy_path(clean_env):
    s = load_settings(
        yaml_paths=[], env_file=None,
        anthropic_api_key="ant-key",
    )
    provider = AnthropicProvider.from_settings(s)
    assert provider._client.api_key == "ant-key"


def test_anthropic_from_settings_missing_key(clean_env):
    s = load_settings(yaml_paths=[], env_file=None)
    with pytest.raises(RuntimeError, match="No Anthropic key"):
        AnthropicProvider.from_settings(s)


def test_anthropic_from_settings_ignores_legacy_alias(clean_env):
    """Single-canonical-name policy: ANTHROPIC_AUTH_TOKEN is ignored."""
    clean_env.setenv("ANTHROPIC_AUTH_TOKEN", "legacy")
    s = load_settings(yaml_paths=[], env_file=None)
    with pytest.raises(RuntimeError, match="No Anthropic key"):
        AnthropicProvider.from_settings(s)
