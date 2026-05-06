"""c6 — assert the drive `jira_task_agent.jira` shim package is gone
and every consumer imports the canonical `_shared` types directly.

These tests prevent regression: nothing should accidentally re-introduce
the shim (or import via it) once c6 deleted the
`src/jira_task_agent/jira/{__init__,client,project_tree}.py` re-exports.
"""
from __future__ import annotations

import importlib

import pytest


def test_drive_jira_package_no_longer_importable():
    """`jira_task_agent.jira` was the home of the shim; after c6 it's
    deleted. Importing it must fail."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("jira_task_agent.jira")


def test_drive_jira_client_module_no_longer_importable():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("jira_task_agent.jira.client")


def test_drive_jira_project_tree_module_no_longer_importable():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("jira_task_agent.jira.project_tree")


def test_runner_jira_client_resolves_to_shared_canonical():
    """The runner's `JiraClient` must be the canonical class from
    `_shared.io.sinks.jira.client`, not a different re-export. Identity
    check is the strongest guarantee — no shim is forwarding the type
    through a different import path."""
    from _shared.io.sinks.jira.client import JiraClient as SharedJiraClient

    import jira_task_agent.runner as runner_module

    assert runner_module.JiraClient is SharedJiraClient


def test_runner_imports_capturing_and_jira_sink_from_shared():
    """c5 + c6 should leave the runner with sink + Ticket types coming
    from `_shared` only. This guards against future drift."""
    from _shared.io.sinks import Ticket as SharedTicket
    from _shared.io.sinks.jira import (
        CapturingJiraSink as SharedCapturingJiraSink,
        JiraSink as SharedJiraSink,
    )

    import jira_task_agent.runner as runner_module

    assert runner_module.Ticket is SharedTicket
    assert runner_module.JiraSink is SharedJiraSink
    assert runner_module.CapturingJiraSink is SharedCapturingJiraSink


def test_runner_module_has_no_get_issue_top_level_import():
    """c5 removed `get_issue` from the runner's apply path (replaced by
    `sink.get_issue_normalized`); c6 dropped the shim. Make sure
    `get_issue` is NOT re-exported as a top-level runner attribute —
    a regression there would mean somebody re-added the legacy import."""
    import jira_task_agent.runner as runner_module

    assert not hasattr(runner_module, "get_issue"), (
        "`get_issue` should not be a top-level attribute on the runner; "
        "the runner reads live state via `sink.get_issue_normalized` now"
    )


def test_run_plan_md_lazy_imports_get_issue_from_shared(monkeypatch):
    """run_plan_md.py keeps three lazy `from .. import get_issue` calls
    for the verify-gate diff view. After c6 they should resolve to the
    `_shared` module path."""
    # Force re-import to be safe; the lazy imports happen inside the
    # *_dict helpers, not at module load.
    from jira_task_agent.pipeline import run_plan_md as rpm
    from _shared.io.sinks.jira import client as shared_client

    # Sanity: the helpers exist and are callable. We don't invoke them
    # against a real Jira (no client) — just confirm the inner import
    # references the right module.
    src = rpm.__file__
    assert src is not None
    text = open(src, encoding="utf-8").read()
    assert "_shared.io.sinks.jira.client import get_issue" in text, (
        "run_plan_md.py must lazy-import get_issue from _shared, "
        "not from the deleted drive shim"
    )
    # And the symbol exists in _shared, so the lazy import will succeed.
    assert callable(shared_client.get_issue)
