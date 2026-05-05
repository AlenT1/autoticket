"""Markdown bug-list parser.

State-machine over heading lines (regex-driven, line-by-line). Body-field
extraction inside each bug uses a second pass over its body text.

The format is documented in `schema.py`. Verification expectations against the
real sample (`Bugs_For_Dev_Review_2026-05-03.md`):
- 20 open bugs total (when `--include-resolved` is False)
- Split: 14 from Core / 2 from CentARB / 0 from CentProjects / 0 from centsdk / 4 from Cross-cutting
- Status labels: 13 timeout-suspected-stale + 7 real-product-gap
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from ..models import ModuleContext, ParsedBug
from ..util.ids import compute_bug_id


class ParseError(Exception):
    """Raised when the input file cannot be parsed."""


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

# CORE-CHAT-026, X-OBS-001, ARB-AUTH-001, etc.  Final segment is numeric.
BUG_ID_PATTERN = r"[A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)*-\d+"

_DASHES = r"[—–\-]+"  # em-dash, en-dash, hyphen

BUG_HEADING_RE = re.compile(
    rf"^(?P<hashes>#{{3,4}})\s+"
    rf"(?P<bug_id>{BUG_ID_PATTERN})\s+"
    rf"\[(?P<priority>P\d)\]\s*"
    rf"{_DASHES}\s*"
    rf"(?P<title>.+?)"
    rf"(?:\s+\((?P<status>[^()]+)\))?\s*$"
)

# ## Module: Core (`_core`, branch `2026-05-03_Auto_Fixes`, commit `b7801f5`)
MODULE_HEADING_RE = re.compile(
    r"^##\s+Module:\s+(?P<display>[^(]+?)\s*"
    r"\(\s*`(?P<repo_alias>[^`]+)`\s*,\s*"
    r"branch\s+`(?P<branch>[^`]+)`\s*,\s*"
    r"commit\s+`(?P<commit>[^`]+)`\s*\)\s*$"
)

CROSS_CUTTING_RE = re.compile(r"^##\s+Cross-cutting\b", re.IGNORECASE)
RECOMMENDED_RE = re.compile(r"^##\s+Recommended action\b", re.IGNORECASE)

CLOSED_SECTION_RE = re.compile(
    r"^###\s+(?:Fixed in this branch|Closed|Resolved)\b", re.IGNORECASE
)
OPEN_SECTION_RE = re.compile(
    r"^###\s+(?:Still open|Open|Need a real fix)\b", re.IGNORECASE
)

ANY_HEADING_RE = re.compile(r"^#{1,6}\s+")

# Bullet-field detection inside a bug body. Accepts both bold styles seen in the wild:
#   - **What's broken:** rest of line   (colon inside bold — actual sample style)
#   - **What's broken**: rest of line   (colon outside bold — alternate style)
FIELD_RE = re.compile(
    r"^\s*-\s*\*\*(?P<label>[^*:]+?):?\*\*:?\s*(?P<rest>.*)$"
)

# Backticked tokens; we extract these from field text to find file-path hints.
BACKTICK_TOKEN_RE = re.compile(r"`([^`]+)`")

# Test-ID list extractor.
TEST_ID_RE = re.compile(rf"\b{BUG_ID_PATTERN}\b")

# Heuristic for "looks like a file path" inside backticks.
_PATH_INDICATORS = ("/", "\\")
_PATH_EXTENSIONS = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".yaml", ".yml", ".md",
    ".json", ".toml", ".sql", ".go", ".java", ".rs", ".c", ".cpp",
    ".h", ".hpp", ".css", ".html", ".cfg", ".ini", ".sh", ".ps1",
)


# ---------------------------------------------------------------------------
# Decoding (UTF-8 first, fall back to CP-1252, then Latin-1)
# ---------------------------------------------------------------------------

def read_and_decode(path: Path) -> tuple[bytes, str]:
    """Read raw bytes and return (raw_bytes_bom_stripped, decoded_normalized)."""
    raw = Path(path).read_bytes()
    # Strip BOM if present.
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    # Normalize line endings before decoding so the decoded string is stable.
    raw = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

    last_err: Exception | None = None
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            text = unicodedata.normalize("NFC", text)
            return raw, text
        except UnicodeDecodeError as e:
            last_err = e
    # latin-1 cannot raise — defensive fallback.
    raise ParseError(f"Failed to decode {path}: {last_err}")


# ---------------------------------------------------------------------------
# Internal collector for an in-progress bug
# ---------------------------------------------------------------------------

@dataclass
class _PendingBug:
    external_id: str
    priority: str
    raw_title: str
    status_notes: str | None
    module: ModuleContext
    closed_section: bool
    source_line_start: int
    body_lines: list[str] = field(default_factory=list)
    source_line_end: int = 0

    def append_body(self, line_no: int, line: str) -> None:
        self.body_lines.append(line)
        self.source_line_end = line_no


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

@dataclass
class ParseResult:
    bugs: list[ParsedBug]
    source_sha256: str
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class _ParserState:
    """State-machine that walks markdown lines and accumulates ParsedBug records."""

    def __init__(self, source_sha256: str) -> None:
        self.source_sha256 = source_sha256
        self.current_module: ModuleContext | None = None
        self.in_cross_cutting = False
        self.in_recommended = False
        self.current_closed: bool | None = None
        self.pending: _PendingBug | None = None
        self.bugs: list[ParsedBug] = []
        self.warnings: list[str] = []

    def process_line(self, line_no: int, line: str) -> None:
        if self.in_recommended:
            return
        if self._try_h2(line):
            return
        if self._try_stage_marker(line):
            return
        if self._try_bug_heading(line, line_no):
            return
        if ANY_HEADING_RE.match(line):
            self._flush_pending()
            return
        if self.pending is not None:
            self.pending.append_body(line_no, line)

    def finalize(self) -> None:
        self._flush_pending()

    # ----- per-line dispatch helpers -----

    def _try_h2(self, line: str) -> bool:
        m_module = MODULE_HEADING_RE.match(line)
        if m_module:
            self._flush_pending()
            self.current_module = ModuleContext(
                repo_alias=m_module["repo_alias"],
                branch=m_module["branch"],
                commit_sha=m_module["commit"],
            )
            self.in_cross_cutting = False
            self.current_closed = None
            return True
        if CROSS_CUTTING_RE.match(line):
            self._flush_pending()
            self.current_module = None
            self.in_cross_cutting = True
            self.current_closed = False  # cross-cutting bugs are open by definition
            return True
        if RECOMMENDED_RE.match(line):
            self._flush_pending()
            self.in_recommended = True
            return True
        return False

    def _try_stage_marker(self, line: str) -> bool:
        if self.current_module is None or self.in_cross_cutting:
            return False
        if CLOSED_SECTION_RE.match(line):
            self._flush_pending()
            self.current_closed = True
            return True
        if OPEN_SECTION_RE.match(line):
            self._flush_pending()
            self.current_closed = False
            return True
        return False

    def _try_bug_heading(self, line: str, line_no: int) -> bool:
        m = BUG_HEADING_RE.match(line)
        if not m:
            return False
        self._flush_pending()
        closed = self._resolve_closed(m["bug_id"], line_no)
        self.pending = _PendingBug(
            external_id=m["bug_id"],
            priority=m["priority"],
            raw_title=m["title"].strip(),
            status_notes=m["status"],
            module=self.current_module or ModuleContext(),
            closed_section=closed,
            source_line_start=line_no,
            source_line_end=line_no,
        )
        return True

    def _resolve_closed(self, external_id: str, line_no: int) -> bool:
        if self.in_cross_cutting:
            return False
        if self.current_closed is None:
            self.warnings.append(
                f"line {line_no}: bug {external_id} appears outside a stage subsection; "
                "treating as open."
            )
            return False
        return self.current_closed

    def _flush_pending(self) -> None:
        if self.pending is None:
            return
        _build_parsed_bug(self.pending, self.source_sha256, self.bugs)
        self.pending = None


def parse_markdown(content: str, *, source_sha256: str) -> ParseResult:
    """Parse a decoded markdown bug list into ParsedBug records.

    Returns *all* bugs (both closed and open). Filtering by `closed_section` is
    the caller's responsibility (driven by the `--include-resolved` CLI flag).
    """
    state = _ParserState(source_sha256)
    for i, line in enumerate(content.splitlines()):
        state.process_line(i + 1, line)
    state.finalize()
    return ParseResult(
        bugs=state.bugs, source_sha256=source_sha256, warnings=state.warnings
    )


# ---------------------------------------------------------------------------
# Body-field extraction
# ---------------------------------------------------------------------------

# Canonical labels we recognize.
_LABEL_BROKEN = "whats_broken"
_LABEL_HYPOTHESIS = "hypothesis"
_LABEL_AFFECTED_FILES = "affected_files"
_LABEL_NEEDED = "whats_needed"          # FIX-PROPOSAL
_LABEL_CHANGED = "what_was_changed"      # FIX-PROPOSAL
_LABEL_TESTS_CLOSED = "tests_it_closed"


def _normalize_label(raw: str) -> str:
    s = raw.lower().replace("’", "'")  # smart apostrophe
    s = re.sub(r"\s+", " ", s).strip().rstrip(":")
    if "broken" in s and "what" in s:
        return _LABEL_BROKEN
    if s == "hypothesis":
        return _LABEL_HYPOTHESIS
    if "affected files" in s:
        return _LABEL_AFFECTED_FILES
    if "needed" in s and "what" in s:
        return _LABEL_NEEDED
    if "changed" in s and "what" in s:
        return _LABEL_CHANGED
    if "tests it closed" in s:
        return _LABEL_TESTS_CLOSED
    return s.replace(" ", "_")


def _extract_fields(body_lines: list[str]) -> dict[str, str]:
    """Walk body lines and return a dict of {canonical_label: text}."""
    fields: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []

    def commit() -> None:
        nonlocal buf, current
        if current and buf:
            text = "\n".join(buf).strip()
            if text:
                fields[current] = text
        buf = []

    for line in body_lines:
        m = FIELD_RE.match(line)
        if m:
            commit()
            current = _normalize_label(m["label"])
            rest = m["rest"]
            buf = [rest] if rest else []
        elif current and line.strip().startswith("- "):
            # New top-level bullet that isn't a labeled field — ends the prior field.
            commit()
            current = None
            buf = []
        elif current is not None:
            buf.append(line)

    commit()
    return fields


_PATH_DISQUALIFIERS = frozenset(' \t()"\'{}<>@')


def _looks_like_path(token: str) -> bool:
    """Heuristic: does this backtick token look like a file path?

    Reject:
    - Tokens containing whitespace, parens, quotes, braces, angle brackets, @
      (these indicate code/URL/decorator syntax: `@app.post(...)`, `POST /arb-types`).
    - Bare URL paths like `/arb-types` that don't end with a file extension.
    """
    t = token.strip()
    if not t:
        return False
    if any(c in _PATH_DISQUALIFIERS for c in t):
        return False
    has_sep = any(sep in t for sep in _PATH_INDICATORS)
    has_ext = any(t.endswith(ext) for ext in _PATH_EXTENSIONS)
    # Bare URL endpoints (start with `/`) without a file extension are not paths.
    if t.startswith("/") and not has_ext:
        return False
    return has_sep or has_ext


def _extract_file_hints(*field_texts: str) -> list[str]:
    seen: list[str] = []
    seen_set: set[str] = set()
    for text in field_texts:
        if not text:
            continue
        for tok in BACKTICK_TOKEN_RE.findall(text):
            tok = tok.strip()
            if _looks_like_path(tok) and tok not in seen_set:
                seen.append(tok)
                seen_set.add(tok)
    return seen


def _parse_test_ids(text: str) -> list[str]:
    if not text:
        return []
    seen: list[str] = []
    seen_set: set[str] = set()
    for m in TEST_ID_RE.findall(text):
        if m not in seen_set:
            seen.append(m)
            seen_set.add(m)
    return seen


def _status_notes_to_labels(status_notes: str | None) -> list[str]:
    """Turn a parenthesized status-notes string into Jira labels.

    Canonical recognizers fire first (e.g. `TIMEOUT, likely stale stack` →
    `timeout-suspected-stale`); their constituent phrases are then suppressed
    from the per-piece slug pass to avoid duplicates like `real-product-gap`
    plus `real-gap` for the same source text.
    """
    if not status_notes:
        return []

    stripped = status_notes.strip()
    upper = stripped.upper()
    labels: list[str] = []
    consumed: set[str] = set()  # lowercased pieces already covered by canonical

    # The sample uses `(TIMEOUT)` for cross-cutting bugs and
    # `(TIMEOUT, likely stale stack)` for module bugs; the doc's prose treats
    # both as "stack staleness suspected", so we collapse to one label.
    if "TIMEOUT" in upper:
        labels.append("timeout-suspected-stale")
        consumed.add("timeout")
        if "STALE" in upper:
            consumed.add("likely stale stack")
    if "REAL GAP" in upper:
        labels.append("real-product-gap")
        consumed.add("real gap")

    for piece in (p.strip() for p in stripped.split(",")):
        if not piece or piece.lower() in consumed:
            continue
        slug = _slugify_label(piece)
        if slug and slug not in labels:
            labels.append(slug)
    return labels


def _slugify_label(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s


# ---------------------------------------------------------------------------
# Pending → ParsedBug
# ---------------------------------------------------------------------------

def _trim_trailing(body_lines: list[str]) -> list[str]:
    """Drop trailing blank lines and `---` separators from the bug's body."""
    out = list(body_lines)
    while out and (out[-1].strip() == "" or out[-1].strip() == "---"):
        out.pop()
    return out


def _build_parsed_bug(
    pending: _PendingBug,
    source_sha256: str,
    bugs: list[ParsedBug],
) -> None:
    body_lines = _trim_trailing(pending.body_lines)
    raw_body = "\n".join(body_lines).strip()

    fields = _extract_fields(body_lines)

    labels = _status_notes_to_labels(pending.status_notes)

    hinted_files = _extract_file_hints(
        fields.get(_LABEL_AFFECTED_FILES, ""),
        fields.get(_LABEL_BROKEN, ""),
        fields.get(_LABEL_HYPOTHESIS, ""),
    )

    hinted_repos: list[str] = []
    if pending.module.repo_alias:
        hinted_repos.append(pending.module.repo_alias)

    removed_parts: list[str] = []
    if _LABEL_NEEDED in fields:
        removed_parts.append(f"What's needed: {fields[_LABEL_NEEDED]}")
    if _LABEL_CHANGED in fields:
        removed_parts.append(f"What was changed: {fields[_LABEL_CHANGED]}")
    removed_fix_text = "\n\n".join(removed_parts) if removed_parts else None

    test_ids: list[str] = []
    if _LABEL_TESTS_CLOSED in fields:
        test_ids = _parse_test_ids(fields[_LABEL_TESTS_CLOSED])

    bug_id = compute_bug_id(
        source_sha256, pending.raw_title, pending.source_line_start
    )

    bugs.append(
        ParsedBug(
            bug_id=bug_id,
            external_id=pending.external_id,
            source_line_start=pending.source_line_start,
            source_line_end=max(pending.source_line_end, pending.source_line_start),
            raw_title=pending.raw_title,
            raw_body=raw_body,
            hinted_priority=pending.priority,
            hinted_repos=hinted_repos,
            hinted_files=hinted_files,
            labels=labels,
            inherited_module=pending.module,
            closed_section=pending.closed_section,
            removed_fix_text=removed_fix_text,
            affected_test_ids=test_ids,
        )
    )
