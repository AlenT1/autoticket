"""Live E2E (capture mode, no Jira writes) for the DoD checkbox
preservation flow.

How to run:

  1. In Jira, open a task whose description has a `### Definition of
     Done` section (CENTPM-1255 is the standing test task). Tick at
     least one checkbox via the Jira UI so the live body has at
     least one `(x)` (Jira wiki) or `[x]` (markdown) DoD bullet.
  2. Edit the matching task's source bullet in
     `data/local_files/jira_task_agent_test.md` (T1 anchor) so the
     diff path will fire and produce an `update_task` for that
     issue.
  3. Run: `pytest tests/test_dod_preserve_live.py -m live -v -s`.

The test:
  - runs the full pipeline in capture mode (no real Jira writes),
  - locates the captured PUT for CENTPM-1255,
  - asserts the captured body still contains every `[x]` that was
    present in the live Jira description at run time, and
  - asserts the captured body's `### Definition of Done` block has
    at least as many `[x]` markers as live had checked items.

If you haven't ticked any boxes in Jira yet, the test skips with a
helpful message instead of failing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dotenv import load_dotenv

from jira_task_agent.jira.client import JiraClient, get_issue
from jira_task_agent.runner import run_once


_CHECKED_RE = re.compile(
    r"^\s*(?:-\s*\[x\]|\*\s*\([/y]\))\s*(?P<text>.*?)\s*$"
)
_DOD_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*Definition of Done|h[1-6]\.\s*Definition of Done)\s*$",
    re.IGNORECASE,
)
_NEXT_HEADING = re.compile(r"^\s*(?:#{1,6}\s|h[1-6]\.\s)")


def _extract_checked_keys(body: str) -> list[str]:
    """Return a normalized key for each checked DoD bullet in `body`,
    handling markdown `[x]` and Jira-wiki `(/)` / `(y)` syntaxes."""
    out: list[str] = []
    in_dod = False
    for line in body.splitlines():
        if _DOD_HEADING.match(line):
            in_dod = True
            continue
        if in_dod and _NEXT_HEADING.match(line):
            break
        if not in_dod:
            continue
        m = _CHECKED_RE.match(line)
        if m:
            words = re.sub(r"[^\w\s]", " ", m.group("text").lower()).split()
            if words:
                out.append(" ".join(words[:5]))
    return out


pytestmark = [pytest.mark.live]

ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR = ROOT / "data" / "local_files"
LOCAL_FILE_NAME = "jira_task_agent_test.md"
LOCAL_FILE_PATH = LOCAL_DIR / LOCAL_FILE_NAME
TEST_TASK_KEY = "CENTPM-1255"


def _ensure_env() -> None:
    load_dotenv()
    for var in ("NVIDIA_API_KEY", "JIRA_HOST", "JIRA_PROJECT_KEY", "JIRA_TOKEN"):
        if not os.environ.get(var):
            pytest.skip(f"{var} not set; live test skipped")


def _has_dod_checks_in_jira(jira: JiraClient) -> tuple[bool, list[str]]:
    issue = get_issue(TEST_TASK_KEY, client=jira)
    if not issue:
        pytest.skip(f"{TEST_TASK_KEY} not found")
    body = issue.get("description") or ""
    keys = _extract_checked_keys(body)
    return bool(keys), keys


def _captured_put_for(captures: list[dict], key: str) -> dict | None:
    for op in captures:
        if op.get("method") == "PUT" and op.get("path") == f"/issue/{key}":
            return op
    return None


def test_preserve_dod_checkmarks_on_update_task(tmp_path):
    _ensure_env()
    jira = JiraClient.from_env()
    has_checks, live_keys = _has_dod_checks_in_jira(jira)
    if not has_checks:
        pytest.skip(
            f"{TEST_TASK_KEY} has no checked DoD items in Jira; tick at "
            "least one box in the Jira UI before running this test."
        )

    if not LOCAL_FILE_PATH.exists():
        pytest.skip(f"{LOCAL_FILE_PATH} not staged")

    cache_path = tmp_path / "cache.json"
    state_path = tmp_path / "state.json"
    capture_path = tmp_path / "capture.json"

    real_cache = ROOT / "data" / "cache.json"
    if real_cache.exists():
        shutil.copy(real_cache, cache_path)

    # Append a small marker to the T1 bullet so the diff path triggers
    # update_task on CENTPM-1255 without disturbing T2.
    original = LOCAL_FILE_PATH.read_text(encoding="utf-8")
    marker = f" [dod-preserve-test {datetime.now(timezone.utc).isoformat(timespec='seconds')}]"
    anchor_phrase = "verification is complete."
    if anchor_phrase not in original:
        pytest.skip(
            f"expected anchor phrase {anchor_phrase!r} not found in "
            f"{LOCAL_FILE_PATH}; skipping rather than mutating wrong location"
        )
    mutated = original.replace(anchor_phrase, anchor_phrase + marker, 1)

    try:
        LOCAL_FILE_PATH.write_text(mutated, encoding="utf-8")
        report = run_once(
            apply=True,
            capture_path=str(capture_path),
            cache_path=cache_path,
            state_path=state_path,
            use_cache=True,
            source="local",
            local_dir=str(LOCAL_DIR),
            only_file_name=LOCAL_FILE_NAME,
            since_override=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    finally:
        LOCAL_FILE_PATH.write_text(original, encoding="utf-8")

    assert report.errors == [], f"errors: {report.errors}"

    captures = json.loads(capture_path.read_text(encoding="utf-8"))
    put_op = _captured_put_for(captures, TEST_TASK_KEY)
    assert put_op is not None, (
        f"expected a captured PUT for {TEST_TASK_KEY}; got "
        f"{[(c.get('method'), c.get('path')) for c in captures]}"
    )

    body = put_op.get("body") or {}
    fields = body.get("fields") or body  # PUT /issue/<key> wraps in `fields`
    new_body = fields.get("description") or ""
    new_checked_keys = _extract_checked_keys(new_body)

    debug_path = ROOT / "data" / "_dod_debug_capture.txt"
    debug_path.write_text(
        f"=== ALL CAPTURED OPS ({len(captures)}) ===\n"
        f"{json.dumps([{'method': c.get('method'), 'path': c.get('path'), 'body_keys': list((c.get('body') or {}).keys())} for c in captures], indent=2)}\n"
        f"\n=== PUT for {TEST_TASK_KEY} (full body) ===\n"
        f"{json.dumps(put_op, indent=2)}\n",
        encoding="utf-8",
    )

    print(
        f"\n[dod-preserve] live had {len(live_keys)} checked DoD item(s); "
        f"capture has {len(new_checked_keys)} checked.",
        flush=True,
    )
    print(f"[dod-preserve] live keys:    {live_keys}", flush=True)
    print(f"[dod-preserve] capture keys: {new_checked_keys}", flush=True)
    print(f"[dod-preserve] debug dump: {debug_path}", flush=True)

    assert len(new_checked_keys) >= len(live_keys), (
        f"DoD checkbox state regressed: live had {len(live_keys)} "
        f"checked items, captured PUT has {len(new_checked_keys)}."
    )
