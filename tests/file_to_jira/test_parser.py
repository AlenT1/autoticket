"""Tests for the markdown bug-list parser.

Verification baseline: the real sample
`examples/Bugs_For_Dev_Review_2026-05-03.md` is treated as the canonical
fixture. If you re-edit that file, expected counts here must move with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from file_to_jira.parse import parse_markdown, read_and_decode
from file_to_jira.parse.markdown_parser import (
    _extract_fields,
    _extract_file_hints,
    _looks_like_path,
    _normalize_label,
    _parse_test_ids,
    _slugify_label,
    _status_notes_to_labels,
    BUG_HEADING_RE,
)
from file_to_jira.util.ids import file_sha256_bytes


def _find_sample() -> Path | None:
    """Locate the bug-review sample. Tolerates filename drift (case, separators,
    date), so a re-downloaded sample doesn't silently skip every assertion."""
    candidates = list(Path("examples").glob("[bB]ugs_[fF]or_[dD]ev_[rR]eview*.md"))
    return candidates[0] if candidates else None


SAMPLE_PATH = _find_sample() or Path("examples/Bugs_For_Dev_Review_2026-05-03.md")


# ---------------------------------------------------------------------------
# Heading regex unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line, expected",
    [
        (
            "#### CORE-CHAT-026 [P0] — Multi-domain composer (TIMEOUT, likely stale stack)",
            ("CORE-CHAT-026", "P0", "Multi-domain composer", "TIMEOUT, likely stale stack"),
        ),
        (
            "#### CORE-MOBILE-001 [P2] — Drawer sidebar at <768 px (REAL GAP)",
            ("CORE-MOBILE-001", "P2", "Drawer sidebar at <768 px", "REAL GAP"),
        ),
        (
            "### X-OBS-001 [P1] — Single chat populates 8 trace tables (TIMEOUT)",
            ("X-OBS-001", "P1", "Single chat populates 8 trace tables", "TIMEOUT"),
        ),
        (
            "#### ARB-AUTH-001 [P0] — `arb:sme` cannot access endpoints (REAL GAP)",
            ("ARB-AUTH-001", "P0", "`arb:sme` cannot access endpoints", "REAL GAP"),
        ),
    ],
)
def test_bug_heading_regex(line: str, expected: tuple[str, str, str, str]) -> None:
    m = BUG_HEADING_RE.match(line)
    assert m is not None, f"Did not match: {line!r}"
    assert m["bug_id"] == expected[0]
    assert m["priority"] == expected[1]
    assert m["title"].strip() == expected[2]
    assert m["status"] == expected[3]


def test_bug_heading_regex_rejects_fix_descriptions() -> None:
    # `#### Fix C-1: ...` has no [P\d] bracket and shouldn't match.
    assert BUG_HEADING_RE.match("#### Fix C-1: Multi-skill SSE event vocabulary") is None


# ---------------------------------------------------------------------------
# Field-label normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, canonical",
    [
        ("What's broken", "whats_broken"),
        ("What is broken", "whats_broken"),
        ("Hypothesis", "hypothesis"),
        ("Affected files", "affected_files"),
        ("Affected files (likely)", "affected_files"),
        ("What's needed", "whats_needed"),
        ("What was changed", "what_was_changed"),
        ("Tests it closed", "tests_it_closed"),
    ],
)
def test_normalize_label(raw: str, canonical: str) -> None:
    assert _normalize_label(raw) == canonical


# ---------------------------------------------------------------------------
# Field extraction (the bullet-style body shape)
# ---------------------------------------------------------------------------


def test_extract_fields_handles_colon_inside_bold() -> None:
    body = [
        "- **What's broken:** symptom text",
        "- **Hypothesis:** debugging guess",
        "- **Affected files (likely):** `path/a.py`, `path/b.py`",
    ]
    fields = _extract_fields(body)
    assert fields["whats_broken"] == "symptom text"
    assert fields["hypothesis"] == "debugging guess"
    assert "path/a.py" in fields["affected_files"]


def test_extract_fields_handles_colon_outside_bold() -> None:
    # Alternative style some authors use.
    body = ["- **What's broken**: symptom text"]
    fields = _extract_fields(body)
    assert fields["whats_broken"] == "symptom text"


# ---------------------------------------------------------------------------
# Path heuristic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token, expected",
    [
        ("apps/centarb_backend/app/main.py", True),
        ("src/components/Sidebar/*", True),
        ("/arb-types", False),                 # bare URL
        ('@app.post("/arb-types", ...)', False),  # decorator code
        ("POST /arb-types", False),            # has space
        ("_require_admin", False),             # bare identifier
        ("config.yaml", True),                 # bare file with extension
        ("foo bar/baz.py", False),             # has space
        ("", False),
    ],
)
def test_looks_like_path(token: str, expected: bool) -> None:
    assert _looks_like_path(token) is expected


def test_extract_file_hints_dedupes_and_filters() -> None:
    text = (
        "`apps/x.py` and `apps/x.py` again, plus `_require_admin` and "
        "`POST /api` and `apps/y.py`"
    )
    hints = _extract_file_hints(text)
    assert hints == ["apps/x.py", "apps/y.py"]


# ---------------------------------------------------------------------------
# Status notes → labels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "notes, expected",
    [
        # Both bare TIMEOUT and TIMEOUT+stale collapse to one canonical label.
        ("TIMEOUT, likely stale stack", ["timeout-suspected-stale"]),
        ("TIMEOUT", ["timeout-suspected-stale"]),
        ("REAL GAP", ["real-product-gap"]),
        ("TIMEOUT, foo bar", ["timeout-suspected-stale", "foo-bar"]),
        (None, []),
        ("", []),
    ],
)
def test_status_notes_to_labels(notes: str | None, expected: list[str]) -> None:
    assert _status_notes_to_labels(notes) == expected


def test_slugify_label() -> None:
    assert _slugify_label("Likely Stale Stack") == "likely-stale-stack"
    assert _slugify_label("  REAL GAP  ") == "real-gap"


# ---------------------------------------------------------------------------
# Test-ID parsing
# ---------------------------------------------------------------------------


def test_parse_test_ids_extracts_all() -> None:
    text = "CORE-CHAT-026, CORE-CHAT-027, CORE-CHAT-029, X-OBS-001"
    assert _parse_test_ids(text) == [
        "CORE-CHAT-026",
        "CORE-CHAT-027",
        "CORE-CHAT-029",
        "X-OBS-001",
    ]


def test_parse_test_ids_dedupes() -> None:
    assert _parse_test_ids("CORE-CHAT-026, CORE-CHAT-026") == ["CORE-CHAT-026"]


# ---------------------------------------------------------------------------
# End-to-end against the real sample
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sample_parse():
    if not SAMPLE_PATH.exists():
        pytest.skip(f"Real sample not present at {SAMPLE_PATH}")
    raw, decoded = read_and_decode(SAMPLE_PATH)
    sha = file_sha256_bytes(raw)
    return parse_markdown(decoded, source_sha256=sha)


def test_sample_total_count(sample_parse) -> None:
    """Every bug in the document parses (none silently dropped)."""
    assert len(sample_parse.bugs) == 20


def test_sample_all_open(sample_parse) -> None:
    """No bug headings appear under closed sections in this sample."""
    closed = [b for b in sample_parse.bugs if b.closed_section]
    assert closed == []


def test_sample_module_distribution(sample_parse) -> None:
    by_repo: dict[str, int] = {}
    for b in sample_parse.bugs:
        key = b.inherited_module.repo_alias or "<cross-cutting>"
        by_repo[key] = by_repo.get(key, 0) + 1
    assert by_repo == {
        "_core": 14,
        "vibe_coding/centarb": 2,
        "<cross-cutting>": 4,
    }


def test_sample_label_distribution(sample_parse) -> None:
    timeout = sum(1 for b in sample_parse.bugs if "timeout-suspected-stale" in b.labels)
    real_gap = sum(1 for b in sample_parse.bugs if "real-product-gap" in b.labels)
    assert timeout == 13
    assert real_gap == 7
    # Every bug carries exactly one of the two canonical status labels.
    assert timeout + real_gap == 20


def test_sample_priorities_extracted(sample_parse) -> None:
    for b in sample_parse.bugs:
        assert b.hinted_priority in {"P0", "P1", "P2", "P3"}, (
            f"{b.external_id}: priority={b.hinted_priority!r}"
        )


def test_sample_external_ids_round_trip(sample_parse) -> None:
    expected = {
        "CORE-CHAT-026", "CORE-CHAT-006", "CORE-CHAT-014", "CORE-CHAT-027",
        "CORE-CHAT-030", "CORE-ORC-004", "CORE-SSE-005", "CORE-SSE-002",
        "CORE-CHAT-031", "CORE-MOBILE-001", "CORE-MOBILE-002",
        "CORE-MOBILE-006", "CORE-MOBILE-007", "CORE-MOBILE-008",
        "ARB-AGENT-001", "ARB-AUTH-001",
        "X-OBS-001", "X-OBS-005", "X-PERF-002", "X-SEC-009",
    }
    actual = {b.external_id for b in sample_parse.bugs}
    assert actual == expected


def test_sample_module_inheritance_carries_branch_and_commit(sample_parse) -> None:
    """Bugs under `## Module: Core (...)` inherit branch and commit_sha."""
    core_bugs = [b for b in sample_parse.bugs if b.inherited_module.repo_alias == "_core"]
    assert core_bugs
    for b in core_bugs:
        assert b.inherited_module.branch == "2026-05-03_Auto_Fixes"
        assert b.inherited_module.commit_sha == "b7801f5"


def test_sample_cross_cutting_has_no_module(sample_parse) -> None:
    cc_bugs = [b for b in sample_parse.bugs if b.external_id.startswith("X-")]
    assert len(cc_bugs) == 4
    for b in cc_bugs:
        assert b.inherited_module.repo_alias is None
        assert b.inherited_module.branch is None


def test_sample_arb_auth_001_strips_whats_needed(sample_parse) -> None:
    bug = next(b for b in sample_parse.bugs if b.external_id == "ARB-AUTH-001")
    assert bug.removed_fix_text is not None
    assert "What's needed" in bug.removed_fix_text
    assert "Wrap `POST /arb-types`" in bug.removed_fix_text
    # The literal source body is preserved (raw_body); the agent prompt builder
    # is what does the strip downstream.
    assert "What's needed" in bug.raw_body


def test_sample_arb_auth_001_extracts_real_path_only(sample_parse) -> None:
    """Path heuristic must reject URL endpoints and decorator code."""
    bug = next(b for b in sample_parse.bugs if b.external_id == "ARB-AUTH-001")
    assert bug.hinted_files == ["apps/centarb_backend/app/main.py"]


def test_sample_bug_id_stable_across_reparse(sample_parse) -> None:
    """Re-parsing the same content yields identical bug_ids."""
    raw, decoded = read_and_decode(SAMPLE_PATH)
    sha = file_sha256_bytes(raw)
    second = parse_markdown(decoded, source_sha256=sha)
    first_ids = [b.bug_id for b in sample_parse.bugs]
    second_ids = [b.bug_id for b in second.bugs]
    assert first_ids == second_ids


def test_sample_no_warnings(sample_parse) -> None:
    """Well-formed sample should produce no parser warnings."""
    assert sample_parse.warnings == []


# ---------------------------------------------------------------------------
# Encoding fallback
# ---------------------------------------------------------------------------


def test_encoding_fallback_handles_cp1252(tmp_path: Path) -> None:
    # Em-dash 0x97 in CP-1252 is invalid as UTF-8.
    cp1252_bytes = b"# Title \x97 with em-dash\n\n## Module: Foo (`alias`, branch `b`, commit `c`)\n"
    p = tmp_path / "cp1252.md"
    p.write_bytes(cp1252_bytes)
    _raw, decoded = read_and_decode(p)
    assert "with em-dash" in decoded


def test_encoding_strips_bom(tmp_path: Path) -> None:
    p = tmp_path / "bom.md"
    p.write_bytes(b"\xef\xbb\xbf# Title\n")
    raw, decoded = read_and_decode(p)
    assert decoded.startswith("# Title")
    assert not raw.startswith(b"\xef\xbb\xbf")
