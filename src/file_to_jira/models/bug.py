"""Pydantic models for bug records and their lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class BugStage(str, Enum):
    PARSED = "parsed"
    ENRICHING = "enriching"
    ENRICHED = "enriched"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    FAILED = "failed"


class ModuleContext(BaseModel):
    """Context inherited from a `## Module:` H2 heading. Applies to every bug under it."""

    model_config = ConfigDict(frozen=True)

    repo_alias: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    repo_url: str | None = None  # resolved at enrichment time, not parse time


class CodeReference(BaseModel):
    repo_alias: str
    repo_url: str | None = None
    file_path: str  # POSIX-style, repo-relative
    line_start: int | None = None
    line_end: int | None = None
    commit_sha: str | None = None
    snippet: str | None = None
    blame_author: str | None = None
    blame_date: str | None = None


class ReproStep(BaseModel):
    order: int = Field(ge=1)
    text: str = Field(min_length=1)


class ParsedBug(BaseModel):
    """Output of phase 1a: raw extraction from the input file."""

    bug_id: str  # internal stable hash; see util.ids.compute_bug_id
    external_id: str | None = None  # e.g. "CORE-CHAT-026"
    source_line_start: int = Field(ge=1)
    source_line_end: int = Field(ge=1)
    raw_title: str
    raw_body: str
    hinted_priority: str | None = None
    hinted_assignee: str | None = None
    hinted_repos: list[str] = Field(default_factory=list)
    hinted_components: list[str] = Field(default_factory=list)
    hinted_files: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    inherited_module: ModuleContext = Field(default_factory=ModuleContext)
    closed_section: bool = False
    removed_fix_text: str | None = None  # stripped What's needed: / What was changed: blocks
    affected_test_ids: list[str] = Field(default_factory=list)


class EnrichmentMeta(BaseModel):
    model: str
    started_at: str
    finished_at: str
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    repos_touched: list[str] = Field(default_factory=list)
    truncated: bool = False
    prompt_hash: str | None = None  # sha256 of the system prompt text


class EnrichedBug(BaseModel):
    bug_id: str
    summary: str = Field(min_length=1, max_length=255)  # Jira summary cap
    description_md: str = Field(min_length=1)
    reproduction_steps: list[ReproStep] = Field(default_factory=list)
    expected_behavior: str | None = None
    actual_behavior: str | None = None
    code_references: list[CodeReference] = Field(default_factory=list)
    relevant_logs: str | None = None
    priority: str
    assignee_hint: str | None = None
    components: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    affected_versions: list[str] = Field(default_factory=list)
    epic_key: str | None = None
    enrichment_meta: EnrichmentMeta


class UploadResult(BaseModel):
    jira_key: str
    jira_url: str
    uploaded_at: str
    response_etag: str | None = None


class BugError(BaseModel):
    stage: BugStage
    message: str
    traceback: str | None = None
    # Optional taxonomy: rate_limit / overload / context_limit / unknown.
    # Drives orchestrator retry decisions.
    failure_class: str | None = None
    occurred_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class BugRecord(BaseModel):
    """Composite record holding all stages of one bug."""

    stage: BugStage = BugStage.PARSED
    parsed: ParsedBug
    enriched: EnrichedBug | None = None
    upload: UploadResult | None = None
    last_error: BugError | None = None
    attempts: int = 0
