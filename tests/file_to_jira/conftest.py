"""Shared test fixtures."""

from __future__ import annotations

import uuid

import pytest

from file_to_jira.models import (
    BugRecord,
    BugStage,
    EnrichedBug,
    EnrichmentMeta,
    IntermediateFile,
    ModuleContext,
    ParsedBug,
)


@pytest.fixture
def parsed_bug() -> ParsedBug:
    return ParsedBug(
        bug_id="0123456789abcdef",
        external_id="CORE-CHAT-026",
        source_line_start=42,
        source_line_end=58,
        raw_title="Multi-domain two-skill section composer",
        raw_body="**What's broken:** test posts a query that classifies into two skills...",
        hinted_priority="P0",
        hinted_repos=["_core"],
        labels=["timeout-suspected-stale"],
        inherited_module=ModuleContext(
            repo_alias="_core",
            branch="2026-05-03_Auto_Fixes",
            commit_sha="b7801f5",
        ),
        closed_section=False,
    )


@pytest.fixture
def enriched_bug() -> EnrichedBug:
    return EnrichedBug(
        bug_id="0123456789abcdef",
        summary="Multi-domain two-skill section composer fails to emit per-skill SSE events",
        description_md="The orchestrator does not emit per-skill SSE events as documented.",
        priority="P0",
        enrichment_meta=EnrichmentMeta(
            model="claude-sonnet-4-6",
            started_at="2026-05-03T12:00:00+00:00",
            finished_at="2026-05-03T12:01:30+00:00",
            tool_calls=4,
            input_tokens=3500,
            output_tokens=800,
        ),
    )


@pytest.fixture
def bug_record(parsed_bug: ParsedBug) -> BugRecord:
    return BugRecord(stage=BugStage.PARSED, parsed=parsed_bug)


@pytest.fixture
def intermediate_file(bug_record: BugRecord) -> IntermediateFile:
    return IntermediateFile(
        run_id=str(uuid.uuid4()),
        source_file="examples/Bugs_For_Dev_Review_2026-05-03.md",
        source_file_sha256="0" * 64,
        bugs=[bug_record],
    )
