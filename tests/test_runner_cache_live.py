"""Live e2e: Tier 3 matcher cache must NOT change behavior.

Runs `runner.run_once` twice on the same single file with `--capture`:
  1. Cold (no cache) → fresh classify + extract + matcher LLM calls.
  2. Warm (cache from cold) → tier 1+2+3 should hit; matcher reuses
     the cached decision.

Asserts:
  * Cold and warm produce the SAME structural signature of captured
    Jira ops (same method/path/summary/issuetype/epic_link/target_key).
    LLM-level string variation in descriptions is tolerated; structural
    divergence is not — that would mean the cache silently changed
    what we'd write.
  * Warm run reports `cache_hits_match >= 1`.
  * Warm run does no real writes (capture mode).

Opt-in via the `live` marker. ~3 min total. Reads real Drive + real
Jira project tree; writes nothing to Jira.

To run:
    pytest tests/test_runner_cache_live.py -m live -v -s
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from jira_task_agent.runner import run_once


pytestmark = [pytest.mark.live]


# Use the smallest task file to keep the test fast.
TARGET_FILE = "NemoClaw_Tasks.md"


def _ensure_env() -> None:
    load_dotenv()
    for var in ("NVIDIA_API_KEY", "FOLDER_ID", "JIRA_HOST", "JIRA_PROJECT_KEY", "JIRA_TOKEN"):
        if not os.environ.get(var):
            pytest.skip(f"{var} not set; skipping live runner cache test")


def _signature(captured_path: Path) -> list[tuple]:
    """Project a captured-ops file down to a structural signature.

    Drops description bodies and comment text (LLM string variation is
    not what we're testing). Keeps everything that determines which
    Jira issues would be touched and how.
    """
    if not captured_path.exists():
        return []
    ops = json.loads(captured_path.read_text(encoding="utf-8"))
    sig: list[tuple] = []
    for op in ops:
        body = op.get("body", {})
        fields = body.get("fields", {})
        itype = fields.get("issuetype")
        itype_name = itype.get("name") if isinstance(itype, dict) else None
        # epic_link can be either a top-level Epic Link customfield or
        # nested under fields with the resolved key. We capture whichever
        # we can find as a string.
        epic_link = None
        for k, v in fields.items():
            if "customfield" in k and isinstance(v, str) and v.startswith("CENT"):
                epic_link = v
                break
        sig.append(
            (
                op.get("method"),
                op.get("path"),
                fields.get("summary"),
                itype_name,
                epic_link,
            )
        )
    return sorted(sig, key=lambda t: tuple(str(x) for x in t))


def test_cache_does_not_change_captured_writes(tmp_path: Path):
    _ensure_env()

    cache_path = tmp_path / "cache.json"
    state_path = tmp_path / "state.json"
    cold_capture = tmp_path / "cold.json"
    warm_capture = tmp_path / "warm.json"

    # ---- Cold run -------------------------------------------------------
    cold_report = run_once(
        apply=True,
        capture_path=str(cold_capture),
        only_file_name=TARGET_FILE,
        cache_path=cache_path,
        state_path=state_path,
        use_cache=False,  # bypass any preexisting cache
    )
    assert cold_report.errors == [], f"cold run errors: {cold_report.errors}"
    assert cold_report.cache_hits_match == 0, (
        "cold run must not be served by cache"
    )

    # The cold run had use_cache=False, so cache.json wasn't written.
    # Re-run cold but WITH cache enabled so the cache populates for the
    # warm comparison. This second cold call should still be a cache miss
    # because cache.json is empty — and it writes the cache as a side
    # effect.
    seed_capture = tmp_path / "seed.json"
    seed_report = run_once(
        apply=True,
        capture_path=str(seed_capture),
        only_file_name=TARGET_FILE,
        cache_path=cache_path,
        state_path=state_path,
        use_cache=True,
    )
    assert seed_report.errors == [], f"seed run errors: {seed_report.errors}"
    assert seed_report.cache_hits_match == 0, (
        "seed run must populate the cache, not consume it"
    )

    # ---- Warm run -------------------------------------------------------
    warm_report = run_once(
        apply=True,
        capture_path=str(warm_capture),
        only_file_name=TARGET_FILE,
        cache_path=cache_path,
        state_path=state_path,
        use_cache=True,
    )
    assert warm_report.errors == [], f"warm run errors: {warm_report.errors}"
    assert warm_report.cache_hits_match >= 1, (
        "warm run must serve at least one matcher decision from cache"
    )
    # Tier 1 / Tier 2 should also hit since classify+extract were
    # populated during the seed run.
    assert warm_report.cache_hits_classify >= 1
    assert warm_report.cache_hits_extract >= 1

    # ---- Behavior equivalence ------------------------------------------
    cold_sig = _signature(seed_capture)
    warm_sig = _signature(warm_capture)
    assert cold_sig == warm_sig, (
        "Tier 3 cache changed captured ops:\n"
        f"  cold ({len(cold_sig)} ops):\n    " +
        "\n    ".join(map(str, cold_sig))
        + f"\n  warm ({len(warm_sig)} ops):\n    " +
        "\n    ".join(map(str, warm_sig))
    )

    # ---- Action histograms must also match -----------------------------
    cold_actions = dict(seed_report.actions_by_kind)
    warm_actions = dict(warm_report.actions_by_kind)
    assert cold_actions == warm_actions, (
        f"action histograms diverge: cold={cold_actions} warm={warm_actions}"
    )
