"""Documentation of the input markdown format the parser expects.

This module is documentation-as-code. The parser implementation lives in
`markdown_parser.py`; this file describes the rules a writer of the input MD
should follow (and which the parser enforces).

## Hierarchy

```
H1  document title (free-form; not parsed)
    prose / context / tables (not parsed)
H2  one of:
    "## Module: <Display> (`<repo_alias>`, branch `<branch>`, commit `<sha>`)"
        each module section contains H3 stage subsections
    "## Cross-cutting <anything>"
        bugs go directly under H3 (no stage subsection)
    "## Recommended action <anything>"
        closes parsing — anything afterwards is ignored
    "## How to read this" / any other H2
        ignored

H3 stage marker (only meaningful inside a module):
    "### Fixed in this branch" / "### Closed" / "### Resolved" → closed_section=True
    "### Still open" / "### Open" / "### Need a real fix"     → closed_section=False

Bug heading (inside a module's stage subsection at H4, OR at H3 directly under Cross-cutting):
    "#### <BUG_ID> [<P0..P3>] — <title> (<status notes>)"
    "### <BUG_ID> [<P0..P3>] — <title> (<status notes>)"  (cross-cutting)

Bug body (between two bug headings or between heading and the next H1..H4):
    "- **What's broken:** <text>"
    "- **Hypothesis:** <text>"
    "- **Affected files (likely):** `<path>`, `<path>` ..."
    "- **What's needed:** <text>"      ← FIX-PROPOSAL: stripped per config
    "- **What was changed:** <text>"   ← FIX-PROPOSAL: stripped per config
    "- **Tests it closed:** BUG-ID, BUG-ID, ..."
```

## Bug ID format

`<MODULE>-<AREA>(-<AREA>)*-<NNN>`, e.g. `CORE-CHAT-026`, `X-OBS-001`,
`ARB-AUTH-001`. The trailing numeric segment is required.

## Priority

`[P0]` through `[P9]`. Maps to Jira priority via `jira.priority_values`.

## Status notes

Free-form parenthesized list at end of the bug heading, comma-separated.
Each note becomes a slugified Jira label. A canonical recognizer maps known
phrases:

| Source phrase                         | Slugified label              |
| ------------------------------------- | ---------------------------- |
| "TIMEOUT, likely stale stack"         | "timeout-suspected-stale"    |
| "REAL GAP"                            | "real-product-gap"           |
| Anything else                         | slugified(lowercase)          |

## Module-level inheritance

Each H2 module heading exposes `(repo_alias, branch, commit_sha)` parsed from
its backtick-delimited fields. These are inherited as defaults by every bug
under that module.

## Cross-cutting

The "Cross-cutting" H2 implies open bugs (no stage subsection). The bugs
directly underneath are H3-level rather than H4-level.

## Encoding

The parser opens files with UTF-8 first, falls back to CP-1252, then to
Latin-1. All output is normalized to UTF-8 NFC. BOMs are stripped before
hashing for stable bug_id generation across re-saves.
"""

# This module intentionally contains no executable code.
__all__: list[str] = []
