"""Jira REST API client.

Auth modes:
  - JIRA_AUTH_MODE=bearer (default): Authorization: Bearer <PAT>  (Server/DC)
  - JIRA_AUTH_MODE=basic:            Authorization: Basic b64(email:token)  (Cloud)

Token lookup order (first hit wins):
  1. JIRA_TOKEN env var (explicit override)
  2. ~/.autodev/tokens/task-jira-${JIRA_PROJECT_KEY or REPO_NAME}
  3. ~/.autodev/tokens/${REPO_OWNER}-${REPO_NAME}
  4. AUTODEV_TOKEN env var
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


def _load_token() -> str:
    if t := os.environ.get("JIRA_TOKEN"):
        return t.strip()
    project = (
        os.environ.get("JIRA_PROJECT_KEY")
        or os.environ.get("REPO_NAME")
        or "unknown"
    )
    role_file = Path.home() / ".autodev" / "tokens" / f"task-jira-{project}"
    if role_file.exists():
        return role_file.read_text().strip()
    repo_owner = os.environ.get("REPO_OWNER", "unknown")
    repo_name = os.environ.get("REPO_NAME", "unknown")
    legacy = Path.home() / ".autodev" / "tokens" / f"{repo_owner}-{repo_name}"
    if legacy.exists():
        return legacy.read_text().strip()
    if t := os.environ.get("AUTODEV_TOKEN"):
        return t.strip()
    raise RuntimeError(
        "No Jira token found. Set JIRA_TOKEN or AUTODEV_TOKEN, or place a "
        f"token at ~/.autodev/tokens/task-jira-{project}."
    )


def _normalize_host(host: str) -> str:
    host = host.strip()
    for prefix in ("http://", "https://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host.rstrip("/")


def _build_auth_header(token: str) -> str:
    mode = os.environ.get("JIRA_AUTH_MODE", "bearer").lower()
    if mode == "basic":
        email = os.environ.get("JIRA_USER_EMAIL")
        if not email:
            raise RuntimeError(
                "JIRA_AUTH_MODE=basic requires JIRA_USER_EMAIL to be set."
            )
        encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
        return f"Basic {encoded}"
    return f"Bearer {token}"


@dataclass
class JiraClient:
    host: str
    auth_header: str
    auth_mode: str

    @classmethod
    def from_env(cls) -> "JiraClient":
        host = os.environ.get("JIRA_HOST")
        if not host:
            raise RuntimeError(
                "JIRA_HOST must be set (e.g. jira.example.com or "
                "yourorg.atlassian.net)."
            )
        token = _load_token()
        return cls(
            host=_normalize_host(host),
            auth_header=_build_auth_header(token),
            auth_mode=os.environ.get("JIRA_AUTH_MODE", "bearer").lower(),
        )

    @property
    def api_base(self) -> str:
        return f"https://{self.host}/rest/api/2"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self.auth_header,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
        }

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = requests.get(
            f"{self.api_base}{path}",
            headers=self._headers(),
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def put(self, path: str, json_body: dict[str, Any]) -> None:
        r = requests.put(
            f"{self.api_base}{path}",
            headers=self._headers(),
            json=json_body,
            timeout=30,
        )
        r.raise_for_status()

    def update_issue(self, key: str, fields: dict[str, Any]) -> None:
        """PUT /issue/{key} with the given fields dict (e.g. {"description": "..."}).

        If a 'description' field is present, it is automatically converted
        from Markdown to Jira wiki markup so it renders correctly.
        """
        if "description" in fields:
            fields = {**fields, "description": self._md_to_jira_wiki(fields["description"])}
        self.put(f"/issue/{key}", {"fields": fields})

    def post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        r = requests.post(
            f"{self.api_base}{path}",
            headers=self._headers(),
            json=json_body,
            timeout=30,
        )
        r.raise_for_status()
        # Some endpoints return empty body (201 / 204).
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    # -- Markdown -> Jira wiki conversion ---------------------------------

    @staticmethod
    def _md_to_jira_wiki(text: str) -> str:
        """Convert a subset of Markdown to Jira Server wiki markup so
        descriptions/comments render correctly in the Jira UI.

        Covers: headings, fenced code, inline code, bold, italic, links,
        and checkbox bullets. Leaves the agent marker line untouched.
        """
        if not text:
            return text
        import re as _re

        # Fenced code first (``` ... ```), so we don't munge their contents.
        def _code_block(m: "_re.Match[str]") -> str:
            lang = (m.group(1) or "").strip()
            body = m.group(2)
            return (
                "{code:" + lang + "}\n" + body + "\n{code}"
                if lang
                else "{code}\n" + body + "\n{code}"
            )

        text = _re.sub(r"```(\w*)\n(.*?)\n```", _code_block, text, flags=_re.S)

        # Headings (line-anchored)
        text = _re.sub(r"^###### (.+)$", r"h6. \1", text, flags=_re.M)
        text = _re.sub(r"^##### (.+)$", r"h5. \1", text, flags=_re.M)
        text = _re.sub(r"^#### (.+)$", r"h4. \1", text, flags=_re.M)
        text = _re.sub(r"^### (.+)$", r"h3. \1", text, flags=_re.M)
        text = _re.sub(r"^## (.+)$", r"h2. \1", text, flags=_re.M)
        text = _re.sub(r"^# (.+)$", r"h1. \1", text, flags=_re.M)

        # Markdown checkbox bullets: "- [ ] item" / "- [x] item"
        # Jira: "* (x) item" (red X) for unchecked, "* (/) item" (green) for checked.
        text = _re.sub(
            r"^(\s*)-\s*\[\s*\]\s*(.+)$",
            r"\1* (x) \2",
            text,
            flags=_re.M,
        )
        text = _re.sub(
            r"^(\s*)-\s*\[[xX]\]\s*(.+)$",
            r"\1* (/) \2",
            text,
            flags=_re.M,
        )
        # Plain unordered bullets: "- text" -> "* text"
        text = _re.sub(r"^(\s*)-\s+(?!\(.\) )(.+)$", r"\1* \2", text, flags=_re.M)

        # Inline code `x` -> {{x}}
        text = _re.sub(r"`([^`\n]+)`", r"{{\1}}", text)

        # Bold **x** -> *x* (Jira)
        text = _re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", text)

        # Links [text](url) -> [text|url]
        text = _re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", r"[\1|\2]", text)

        return text

    # -- Customfield discovery (cached) ------------------------------------

    _custom_fields: dict[str, str] | None = None  # name -> id

    def _custom_field_id(self, name: str) -> str | None:
        """One-shot fetch + cache of all custom-field IDs by display name.

        Returns the customfield id for `name` (e.g. "Epic Link",
        "Epic Name") or None if not present on this instance.
        """
        if self._custom_fields is None:
            resp = requests.get(
                f"{self.api_base}/field",
                headers=self._headers(),
                timeout=30,
            )
            resp.raise_for_status()
            self._custom_fields = {
                f["name"]: f["id"] for f in resp.json() if f.get("name")
            }
        return self._custom_fields.get(name)

    def epic_link_field_id(self) -> str | None:
        return self._custom_field_id("Epic Link")

    def epic_name_field_id(self) -> str | None:
        return self._custom_field_id("Epic Name")

    # -- Writes -------------------------------------------------------------

    def create_issue(
        self,
        *,
        project_key: str,
        summary: str,
        description: str,
        issue_type: str = "Task",
        epic_link: str | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /issue. Returns Jira's response: {id, key, self}.

        Always adds the `ai-generated` label so machine-created issues are
        visible at a glance in the Jira UI. Caller can override `labels`
        via `extra_fields`.

        For `issue_type="Epic"` on Jira Server, automatically discovers and
        populates the Epic Name customfield with the summary. (Epic Name
        is required on epic creation; Server / Software addon.)
        """
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "description": self._md_to_jira_wiki(description),
            "issuetype": {"name": issue_type},
            "labels": ["ai-generated"],
        }
        if epic_link:
            field_id = self.epic_link_field_id()
            if field_id:
                fields[field_id] = epic_link
            else:
                fields["parent"] = {"key": epic_link}  # next-gen / company-managed
        if issue_type.lower() == "epic":
            epic_name_id = self.epic_name_field_id()
            if epic_name_id:
                fields[epic_name_id] = summary
        if extra_fields:
            fields.update(extra_fields)
        return self.post("/issue", {"fields": fields})

    def post_comment(self, key: str, body: str) -> dict[str, Any]:
        """POST /issue/{key}/comment. `body` is converted from Markdown to
        Jira wiki markup so it renders correctly in the UI.
        """
        return self.post(
            f"/issue/{key}/comment",
            {"body": self._md_to_jira_wiki(body)},
        )

    def transition_issue(self, key: str, target_status: str) -> bool:
        """Transition `key` to a state whose `to.name` matches `target_status`.
        Returns True on success, False if no matching transition is available.
        """
        data = self.get(f"/issue/{key}/transitions")
        for t in data.get("transitions", []):
            if (t.get("to") or {}).get("name") == target_status:
                self.post(
                    f"/issue/{key}/transitions",
                    {"transition": {"id": t["id"]}},
                )
                return True
        return False

    # -- Username resolution (assignee owners → Jira login) ----------------

    _username_cache: dict[str, str | None] = None  # type: ignore[assignment]
    _static_map: dict[str, str] | None = None

    def _load_static_map(self) -> dict[str, str]:
        """Load `team_mapping.json` from CWD if present. Keys lowercased."""
        if self._static_map is not None:
            return self._static_map
        import json as _json
        path = Path("team_mapping.json")
        if path.exists():
            try:
                raw = _json.loads(path.read_text(encoding="utf-8"))
                self._static_map = {
                    str(k).strip().lower(): str(v).strip()
                    for k, v in raw.items()
                    if k and v
                }
            except Exception:
                self._static_map = {}
        else:
            self._static_map = {}
        return self._static_map

    def resolve_assignee_username(self, raw: str | None) -> str | None:
        """Resolve a free-form owner string (e.g. "Sharon", "Lior + Aviv") to
        a Jira-Server username via `team_mapping.json` (case-insensitive).

        Composite owners ("Lior + Aviv", "Nick/Joe + Guy") resolve to the
        FIRST individual; the rest are dropped (caller can @-mention them
        separately in the description if needed).

        Returns None if the name isn't in the static map. We deliberately
        do NOT fall back to Jira's `/user/search` because first-name
        matching at scale produces wrong assignees silently.

        Cached per-client.
        """
        if not raw:
            return None
        if self._username_cache is None:
            self._username_cache = {}
        if raw in self._username_cache:
            return self._username_cache[raw]

        import re as _re
        first = _re.split(r"\s*(?:\+|/|&|,| and )\s*", raw)[0].strip()
        if not first:
            self._username_cache[raw] = None
            return None

        username = self._load_static_map().get(first.lower())
        self._username_cache[raw] = username
        return username

    def search(
        self,
        jql: str,
        *,
        fields: list[str] | None = None,
        max_results: int = 100,
    ) -> list[dict]:
        results: list[dict] = []
        start = 0
        while True:
            params: dict[str, Any] = {
                "jql": jql,
                "startAt": start,
                "maxResults": max_results,
            }
            if fields:
                params["fields"] = ",".join(fields)
            data = self.get("/search", params=params)
            issues = data.get("issues") or []
            results.extend(issues)
            total = data.get("total", 0)
            start += len(issues)
            if not issues or start >= total:
                break
        return results


_ISSUE_FIELDS = [
    "summary",
    "description",
    "status",
    "assignee",
    "reporter",
    "created",
    "updated",
    "labels",
    "priority",
    "issuetype",
]


def _normalize_issue(issue: dict) -> dict:
    f = issue.get("fields") or {}
    assignee = f.get("assignee") or {}
    reporter = f.get("reporter") or {}
    status = f.get("status") or {}
    priority = f.get("priority") or {}
    issuetype = f.get("issuetype") or {}
    return {
        "key": issue.get("key"),
        "summary": f.get("summary"),
        "description": f.get("description"),
        "issue_type": issuetype.get("name"),
        "status": status.get("name"),
        "assignee_name": assignee.get("displayName"),
        "assignee_email": assignee.get("emailAddress"),
        "assignee_username": assignee.get("name"),  # Jira-Server login; used for [~name]
        "reporter_name": reporter.get("displayName"),
        "reporter_username": reporter.get("name"),
        "priority": priority.get("name"),
        "labels": f.get("labels") or [],
        "created": f.get("created"),
        "updated": f.get("updated"),
    }


def list_epics(project_key: str, *, client: JiraClient | None = None) -> list[dict]:
    if client is None:
        client = JiraClient.from_env()
    jql = f'project = "{project_key}" AND issuetype = Epic ORDER BY created DESC'
    raw = client.search(jql, fields=_ISSUE_FIELDS)
    return [_normalize_issue(i) for i in raw]


def get_issue(key: str, *, client: JiraClient | None = None) -> dict:
    if client is None:
        client = JiraClient.from_env()
    raw = client.get(f"/issue/{key}", params={"fields": ",".join(_ISSUE_FIELDS)})
    return _normalize_issue(raw)


def list_epic_children(
    epic_key: str, *, client: JiraClient | None = None
) -> list[dict]:
    """Return non-epic issues whose epic-parent is `epic_key`.

    Tries `"Epic Link" = X` (classic Jira Server with Software addon) first,
    falls back to `parent = X` (next-gen / company-managed projects).
    """
    if client is None:
        client = JiraClient.from_env()
    for jql in (
        f'"Epic Link" = {epic_key} ORDER BY created ASC',
        f"parent = {epic_key} ORDER BY created ASC",
    ):
        try:
            raw = client.search(jql, fields=_ISSUE_FIELDS)
        except requests.HTTPError:
            continue
        if raw:
            return [_normalize_issue(i) for i in raw]
    return []
