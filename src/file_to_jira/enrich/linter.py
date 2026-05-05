"""Post-hoc fix-language linter.

Scans an EnrichedBug's `description_md` for prescriptive phrases. The pattern
set is tuned against the real sample (`Bugs_For_Dev_Review_2026-05-03.md`) but
the goal is general "no fix proposals in bug descriptions".

Three modes (config: `enrichment.fix_proposals`):
- `strip`: remove offending lines and warn.
- `reframe`: leave content; mark lines for the agent to rewrite (Phase-2).
- `keep`: warn only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Phrases that strongly imply prescriptive fix language.
# Each entry is matched case-insensitively against a single line.
_FIX_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bshould\s+(?:be|change|add|call|return|become|wrap|set)\b", re.I),
    re.compile(r"\bthe\s+fix\s+is\b", re.I),
    re.compile(r"\bfix(?:es)?:\s", re.I),
    re.compile(r"\bto\s+fix\s+this\b", re.I),
    re.compile(r"\bchange\s+\w+\s+to\b", re.I),
    re.compile(r"\bwrap\s+\w*?\s*(?:with|using)\b", re.I),
    re.compile(r"\bwe\s+(?:should|must|need\s+to)\b", re.I),
    re.compile(r"\bI\s+recommend\b", re.I),
    re.compile(r"\bthe\s+correct\s+approach\b", re.I),
    re.compile(r"\badd\s+a\s+`?Depends\(", re.I),
    re.compile(r"\badd\s+(?:a\s+)?(?:check|validation|guard)\b", re.I),
    re.compile(r"\brefactor\b", re.I),
    re.compile(r"\breplace\s+\w+\s+with\b", re.I),
    re.compile(r"\bset\s+\w[\w.]*\s*=\s*", re.I),
]


@dataclass
class LintResult:
    cleaned: str
    stripped_lines: list[str]
    flagged_lines: list[str]


def _line_offends(line: str) -> bool:
    return any(p.search(line) for p in _FIX_PATTERNS)


@dataclass
class _FenceState:
    in_fence: bool = False
    marker: str | None = None

    def update(self, line_lstripped: str) -> bool:
        """Mutate state for one line; return True if this line is inside a fence."""
        if not self.in_fence:
            if line_lstripped.startswith(("```", "~~~")):
                self.in_fence = True
                self.marker = line_lstripped[:3]
                return True
            return False
        # Inside a fence — check for closing.
        if line_lstripped.startswith(self.marker or "```"):
            self.in_fence = False
            self.marker = None
        return True


def lint_description(text: str, *, mode: str = "strip") -> LintResult:
    """Scan `text` line-by-line for fix-proposal phrases.

    Returns a LintResult with the cleaned text and the list of offending lines.
    Lines inside fenced code blocks are not scanned (so a copied snippet that
    happens to contain "fix:" doesn't get clobbered).
    """
    if mode not in {"strip", "reframe", "keep"}:
        raise ValueError(f"unknown mode {mode!r}")

    out_lines: list[str] = []
    stripped: list[str] = []
    flagged: list[str] = []
    fence = _FenceState()

    for line in text.splitlines():
        if fence.update(line.lstrip()):
            out_lines.append(line)
            continue
        if _line_offends(line):
            flagged.append(line)
            if mode == "strip":
                stripped.append(line)
                continue
        out_lines.append(line)

    cleaned = "\n".join(out_lines)
    if text.endswith("\n") and not cleaned.endswith("\n"):
        cleaned += "\n"
    return LintResult(cleaned=cleaned, stripped_lines=stripped, flagged_lines=flagged)
