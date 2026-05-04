"""Layered configuration loading.

Layering (later wins):
1. configs/default.yaml shipped with the package
2. ~/.config/f2j/config.yaml (POSIX) or %APPDATA%\\f2j\\config.yaml (Windows)
3. ./f2j.yaml (project-local)
4. --config <path> (explicit)
5. F2J_* env vars (nested via __ delimiter)
6. CLI flags (applied at call site, not here)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv(override=False)


class AnthropicConfig(BaseModel):
    model: str = "claude-sonnet-4-6"
    enable_prompt_caching: bool = True
    # Override the SDK default `https://api.anthropic.com`. Set this to route
    # through an Anthropic-compatible internal proxy (NVIDIA MaaS, AWS Bedrock,
    # etc.). Per-operator: each user can set this in their own `.env` or
    # `~/.config/f2j/config.yaml` without touching the shared project repo.
    base_url: str | None = None
    # Name of the env var holding the bearer token, when not using the default
    # `ANTHROPIC_API_KEY`. Some proxies expect `ANTHROPIC_AUTH_TOKEN` or a
    # team-specific name. Only the *name* lives in config; the token itself
    # always comes from env (never YAML).
    auth_token_env: str | None = None


class EnrichmentConfig(BaseModel):
    concurrency: int = 8
    max_turns: int = 20
    per_bug_timeout_seconds: int = 360
    fix_proposals: str = "strip"
    warn_on_fix_suggestions: bool = True
    max_budget_usd: float | None = None
    context_docs: dict[str, list[str]] = Field(default_factory=dict)
    # Which agent backend drives enrichment:
    #   "anthropic"          → Anthropic Messages API (the original path).
    #   "openai_compatible"  → OpenAI Chat Completions protocol; works against
    #                          any compatible endpoint (NVIDIA Inference Hub,
    #                          Azure OpenAI, vLLM, LocalAI, etc.). See the
    #                          `openai_compatible` block below for endpoint
    #                          configuration.
    provider: str = "anthropic"


class OpenAICompatibleConfig(BaseModel):
    """Endpoint configuration for the openai_compatible provider.

    The token itself never lives in YAML — only the env-var *name* does. That
    keeps the project repo shareable; each operator drops their token in `.env`.

    The base URL can also come from env (via ``base_url_env``) so an operator
    behind a different region/proxy doesn't need to edit f2j.yaml.
    Resolution order for the URL: ``base_url_env`` (if set and the env var has
    a value) → ``base_url`` literal → SDK default.
    """

    base_url: str = "https://api.openai.com/v1"
    base_url_env: str | None = None  # optional: read the URL from this env var
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "gpt-4o"
    temperature: float = 0.0
    max_tokens_per_turn: int = 4096


class SubtaskTemplate(BaseModel):
    """One subtask to auto-create alongside each newly-created bug ticket.

    Subtasks are only created when the bug ticket itself is newly created.
    On idempotent-skip (parent already exists), subtasks are NOT re-created
    or backfilled — they were created with the parent on first upload.

    Fields:
      - ``title``: subtask summary (e.g. ``"[QA] - Testing"``).
      - ``assignee``: literal Jira username for this subtask, OR ``None`` if
        ``inherit_assignee`` is true. Pass an SSO short name (``"nlederman"``),
        not a display name.
      - ``inherit_assignee``: if true, use the parent ticket's resolved
        assignee. Mutually exclusive with a non-null ``assignee``.
      - ``issue_type``: Jira issue type, default ``"Sub-task"`` (standard JIRA
        subtask name; override if your project uses ``"Sub Task"`` etc.).
      - ``description``: optional body text. Empty by default.
    """

    title: str
    assignee: str | None = None
    inherit_assignee: bool = False
    issue_type: str = "Sub-task"
    description: str = ""


class EpicEntry(BaseModel):
    """One row of the operator-curated epic candidate list.

    Exposed to the enrichment agent so it can pick a contextually-appropriate
    epic per bug. Keeping the list operator-curated (rather than auto-fetched
    from Jira) prevents the agent from inventing keys and keeps the enrich
    path Jira-agnostic.
    """

    key: str
    summary: str


class RepoCacheConfig(BaseModel):
    dir: str | None = None
    max_size_gb: int = 5
    clone_depth: int = 1


class HttpsTokenConfig(BaseModel):
    askpass_helper: bool = True


class GlabAuthConfig(BaseModel):
    hostname: str = "gitlab-master.nvidia.com"


class GitAuthConfig(BaseModel):
    ca_bundle: str | None = None
    https_token: HttpsTokenConfig = Field(default_factory=HttpsTokenConfig)
    glab: GlabAuthConfig = Field(default_factory=GlabAuthConfig)


class RepoAlias(BaseModel):
    url: str
    auth: str = "glab"
    default_branch: str = "main"
    token_env: str | None = None


class JiraConfig(BaseModel):
    url: str | None = None
    project_key: str | None = None
    issue_type: str = "Bug"
    board_id: int | None = None
    # Auth dispatch:
    #   "bearer" → Server / Data Center; uses `Authorization: Bearer <PAT>`.
    #   "basic"  → Cloud; uses `Authorization: Basic base64(email:token)` and
    #              requires `user_email` to be set.
    auth_mode: str = "bearer"
    user_email: str | None = None
    default_assignee: str | None = None
    default_components: list[str] = Field(default_factory=list)
    default_labels: list[str] = Field(default_factory=lambda: ["from-md", "auto-created"])
    unknown_assignee_policy: str = "default"
    field_map: dict[str, dict[str, str]] = Field(default_factory=dict)
    priority_values: dict[str, str] = Field(
        default_factory=lambda: {
            "P0": "Highest",
            "P1": "High",
            "P2": "Medium",
            "P3": "Low",
        }
    )
    user_map_path: str = "configs/user_map.yaml"
    module_to_component: dict[str, str] = Field(default_factory=dict)
    # H2 module name (i.e. repo_alias) → assignee. Values are typically display
    # names ("Yair Sadan") that resolve through user_map.yaml or via Jira's
    # /user/search endpoint. They can also be raw Jira usernames if you know them.
    # Lookup order at upload time:
    #   1. EnrichedBug.assignee_hint  (set by the agent)
    #   2. ParsedBug.hinted_assignee  (from the input markdown)
    #   3. module_to_assignee[repo_alias]
    #   4. default_assignee
    module_to_assignee: dict[str, str] = Field(default_factory=dict)
    external_id_field: str | None = None
    external_id_label_prefix: str = "f2j-id"
    ca_bundle: str | None = None
    concurrency: int = 8
    # Epic Link wiring. The enrichment agent picks one entry from
    # ``available_epics`` per bug; the uploader writes that key to
    # ``epic_link_field``. Falls back to ``default_epic`` if the agent
    # didn't pick or picked a key not in the list.
    epic_link_field: str | None = None
    default_epic: str | None = None
    available_epics: list[EpicEntry] = Field(default_factory=list)
    # Deterministic epic routing. Applied before falling back to the LLM's
    # `enriched.epic_key`. Order of priority at upload time:
    #   1. external_id_prefix_to_epic[<longest matching prefix>]
    #   2. module_to_epic[<repo_alias>]
    #   3. enriched.epic_key (if it's in available_epics)
    #   4. default_epic
    # Prefix matching tries the longest configured prefix first, so
    # `CORE-MOBILE-` beats `CORE-` if both are listed.
    external_id_prefix_to_epic: dict[str, str] = Field(default_factory=dict)
    module_to_epic: dict[str, str] = Field(default_factory=dict)
    # Subtasks auto-created alongside each newly-created bug ticket.
    # Empty list = no subtasks. See SubtaskTemplate for field semantics.
    subtasks: list[SubtaskTemplate] = Field(default_factory=list)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "console"
    log_dir: str = ".logs"


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="F2J_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)
    openai_compatible: OpenAICompatibleConfig = Field(
        default_factory=OpenAICompatibleConfig
    )
    enrichment: EnrichmentConfig = Field(default_factory=EnrichmentConfig)
    repo_cache: RepoCacheConfig = Field(default_factory=RepoCacheConfig)
    git_auth: GitAuthConfig = Field(default_factory=GitAuthConfig)
    repo_aliases: dict[str, RepoAlias] = Field(default_factory=dict)
    jira: JiraConfig = Field(default_factory=JiraConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Documented precedence (later wins): YAML → env vars → CLI flags.
        # pydantic-settings exposes the inverse list (highest first), so put
        # env_settings BEFORE init_settings (which is where YAML lives).
        return (
            env_settings,
            dotenv_settings,
            init_settings,
            file_secret_settings,
        )


SECRET_ENV_VARS: dict[str, str] = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "jira_pat": "JIRA_PAT",
}


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _user_config_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "f2j"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file {path} must contain a YAML mapping at the root.")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Override wins. Nested dicts merged recursively. Lists replace."""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def config_paths(explicit: Path | None) -> list[Path]:
    """Return config file paths in load order (later wins). Existence not checked here."""
    paths = [_project_root() / "configs" / "default.yaml"]
    paths.append(_user_config_dir() / "config.yaml")
    paths.append(Path.cwd() / "f2j.yaml")
    if explicit is not None:
        paths.append(explicit)
    return paths


def load_config(explicit: Path | None = None) -> AppConfig:
    """Load layered configuration."""
    merged: dict[str, Any] = {}
    for p in config_paths(explicit):
        merged = _deep_merge(merged, _read_yaml(p))
    return AppConfig(**merged)


def redact_secret(value: str | None) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}...{value[-2:]}"


def get_secret(name: str) -> str | None:
    return os.environ.get(name)
