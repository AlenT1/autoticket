"""Tests for Pydantic models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from file_to_jira.models import (
    BugRecord,
    BugStage,
    EnrichedBug,
    EnrichmentMeta,
    IntermediateFile,
    ModuleContext,
    ParsedBug,
    ReproStep,
)


def test_parsed_bug_minimal_valid() -> None:
    p = ParsedBug(
        bug_id="abc",
        source_line_start=1,
        source_line_end=2,
        raw_title="t",
        raw_body="b",
    )
    assert p.closed_section is False
    assert p.hinted_repos == []
    assert isinstance(p.inherited_module, ModuleContext)


def test_parsed_bug_rejects_zero_line_start() -> None:
    with pytest.raises(ValidationError):
        ParsedBug(
            bug_id="abc",
            source_line_start=0,  # ge=1
            source_line_end=2,
            raw_title="t",
            raw_body="b",
        )


def test_module_context_is_frozen() -> None:
    m = ModuleContext(repo_alias="_core", branch="main")
    with pytest.raises(ValidationError):
        m.repo_alias = "different"  # type: ignore[misc]


def test_repro_step_requires_order_ge_1() -> None:
    with pytest.raises(ValidationError):
        ReproStep(order=0, text="step")


def test_repro_step_requires_nonempty_text() -> None:
    with pytest.raises(ValidationError):
        ReproStep(order=1, text="")


def test_enriched_bug_summary_max_length() -> None:
    too_long = "x" * 256
    with pytest.raises(ValidationError):
        EnrichedBug(
            bug_id="abc",
            summary=too_long,
            description_md="ok",
            priority="P1",
            enrichment_meta=EnrichmentMeta(
                model="m", started_at="t", finished_at="t"
            ),
        )


def test_enriched_bug_summary_at_limit() -> None:
    at_limit = "x" * 255
    e = EnrichedBug(
        bug_id="abc",
        summary=at_limit,
        description_md="ok",
        priority="P1",
        enrichment_meta=EnrichmentMeta(model="m", started_at="t", finished_at="t"),
    )
    assert len(e.summary) == 255


def test_bug_record_default_stage_parsed(parsed_bug: ParsedBug) -> None:
    r = BugRecord(parsed=parsed_bug)
    assert r.stage == BugStage.PARSED
    assert r.enriched is None
    assert r.upload is None
    assert r.attempts == 0


def test_intermediate_file_round_trip(intermediate_file: IntermediateFile) -> None:
    payload = intermediate_file.model_dump_json()
    restored = IntermediateFile.model_validate_json(payload)
    assert restored.run_id == intermediate_file.run_id
    assert restored.source_file_sha256 == intermediate_file.source_file_sha256
    assert len(restored.bugs) == 1
    assert restored.bugs[0].parsed.bug_id == intermediate_file.bugs[0].parsed.bug_id
    assert restored.bugs[0].parsed.inherited_module.repo_alias == "_core"


def test_intermediate_file_round_trip_with_enriched(
    intermediate_file: IntermediateFile, enriched_bug: EnrichedBug
) -> None:
    intermediate_file.bugs[0].enriched = enriched_bug
    intermediate_file.bugs[0].stage = BugStage.ENRICHED
    payload = intermediate_file.model_dump_json()
    restored = IntermediateFile.model_validate_json(payload)
    assert restored.bugs[0].stage == BugStage.ENRICHED
    assert restored.bugs[0].enriched is not None
    assert restored.bugs[0].enriched.priority == "P0"


def test_intermediate_file_touch_updates_timestamp(
    intermediate_file: IntermediateFile,
) -> None:
    before = intermediate_file.updated_at
    intermediate_file.touch()
    # timestamps are ISO strings; we compare lexicographically (UTC, fixed format).
    assert intermediate_file.updated_at >= before


def test_bug_stage_string_serialization() -> None:
    # Stages serialize as their string values for JSON-friendliness.
    payload = BugStage.UPLOADED.value
    assert payload == "uploaded"
