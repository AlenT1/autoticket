"""Integration test for the LLM matcher — small, opt-in, hits the LLM.

Verifies real model behaviour on a handful of known-correct pairings
drawn from the May1 release plan vs CENTPM-1162 ("Security Hardening")
children. Uses the real `chat()` so it costs a few LLM calls per run.

Run only on demand:
    pytest tests/test_matcher_integration.py -m live -v

Skipped by default (no `live` marker on the command line).
"""
from __future__ import annotations

import os
import pytest

from dotenv import load_dotenv

from jira_task_agent.pipeline.matcher import MatchInput, match


pytestmark = [pytest.mark.live]


def _ensure_env() -> None:
    load_dotenv()
    if not os.environ.get("NVIDIA_API_KEY"):
        pytest.skip("NVIDIA_API_KEY not set; skipping live LLM matcher test")


# Known correct pairings — verbose Drive-side wording vs terse Jira-side.
# These tasks are CENTPM-1162's children read earlier in the project.
KNOWN_PAIRS = [
    {
        "item": MatchInput(
            summary="Generate JWT secret and store in Vault for prod",
            description=(
                "Generate a 64-char secret, store it in Vault under the "
                "production path, and update the prod env template to "
                "reference it."
            ),
        ),
        "expected_key": "CENTPM-1163",  # "JWT secret"
    },
    {
        "item": MatchInput(
            summary="Disable SSO bypass in prod and add startup validation",
            description=(
                "Verify SSO bypass is set to false in production. Add a "
                "startup-time check that fails fast if the bypass flag "
                "is enabled."
            ),
        ),
        "expected_key": "CENTPM-1165",  # "Disable SSO bypass"
    },
    {
        "item": MatchInput(
            summary="Restrict CORS origins to production domain",
            description=(
                "Production cors_origins must only allow the production "
                "domain — no wildcards, no staging domains."
            ),
        ),
        "expected_key": "CENTPM-1170",  # "CORS policy review"
    },
]


CANDIDATES = [
    MatchInput(key="CENTPM-1163", summary="JWT secret",
               description="Generate 64-char secret, store in Vault, update prd template"),
    MatchInput(key="CENTPM-1164", summary="ID token verification",
               description="Enable JWKS signature verification (currently disabled). Consult Evgeny"),
    MatchInput(key="CENTPM-1165", summary="Disable SSO bypass",
               description="Verify bypass=false in prod. Add startup validation"),
    MatchInput(key="CENTPM-1167", summary="Sanitize error responses",
               description="Return generic errors in prod. Log details server-side only"),
    MatchInput(key="CENTPM-1168", summary="Rate limiting",
               description="Limit auth, chat, flow-generate endpoints"),
    MatchInput(key="CENTPM-1169", summary="RBAC on admin endpoints",
               description="Check JWT roles on /api/registry/*. Admin-only"),
    MatchInput(key="CENTPM-1170", summary="CORS policy review",
               description="Prod cors_origins: only allow prod domain"),
    MatchInput(key="CENTPM-1171", summary="Encrypt provider tokens",
               description="ProviderToken stores plaintext. Enable TDE or app-level encryption"),
]


def test_matcher_pairs_known_security_tasks_correctly():
    """Real LLM call. Each known-correct pairing should be picked with
    confidence >= 0.7."""
    _ensure_env()

    items = [p["item"] for p in KNOWN_PAIRS]
    decisions = match(items=items, candidates=CANDIDATES, kind="task")

    assert len(decisions) == len(KNOWN_PAIRS), "matcher must return one decision per item"

    failures: list[str] = []
    for d, expected in zip(decisions, KNOWN_PAIRS):
        if d.candidate_key != expected["expected_key"]:
            failures.append(
                f"  item {d.item_index} expected {expected['expected_key']!r}, "
                f"got {d.candidate_key!r} (conf={d.confidence:.2f}, reason={d.reason!r})"
            )

    if failures:
        pytest.fail(
            "matcher pairings disagreed with known-correct expectations:\n"
            + "\n".join(failures)
        )


def test_matcher_returns_none_for_clearly_unrelated_item():
    """A wildly-unrelated item ('upgrade kitchen sink') against the
    security candidates should be returned as None."""
    _ensure_env()

    decisions = match(
        items=[MatchInput(summary="Upgrade the office kitchen sink",
                          description="Plumbing work in building 5.")],
        candidates=CANDIDATES,
        kind="task",
    )
    assert decisions[0].candidate_key is None, (
        f"unrelated item should not match any candidate, got "
        f"{decisions[0].candidate_key!r} (conf={decisions[0].confidence:.2f})"
    )
