"""Lock the run-plan MD format against drift.

Pure-function tests: feed the renderer a fixture dict, assert key
strings appear. Determinism is enforced by calling the renderer twice
and asserting byte-equality.
"""
from __future__ import annotations

from jira_task_agent.pipeline.run_plan_md import render_run_plan_md


def _may1_fixture() -> dict:
    return {
        "generated_at": "2026-05-04T14:32:00Z",
        "mode": "dry-run",
        "source": "Drive + local",
        "files_scanned": 19,
        "files_with_changes": 1,
        "totals": {
            "create_epic": 1, "update_epic": 0,
            "create_task": 4, "update_task": 1,
            "skip_completed_epic": 0, "covered_by_rollup": 0,
            "comments": 1, "jira_ops": 6,
        },
        "files": [
            {
                "file_name": "May1_Initial_Version_Tasks.md",
                "source_url": "https://docs.google.com/document/d/abc123/edit",
                "last_edited_by": "Saar",
                "actions": [
                    {
                        "kind": "update_task",
                        "target_key": "CENTPM-1237",
                        "jira_url": "https://jira.local/browse/CENTPM-1237",
                        "summary": "Hide schedule button for May 1st release",
                        "description": "Hide or disable button.\n\n### DoD\n- [ ] done",
                        "source_anchor": "UI-1",
                        "assignee_username": "lzilberberg",
                        "live_summary": "Disable Flows for May 1st release",
                        "live_description": "Disable the Flows feature.",
                    },
                    {
                        "kind": "create_task",
                        "summary": "Add burst-alerting verification",
                        "description": "Validate burst alerting.\n\n### DoD\n- [ ] thresholds set",
                        "source_anchor": "MON-NEW",
                        "assignee_username": "aklein",
                        "parent_epic_key": "CENTPM-1198",
                        "parent_epic_url": "https://jira.local/browse/CENTPM-1198",
                        "parent_epic_summary": "Monitoring & Alerting",
                    },
                    {
                        "kind": "create_epic",
                        "summary": "Disaster recovery readiness",
                        "description": "Validate runbooks and recovery RPO targets.",
                        "source_anchor": "<epic>:9",
                    },
                    {
                        "kind": "create_task",
                        "summary": "Confirm DB snapshot cadence covers RPO target",
                        "description": "...",
                        "source_anchor": "DR-1",
                        "assignee_username": "saar",
                        "parent_epic_key": None,
                    },
                ],
            }
        ],
    }


def test_renderer_is_deterministic():
    fix = _may1_fixture()
    a = render_run_plan_md(fix)
    b = render_run_plan_md(fix)
    assert a == b
    assert a.endswith("\n")


def test_header_carries_counts_and_mode():
    md = render_run_plan_md(_may1_fixture())
    assert "# Run plan — 2026-05-04T14:32:00Z" in md
    assert "**Mode:** dry-run" in md
    assert "**Source:** Drive + local" in md
    assert "1 changed of 19" in md
    assert "| create_epic | 1 |" in md
    assert "| create_task | 4 |" in md
    assert "| update_task | 1 |" in md
    assert "**Total Jira ops:** 6 writes + 1 comments" in md


def test_file_section_shows_metadata():
    md = render_run_plan_md(_may1_fixture())
    assert "## May1_Initial_Version_Tasks.md" in md
    assert "[Open source doc](https://docs.google.com/document/d/abc123/edit)" in md
    assert "last edited by **Saar**" in md


def test_updates_section_renders_first():
    md = render_run_plan_md(_may1_fixture())
    upd_idx = md.index("### Updates (1)")
    crt_idx = md.index("### Creates (3)")
    assert upd_idx < crt_idx, "Updates must precede Creates"


def test_update_task_shows_new_data_with_jira_link():
    md = render_run_plan_md(_may1_fixture())
    assert "UPDATE TASK — [CENTPM-1237](https://jira.local/browse/CENTPM-1237)" in md
    assert "_anchor:_ `UI-1`" in md
    assert "Disable Flows for May 1st release  →  **Hide schedule button for May 1st release**" in md
    assert "**New description:**" in md
    assert "> Hide or disable button." in md
    assert "Diff vs live description" in md
    assert "- Disable the Flows feature." in md
    assert "+ Hide or disable button." in md


def test_update_task_shows_comment_plan():
    md = render_run_plan_md(_may1_fixture())
    assert "**Comment that will be posted:** mentions [~lzilberberg]" in md
    assert "source doc + last editor" in md


def test_create_task_shows_parent_epic_link():
    md = render_run_plan_md(_may1_fixture())
    assert "CREATE TASK" in md
    assert "_anchor:_ `MON-NEW`" in md
    assert "**Title:** Add burst-alerting verification" in md
    assert "**Under epic:** [CENTPM-1198](https://jira.local/browse/CENTPM-1198) — *Monitoring & Alerting*" in md
    assert "**Assignee:** `aklein`" in md
    assert "> Validate burst alerting." in md


def test_create_task_under_new_epic_says_so():
    md = render_run_plan_md(_may1_fixture())
    assert "**Under epic:** _new epic created in this run_" in md


def test_create_epic_shows_title_and_description():
    md = render_run_plan_md(_may1_fixture())
    assert "CREATE EPIC" in md
    assert "**Title:** Disaster recovery readiness" in md
    assert "> Validate runbooks and recovery RPO targets." in md


def test_skip_completed_renders_as_note():
    plan = _may1_fixture()
    plan["files"][0]["actions"] = [{
        "kind": "skip_completed_epic",
        "target_key": "CENTPM-9999",
        "jira_url": "https://jira.local/browse/CENTPM-9999",
    }]
    plan["totals"] = {"skip_completed_epic": 1, "comments": 0, "jira_ops": 0}
    md = render_run_plan_md(plan)
    assert "### Notes" in md
    assert "**Skipping**" in md
    assert "[CENTPM-9999](https://jira.local/browse/CENTPM-9999)" in md
    assert "completed status" in md


def test_covered_by_rollup_renders_as_note():
    plan = _may1_fixture()
    plan["files"][0]["actions"] = [{
        "kind": "covered_by_rollup",
        "target_key": "CENTPM-555",
        "jira_url": "https://jira.local/browse/CENTPM-555",
        "source_anchor": "T-2",
    }]
    plan["totals"] = {"covered_by_rollup": 1, "comments": 0, "jira_ops": 0}
    md = render_run_plan_md(plan)
    assert "**Covered by rollup:** [CENTPM-555](https://jira.local/browse/CENTPM-555) (anchor `T-2`)" in md


def test_no_actions_renders_no_changes_note():
    plan = _may1_fixture()
    plan["files"][0]["actions"] = []
    md = render_run_plan_md(plan)
    assert "_No changes._" in md
    assert "### Updates" not in md
    assert "### Creates" not in md


def test_empty_files_list_still_valid():
    plan = {
        "generated_at": "x", "mode": "dry-run", "source": "local",
        "files_scanned": 0, "files_with_changes": 0,
        "totals": {"jira_ops": 0, "comments": 0},
        "files": [],
    }
    md = render_run_plan_md(plan)
    assert "**Total Jira ops:** 0 writes + 0 comments" in md
    assert "## 📄" not in md
