"""Smoke-test helper: create a sample state.json for inspect."""

import uuid
from pathlib import Path

from file_to_jira.models import (
    BugRecord,
    BugStage,
    IntermediateFile,
    ModuleContext,
    ParsedBug,
)
from file_to_jira.state import save_state


def main() -> None:
    bugs = [
        BugRecord(
            stage=BugStage.PARSED,
            parsed=ParsedBug(
                bug_id="0123456789abcdef",
                external_id="CORE-CHAT-026",
                source_line_start=42,
                source_line_end=58,
                raw_title="Multi-domain two-skill section composer",
                raw_body="What's broken: composer fails to emit per-skill SSE events.",
                hinted_priority="P0",
                labels=["timeout-suspected-stale"],
                inherited_module=ModuleContext(
                    repo_alias="_core",
                    branch="2026-05-03_Auto_Fixes",
                    commit_sha="b7801f5",
                ),
                removed_fix_text="What's needed: wrap with Depends(_require_admin)",
            ),
        ),
        BugRecord(
            stage=BugStage.ENRICHED,
            parsed=ParsedBug(
                bug_id="fedcba9876543210",
                external_id="ARB-AUTH-001",
                source_line_start=120,
                source_line_end=138,
                raw_title="arb:sme cannot access ARB-type management endpoints",
                raw_body="What's broken: arb:sme calling POST /arb-types returns 201, expected 403.",
                hinted_priority="P0",
                labels=["real-product-gap"],
                inherited_module=ModuleContext(
                    repo_alias="vibe_coding/centarb",
                    branch="2026-05-03_Auto_Fixes",
                ),
            ),
        ),
    ]
    f = IntermediateFile(
        run_id=str(uuid.uuid4()),
        source_file="examples/sample.md",
        source_file_sha256="0" * 64,
        bugs=bugs,
    )
    out = Path("smoketest.state.json")
    save_state(f, out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
