"""Thin wrapper around atlassian-python-api with retry + clean error surfacing.

We intentionally keep the public API small (whoami / discover / create / search /
add_attachment) so it's easy to mock in tests and easy to swap the backend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atlassian import Jira
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)


class JiraError(Exception):
    """Raised when Jira returns an unrecoverable error."""


@dataclass
class WhoamiResult:
    username: str
    display_name: str
    email: str | None


def _is_retryable(exc: BaseException) -> bool:
    """Decide whether to retry a Jira API exception.

    atlassian-python-api wraps requests; transient HTTP errors usually surface
    via ``requests.exceptions.HTTPError`` with a status_code on ``response``.
    """
    import requests

    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError):
        resp = getattr(exc, "response", None)
        if resp is not None and resp.status_code in {429, 502, 503, 504}:
            return True
    return False


_retry_transient = retry(
    retry=retry_if_exception_type((Exception,)),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    reraise=True,
)


class JiraClient:
    """Wrapper around ``atlassian.Jira`` with bearer (Server/DC) or basic (Cloud) auth.

    Two HTTP-level defenses borrowed from a sibling project's hard-won
    experience:

    - ``Accept-Encoding: identity`` is forced on every request. Some corporate
      proxies (NVIDIA's historically) mangle gzip-encoded responses and the
      atlassian library receives garbled JSON. Disabling compression sidesteps
      it at the cost of a few extra bytes on the wire.
    - Auth-mode dispatch chooses between bearer (Server/DC PAT) and basic
      (Cloud email+token).
    """

    def __init__(
        self,
        url: str,
        *,
        pat: str | None = None,
        auth_mode: str = "bearer",
        user_email: str | None = None,
        ca_bundle: str | None = None,
        timeout: int = 30,
        client: Any | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.auth_mode = auth_mode

        if client is not None:
            self._client = client
            return

        jira_kwargs: dict[str, Any] = {
            "url": self.url,
            "verify_ssl": ca_bundle if ca_bundle else True,
            "timeout": timeout,
        }
        if auth_mode == "basic":
            if not user_email:
                raise JiraError(
                    "auth_mode='basic' (Jira Cloud) requires user_email; "
                    "set jira.user_email in f2j.yaml."
                )
            jira_kwargs["username"] = user_email
            jira_kwargs["password"] = pat
        elif auth_mode == "bearer":
            jira_kwargs["token"] = pat
        else:
            raise JiraError(
                f"unknown auth_mode {auth_mode!r}; expected 'bearer' or 'basic'."
            )

        self._client = Jira(**jira_kwargs)
        # Force identity encoding on the underlying requests session so corporate
        # proxies don't return a gzipped body the library can't decode.
        session = getattr(self._client, "_session", None) or getattr(
            self._client, "session", None
        )
        if session is not None:
            session.headers["Accept-Encoding"] = "identity"

    # ------------------------------------------------------------------
    # Read API

    @_retry_transient
    def whoami(self) -> WhoamiResult:
        """Return information about the authenticated user."""
        data = self._client.myself()
        return WhoamiResult(
            username=data.get("name") or data.get("key") or data.get("accountId", ""),
            display_name=data.get("displayName", ""),
            email=data.get("emailAddress"),
        )

    @_retry_transient
    def server_info(self) -> dict[str, Any]:
        return self._client.get_server_info()

    @_retry_transient
    def create_meta(self, project_key: str) -> dict[str, Any]:
        """GET /rest/api/2/issue/createmeta with field expansion."""
        method = getattr(self._client, "issue_createmeta", None)
        if method is None:
            # Older atlassian-python-api: fall back to raw GET.
            return self._client.get(
                "rest/api/2/issue/createmeta",
                params={"projectKeys": project_key, "expand": "projects.issuetypes.fields"},
            )
        return method(project=project_key, expand="projects.issuetypes.fields")

    @_retry_transient
    def search_by_jql(self, jql: str, *, fields: list[str] | None = None, limit: int = 5) -> dict[str, Any]:
        return self._client.jql(jql, fields=",".join(fields or ["summary"]), limit=limit)

    @_retry_transient
    def list_project_components(self, project_key: str) -> list[str]:
        """Return the project's component names (empty list if none configured)."""
        try:
            data = self._client.get(f"rest/api/2/project/{project_key}/components")
        except Exception:  # noqa: BLE001 — components are optional metadata
            return []
        if not isinstance(data, list):
            return []
        return [c.get("name", "") for c in data if isinstance(c, dict) and c.get("name")]

    @_retry_transient
    def search_user(self, query: str) -> list[dict[str, Any]]:
        """Find Jira users matching ``query`` (display name, username, or email).

        Prefers ``/rest/api/2/user/picker`` because it does smart prefix
        matching across all name fields (verified to find users like "Yair
        Sadan" → ``ysadan`` on NVIDIA jirasw where ``/user/search`` returns
        empty for the same query). Falls back to ``/user/search`` if the
        picker is not available on this Jira instance.
        """
        try:
            picker = self._client.get(
                "rest/api/2/user/picker", params={"query": query, "maxResults": 5}
            )
            if isinstance(picker, dict) and "users" in picker:
                return picker["users"]
        except Exception as e:  # noqa: BLE001
            log.warning("user picker failed for %r: %s", query, e)
        try:
            result = self._client.user_find_by_user_string(query=query, start=0, limit=5)
        except Exception as e:  # noqa: BLE001
            log.warning("user search failed for %r: %s", query, e)
            return []
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "users" in result:
            return result["users"]
        return []

    # ------------------------------------------------------------------
    # Write API

    @_retry_transient
    def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        return self._client.issue_create(fields=fields)

    def issue_browse_url(self, key: str) -> str:
        return f"{self.url}/browse/{key}"

    @_retry_transient
    def get_issue_with_attachments(self, key: str) -> dict[str, Any]:
        """Fetch an issue including its attachments field.

        Returns the raw JSON shape from ``GET /rest/api/2/issue/{key}``. Used
        when the agent needs to pull screenshots / logs / PDFs attached to an
        upstream ticket referenced by an input bug.
        """
        return self._client.get(
            f"rest/api/2/issue/{key}",
            params={"fields": "summary,description,attachment,subtasks,issuelinks"},
        )

    @_retry_transient
    def download_attachment(
        self, attachment_url: str, dest: Path, *, max_bytes: int = 5_000_000
    ) -> int:
        """Download a Jira attachment to ``dest``. Returns bytes written.

        Refuses to write more than ``max_bytes`` (defaults to 5 MB to match the
        sibling project's convention) — large files would balloon the agent's
        context if read back via the Read tool.

        Auth header is reused from the underlying atlassian session so we
        don't redo the bearer/basic dispatch.
        """
        session = getattr(self._client, "_session", None) or getattr(
            self._client, "session", None
        )
        if session is None:
            raise JiraError("internal: no session on atlassian client")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with session.get(
            attachment_url, stream=True, allow_redirects=True, timeout=60
        ) as resp:
            resp.raise_for_status()
            written = 0
            with dest.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=65_536):
                    if not chunk:
                        continue
                    if written + len(chunk) > max_bytes:
                        # Truncate to cap so the partial file is still readable
                        # rather than half-written. Caller decides whether to
                        # use it.
                        f.write(chunk[: max_bytes - written])
                        written = max_bytes
                        break
                    f.write(chunk)
                    written += len(chunk)
            return written
