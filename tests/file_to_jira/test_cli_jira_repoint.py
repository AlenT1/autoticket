"""c7 — assert f2j's CLI uses the canonical `_shared.io.sinks.jira`
types, not the legacy `file_to_jira.jira` package. Regression guard for
the c7 repoint.

The legacy package itself is still present at this commit boundary
(c8 deletes it after the test migration); these tests just assert the
CLI no longer references it.
"""
from __future__ import annotations


def test_cli_module_loads_without_legacy_jira_import():
    """Importing `file_to_jira.cli` shouldn't pull in the legacy package
    transitively. (CLI commands lazy-import the client inside each
    function, so a top-level CLI import shouldn't trigger that path.)"""
    import sys

    legacy_modules_before = {
        m for m in sys.modules
        if m.startswith("file_to_jira.jira")
    }
    import file_to_jira.cli  # noqa: F401
    legacy_modules_after = {
        m for m in sys.modules
        if m.startswith("file_to_jira.jira")
    }

    # A bare `import file_to_jira.cli` should not have pulled the legacy
    # client/field_map into sys.modules.
    new_legacy = legacy_modules_after - legacy_modules_before
    new_legacy.discard("file_to_jira.jira")  # __init__ alone is harmless
    assert new_legacy == set(), (
        f"f2j cli is still pulling in legacy modules: {new_legacy}"
    )


def test_cli_build_jira_client_uses_shared_jira_client(monkeypatch):
    """`_build_jira_client_for_cli` should construct a `_shared.io.sinks.jira.JiraClient`
    via `Settings.from_settings(...)`, not the legacy `file_to_jira.jira.JiraClient`.
    Connection details come from canonical env vars (JIRA_HOST + JIRA_TOKEN)."""
    monkeypatch.setenv("JIRA_HOST", "example.atlassian.net")
    monkeypatch.setenv("JIRA_TOKEN", "test-token")

    from _shared.io.sinks.jira import JiraClient as SharedJiraClient
    from file_to_jira.cli import _build_jira_client_for_cli

    client = _build_jira_client_for_cli()
    assert isinstance(client, SharedJiraClient), (
        f"expected `_shared` JiraClient, got {type(client)}"
    )


def test_cli_jira_fields_uses_shared_field_discovery(monkeypatch):
    """`_discover_fields_or_exit` should pull discovery functions from
    `_shared.io.sinks.jira`, not from the legacy `file_to_jira.jira`."""
    import inspect
    from file_to_jira.cli import _discover_fields_or_exit
    src = inspect.getsource(_discover_fields_or_exit)
    assert "_shared.io.sinks.jira" in src
    assert "from .jira " not in src and "file_to_jira.jira " not in src


def test_cli_whoami_uses_shared_jira_client():
    """`jira_whoami` should also import from `_shared`."""
    import inspect
    from file_to_jira.cli import jira_whoami
    src = inspect.getsource(jira_whoami)
    assert "_shared.io.sinks.jira" in src
    assert "from .jira " not in src
