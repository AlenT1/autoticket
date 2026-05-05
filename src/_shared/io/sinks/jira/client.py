"""Jira REST API client.

Shared between f2j and jira_task_agent. Bearer (Server/DC) or basic (Cloud)
auth via the ``auth_mode`` parameter.

Auth model:
  - ``auth_mode="bearer"`` (default): ``Authorization: Bearer <PAT>``  (Server/DC)
  - ``auth_mode="basic"``:           ``Authorization: Basic b64(email:token)``  (Cloud)

Construction:
  - :meth:`JiraClient.from_env` — reads ``JIRA_HOST`` + ``JIRA_AUTH_MODE`` +
    ``JIRA_USER_EMAIL`` + autodev token chain (drive's pattern).
  - :meth:`JiraClient.from_config` — explicit args; no env reads (f2j's
    pattern; honors ``ca_bundle``).

Token lookup order for :meth:`from_env` (first hit wins):
  1. ``JIRA_TOKEN`` env var (explicit override)
  2. ``~/.autodev/tokens/task-jira-${JIRA_PROJECT_KEY or REPO_NAME}``
  3. ``~/.autodev/tokens/${REPO_OWNER}-${REPO_NAME}``
  4. ``AUTODEV_TOKEN`` env var
"""
from __future__ import annotations

import base64
import functools
import os
import time
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


def _build_auth_header(
    token: str,
    auth_mode: str | None = None,
    user_email: str | None = None,
) -> str:
    """Build the ``Authorization`` header value.

    ``auth_mode`` and ``user_email`` fall through to env vars
    (``JIRA_AUTH_MODE``, ``JIRA_USER_EMAIL``) when not given — preserves the
    legacy single-arg ``_build_auth_header(token)`` shape that drive's
    :meth:`from_env` expects.
    """
    if auth_mode is None:
        auth_mode = os.environ.get("JIRA_AUTH_MODE", "bearer")
    auth_mode = auth_mode.lower()
    if auth_mode == "basic":
        email = user_email or os.environ.get("JIRA_USER_EMAIL")
        if not email:
            raise RuntimeError(
                "auth_mode='basic' (Jira Cloud) requires user_email "
                "(arg or JIRA_USER_EMAIL env)."
            )
        encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
        return f"Basic {encoded}"
    if auth_mode == "bearer":
        return f"Bearer {token}"
    raise RuntimeError(
        f"unknown auth_mode {auth_mode!r}; expected 'bearer' or 'basic'."
    )


class JiraError(Exception):
    """Raised when Jira returns an unrecoverable error or the client is
    misconfigured (e.g. ``auth_mode='basic'`` without ``user_email``)."""


@dataclass
class WhoamiResult:
    username: str
    display_name: str
    email: str | None


def _retry_transient(fn):
    """Decorator: retry on transient HTTP errors (429 / 502 / 503 / 504) and
    connection / timeout errors. Up to 4 attempts with exponential backoff
    (0.5s, 1s, 2s, 4s — capped). Non-transient HTTPError (4xx auth/perm) is
    re-raised immediately.
    """
    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        last_exc: BaseException | None = None
        for attempt in range(4):
            try:
                return fn(*args, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
            except requests.HTTPError as e:
                resp = getattr(e, "response", None)
                if resp is None or resp.status_code not in {429, 502, 503, 504}:
                    raise
                last_exc = e
            if attempt < 3:
                time.sleep(0.5 * (2 ** attempt))
        assert last_exc is not None
        raise last_exc
    return wrapped


@dataclass
class JiraClient:
    host: str
    auth_header: str
    auth_mode: str
    verify_ssl: bool | str = True

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

    @classmethod
    def from_config(
        cls,
        *,
        url: str,
        pat: str,
        auth_mode: str = "bearer",
        user_email: str | None = None,
        ca_bundle: str | None = None,
    ) -> "JiraClient":
        """Build a client from explicit args (no env reads).

        ``url`` may be a full URL or just a host; both are normalized.
        ``ca_bundle`` is the path to a custom CA bundle for ``requests``
        SSL verification (defaults to system CAs).
        """
        return cls(
            host=_normalize_host(url),
            auth_header=_build_auth_header(
                pat, auth_mode=auth_mode, user_email=user_email
            ),
            auth_mode=auth_mode.lower(),
            verify_ssl=ca_bundle if ca_bundle else True,
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

    @_retry_transient
    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = requests.get(
            f"{self.api_base}{path}",
            headers=self._headers(),
            params=params,
            timeout=30,
            verify=self.verify_ssl,
        )
        r.raise_for_status()
        return r.json()

    @_retry_transient
    def put(self, path: str, json_body: dict[str, Any]) -> None:
        r = requests.put(
            f"{self.api_base}{path}",
            headers=self._headers(),
            json=json_body,
            timeout=30,
            verify=self.verify_ssl,
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

    @_retry_transient
    def post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        r = requests.post(
            f"{self.api_base}{path}",
            headers=self._headers(),
            json=json_body,
            timeout=30,
            verify=self.verify_ssl,
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
                verify=self.verify_ssl,
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
        parent_key: str | None = None,
        extra_fields: dict[str, Any] | None = None,
        add_ai_generated_label: bool = True,
    ) -> dict[str, Any]:
        """POST /issue. Returns Jira's response: {id, key, self}.

        ``add_ai_generated_label`` controls whether the ``ai-generated`` label
        is auto-added (defaults to True for jira_task_agent's flow; f2j's
        bug-ticket flow opts out via False so Bug tickets don't carry the
        marker). Caller can also override ``labels`` via ``extra_fields``.

        ``parent_key`` is for Sub-task issuetypes only — sets ``fields.parent``
        directly. For non-Sub-task issues with a parent epic, use
        ``epic_link`` (which routes via the Epic Link customfield).

        For ``issue_type="Epic"`` on Jira Server, automatically discovers and
        populates the Epic Name customfield with the summary. (Epic Name
        is required on epic creation; Server / Software addon.)
        """
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "description": self._md_to_jira_wiki(description),
            "issuetype": {"name": issue_type},
        }
        if add_ai_generated_label:
            fields["labels"] = ["ai-generated"]
        if parent_key:
            # Sub-task parent linkage; not via Epic Link.
            fields["parent"] = {"key": parent_key}
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

    # -- Components + user picker (used by f2j strategies) ----------------

    def get_components(self, project_key: str) -> list[dict[str, Any]]:
        """GET /project/{key}/components. Returns the live list of project
        components (each has ``id`` and ``name`` at minimum). Used by f2j's
        upload path to filter out invented component names against the
        project's actual component set.
        """
        data = self.get(f"/project/{project_key}/components")
        # The endpoint returns a JSON array, but `get()` deserializes either
        # a dict or list — normalize to list-of-dict.
        if isinstance(data, list):
            return [c for c in data if isinstance(c, dict)]
        return []

    def search_user_picker(self, query: str) -> list[dict[str, Any]]:
        """GET /user/picker?query=... — more lenient than /user/search on
        Jira Server installs that return empty for many display-name queries.
        Returns a list of `{name, displayName, emailAddress, ...}` dicts.

        Used by f2j's PickerWithCache assignee resolver.
        """
        if not query:
            return []
        try:
            data = self.get("/user/picker", params={"query": query})
        except requests.HTTPError:
            return []
        users = data.get("users") if isinstance(data, dict) else None
        if not isinstance(users, list):
            return []
        return [u for u in users if isinstance(u, dict)]

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

    # -- f2j diagnostic / attachment surface ------------------------------

    def whoami(self) -> WhoamiResult:
        """Return information about the authenticated user.

        Server returns ``name``, Cloud returns ``accountId``. Falls back
        ``name → key → accountId`` so the same call works on both.
        """
        data = self.get("/myself")
        return WhoamiResult(
            username=data.get("name") or data.get("key") or data.get("accountId", ""),
            display_name=data.get("displayName", ""),
            email=data.get("emailAddress"),
        )

    def create_meta(self, project_key: str) -> dict[str, Any]:
        """GET /issue/createmeta?projectKeys=...&expand=projects.issuetypes.fields.

        Used by f2j's ``jira fields`` diagnostic to discover custom field IDs
        for the configured project + issue type.
        """
        return self.get(
            "/issue/createmeta",
            params={
                "projectKeys": project_key,
                "expand": "projects.issuetypes.fields",
            },
        )

    def issue_browse_url(self, key: str) -> str:
        """Return the human-friendly URL for ``key``
        (e.g. ``https://jira.example.com/browse/KEY-123``)."""
        return f"https://{self.host}/browse/{key}"

    def download_attachment(
        self,
        attachment_url: str,
        dest: Path,
        *,
        max_bytes: int = 5_000_000,
    ) -> int:
        """Download a Jira attachment to ``dest``. Returns bytes written.

        Refuses to write more than ``max_bytes`` (default 5 MB) so attachments
        don't balloon the agent's context if read back via the Read tool.
        Reuses the client's auth header so we don't redo the bearer/basic
        dispatch.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(
            attachment_url,
            headers=self._headers(),
            stream=True,
            allow_redirects=True,
            timeout=60,
            verify=self.verify_ssl,
        ) as resp:
            resp.raise_for_status()
            written = 0
            with dest.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=65_536):
                    if not chunk:
                        continue
                    if written + len(chunk) > max_bytes:
                        f.write(chunk[: max_bytes - written])
                        written = max_bytes
                        break
                    f.write(chunk)
                    written += len(chunk)
            return written

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
