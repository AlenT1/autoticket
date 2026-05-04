"""Preserve user-mutable state from the live Jira issue when the agent
rewrites a description.

Today the only state we preserve is **DoD checkbox marks**: when a
human ticks `[x]` (markdown) or `(x)` (Jira wiki) in a Jira issue, the
agent's next update_* must not silently flip those back to unchecked.

The agent always writes markdown, so the outgoing body is markdown.
The live body is whatever Jira holds — markdown or wiki, depending on
whether a human or the agent last edited it. We extract the *text* of
checked bullets from the live body (format-agnostic) and re-mark the
matching outgoing markdown bullets.

Match is fuzzy on the first 5 normalized content words: lowercased,
punctuation-stripped. Strict enough to avoid false positives, loose
enough to survive LLM paraphrasing of the bullet wording.
"""
from __future__ import annotations

import re

# Markdown checkbox: "- [x] text" / "- [ ] text" (any leading spaces).
_MD_CHECKBOX = re.compile(r"^\s*-\s*\[(?P<state>[x ])\]\s*(?P<text>.*?)\s*$")

# Jira wiki checkbox: the agent's MD->wiki converter renders [x] as
# (/) (green tick) and [ ] as (x) (red X). So (/) is "checked" in our
# context; (x) is "unchecked". Also accept (y) for completeness — it's
# Jira's alt syntax for the green tick.
_WIKI_CHECKBOX = re.compile(r"^\s*\*\s*\((?P<state>[/xy ]?)\)\s*(?P<text>.*?)\s*$")
_WIKI_CHECKED_STATES = frozenset({"/", "y"})

_PUNCT = re.compile(r"[^\w\s]")
_DOD_HEADING = re.compile(
    r"^\s*(#{1,6}\s*Definition of Done|h[1-6]\.\s*Definition of Done)\s*$",
    re.IGNORECASE,
)
_NEXT_HEADING = re.compile(r"^\s*(#{1,6}\s|h[1-6]\.\s)")


def preserve_dod_checkboxes(new_body: str, live_body: str) -> str:
    """Return `new_body` with `[ ]` -> `[x]` for any DoD bullet whose
    text fuzzy-matches a bullet that was checked in `live_body`.

    Both bodies are searched for their `### Definition of Done` /
    `h3. Definition of Done` block independently. If `live_body` has
    no DoD or no checked items, returns `new_body` unchanged.
    """
    checked_keys = _extract_checked_keys(live_body)
    if not checked_keys:
        return new_body
    return _apply_checkmarks_to_md(new_body, checked_keys)


def _extract_checked_keys(body: str) -> list[str]:
    """Normalized text (lowercased, punctuation-stripped) for each
    checked DoD bullet. Returned as a list so the caller can use
    prefix matching against new-body keys."""
    keys: list[str] = []
    for line in _dod_lines(body):
        m = _MD_CHECKBOX.match(line)
        if m and m.group("state").strip().lower() == "x":
            key = _normalize_key(m.group("text"))
            if key:
                keys.append(key)
            continue
        m = _WIKI_CHECKBOX.match(line)
        if m and m.group("state") in _WIKI_CHECKED_STATES:
            key = _normalize_key(m.group("text"))
            if key:
                keys.append(key)
    return keys


def _apply_checkmarks_to_md(body: str, checked_keys: list[str]) -> str:
    lines = body.splitlines(keepends=True)
    in_dod = False
    out: list[str] = []
    for line in lines:
        if _DOD_HEADING.match(line):
            in_dod = True
            out.append(line)
            continue
        if in_dod and _NEXT_HEADING.match(line) and not _DOD_HEADING.match(line):
            in_dod = False
        out.append(_maybe_flip_checkbox(line, checked_keys) if in_dod else line)
    return "".join(out)


def _maybe_flip_checkbox(line: str, checked_keys: list[str]) -> str:
    m = _MD_CHECKBOX.match(line)
    if not m or m.group("state").strip().lower() == "x":
        return line
    new_key = _normalize_key(m.group("text"))
    if not _key_matches_any(new_key, checked_keys):
        return line
    return line.replace("[ ]", "[x]", 1)


def _key_matches_any(key: str, candidates: list[str]) -> bool:
    """Match if `key` and any candidate share a common prefix of at
    least 3 content words. Handles the case where the live bullet was
    "Frontend PR merged" and the new bullet is "Frontend PR merged to
    release branch" (or vice-versa)."""
    key_words = key.split()
    if not key_words:
        return False
    for c in candidates:
        c_words = c.split()
        if not c_words:
            continue
        common = 0
        for a, b in zip(key_words, c_words):
            if a != b:
                break
            common += 1
        if common >= 3 or common == min(len(key_words), len(c_words)):
            return True
    return False


def _dod_lines(body: str) -> list[str]:
    out: list[str] = []
    in_dod = False
    for line in body.splitlines():
        if _DOD_HEADING.match(line):
            in_dod = True
            continue
        if in_dod and _NEXT_HEADING.match(line):
            break
        if in_dod:
            out.append(line)
    return out


def _normalize_key(text: str) -> str:
    cleaned = _PUNCT.sub(" ", text or "").lower()
    return " ".join(cleaned.split()[:5])
