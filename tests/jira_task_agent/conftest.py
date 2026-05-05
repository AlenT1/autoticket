"""pytest config + shared fixtures."""
from __future__ import annotations

from typing import Any

from jira_task_agent.jira.client import JiraClient


class MockJiraClient(JiraClient):
    """In-process JiraClient — no network. Reads return canned data;
    writes are recorded into self.recorded for assertions.

    Inherits from JiraClient so the markdown -> wiki conversion in
    create_issue / update_issue / post_comment runs exactly as it would
    in production. Tests then see the **post-conversion payload**, which
    is what the captured `would_send.json` would contain in the live
    runner with --capture.
    """

    def __init__(
        self,
        *,
        project_epics: list[dict] | None = None,
        issues: dict[str, dict] | None = None,
        children_by_epic: dict[str, list[dict]] | None = None,
        static_map: dict[str, str] | None = None,
    ):
        # Bypass the from_env path; set the auth-state fields directly.
        self.host = "mock.local"
        self.auth_header = "Bearer mock"
        self.auth_mode = "bearer"
        self._project_epics = list(project_epics or [])
        self._issues = dict(issues or {})
        self._children_by_epic = dict(children_by_epic or {})
        # Username resolution: pre-populate the static map.
        self._username_cache = {}
        self._static_map = {
            k.lower(): v for k, v in (static_map or {}).items()
        }
        # Pre-populate customfield IDs so create_issue doesn't try to
        # call /field over the (mocked) network.
        self._custom_fields = {
            "Epic Link": "customfield_10005",
            "Epic Name": "customfield_10006",
        }
        self.recorded: list[dict[str, Any]] = []

    # -- read-side: search / get -------------------------------------------

    def search(self, jql: str, *, fields: list[str] | None = None,
               max_results: int = 100) -> list[dict]:
        if "issuetype = Epic" in jql or "issuetype=Epic" in jql:
            return list(self._project_epics)
        # children lookup: 'Epic Link' = X or parent = X
        for key, children in self._children_by_epic.items():
            if f'"Epic Link" = {key}' in jql or f"parent = {key}" in jql:
                return list(children)
        return []

    def get(self, path: str, params: dict | None = None) -> dict:
        # /issue/<key>  (but NOT /issue/<key>/transitions etc.)
        if path.startswith("/issue/") and "/" not in path[len("/issue/"):]:
            key = path[len("/issue/"):]
            return self._issues.get(key, {})
        return {}

    # -- write-side: capture instead of send -------------------------------

    def post(self, path: str, json_body: dict) -> dict:
        self.recorded.append({"method": "POST", "path": path, "body": json_body})
        if path == "/issue":
            n = sum(1 for r in self.recorded if r["path"] == "/issue")
            return {"key": f"MOCK-{n}", "id": str(n), "self": "(mock)"}
        return {}

    def put(self, path: str, json_body: dict) -> None:
        self.recorded.append({"method": "PUT", "path": path, "body": json_body})

    # -- helpers for asserting --------------------------------------------

    def issue_creates(self) -> list[dict]:
        return [r for r in self.recorded if r["path"] == "/issue"]

    def updates(self) -> list[dict]:
        return [r for r in self.recorded
                if r["method"] == "PUT" and r["path"].startswith("/issue/")
                and "/" not in r["path"][len("/issue/"):]]

    def comments(self) -> list[dict]:
        return [r for r in self.recorded if r["path"].endswith("/comment")]
