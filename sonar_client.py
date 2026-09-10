"""Minimal HTTP client for SonarQube (T01 + T02, extended by T16/T17).

Responsibility boundary
-----------------------
* HTTP communication with SonarQube.
* Authentication (token).
* Retrieving SonarQube data (project verification, open issues, and - added for
  the re-analysis stages - the compute-engine task status used to wait for the
  analysis triggered by T16).

All network access to SonarQube lives here; other modules never talk to
SonarQube directly.
"""

from typing import Any, Dict, Optional

import requests


class SonarQubeError(Exception):
    """Raised when a SonarQube request fails or returns an unexpected result."""


class SonarQubeNotFoundError(SonarQubeError):
    """Raised when a SonarQube resource does not exist (HTTP 404).

    A subclass of :class:`SonarQubeError`, so existing callers that catch
    ``SonarQubeError`` keep working unchanged; the added precision lets T17
    distinguish a missing compute-engine task from a temporary API failure.
    """


class SonarClient:
    """Thin, stateless-per-call client for the SonarQube Web API.

    Args:
        sonar_url: Base URL of the SonarQube server (a trailing slash, if any,
            is stripped defensively).
        sonar_token: SonarQube access token used for authentication. The token
            is sent as HTTP Basic auth user (SonarQube convention) and is
            never included in URLs or logged.
        project_key: Key of the SonarQube project this client operates on.
        timeout: Request timeout in seconds. Defaults to
            :attr:`DEFAULT_TIMEOUT_SECONDS`.
    """

    DEFAULT_TIMEOUT_SECONDS: float = 30.0
    DEFAULT_PAGE_SIZE: int = 100

    def __init__(
        self,
        sonar_url: str,
        sonar_token: str,
        project_key: str,
        timeout: Optional[float] = None,
    ) -> None:
        self.sonar_url = sonar_url.rstrip("/")
        self.project_key = project_key
        self.timeout = (
            timeout if timeout is not None else self.DEFAULT_TIMEOUT_SECONDS
        )

        self._session = requests.Session()
        # SonarQube user tokens are accepted as the HTTP Basic username with an
        # empty password (equivalent to ``Authorization: Basic <token>:``).
        self._session.auth = (sonar_token, "")
        self._session.headers["Accept"] = "application/json"
        self._session.headers["User-Agent"] = "sonar-ai-fixer-poc"

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Perform a GET request and return the decoded JSON payload.

        Raises:
            SonarQubeError: on any transport/HTTP error. HTTP error codes are
                surfaced via ``raise_for_status()`` first and then wrapped so
                that callers never deal with ``requests`` exceptions.
        """
        url = f"{self.sonar_url}{path}"
        try:
            response = self._session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise SonarQubeError(f"SonarQube request to '{url}' failed: {exc}") from exc

    def verify_project(self) -> Dict[str, Any]:
        """Verify that the configured project exists and is accessible.

        Returns:
            The SonarQube project component (dict with at least ``key`` and
            ``name``).

        Raises:
            SonarQubeError: if the project cannot be found or accessed.
        """
        data = self._get("/api/projects/search", {"projects": self.project_key})
        components = data.get("components") or []
        matches = [c for c in components if c.get("key") == self.project_key]
        if not matches:
            raise SonarQubeError(
                f"Project '{self.project_key}' was not found or is not "
                f"accessible with the configured token."
            )
        return matches[0]

    def get_open_issues(self, page_size: int = DEFAULT_PAGE_SIZE) -> Dict[str, Any]:
        """Retrieve unresolved (open) issues for the configured project.

        Args:
            page_size: Page size requested from SonarQube ("ps"). A single
                page is returned; iterate/paginate in a later task when the
                full issue set is needed.

        Returns:
            A normalized dict with a predictable structure:

            .. code-block:: python

                {
                    "total": int | None,       # issues matching the query
                    "page_index": int | None,  # 1-based page returned
                    "page_size": int | None,   # page size actually used
                    "issues": [dict, ...],     # raw SonarQube issue dicts
                }

        Raises:
            SonarQubeError: if the request fails.
        """
        params = {
            "componentKeys": self.project_key,
            "resolved": "false",
            "ps": str(page_size),
        }
        data = self._get("/api/issues/search", params)
        return self._normalize_search_response(data, page_size)

    def get_ce_task(self, task_id: str) -> Dict[str, Any]:
        """Retrieve one compute-engine task (added for T16/T17 re-analysis).

        The analysis triggered by T16 is identified by its compute-engine task
        id, so this is how T17 asks "has *this* analysis finished?" instead of
        inspecting project-level summary data.

        Args:
            task_id: compute-engine task id returned by the scanner output.

        Returns:
            The raw ``/api/ce/task`` payload (a dict, normally containing a
            ``task`` object with ``id``/``status``/``analysisId``/...). The
            payload is external data and is validated by the caller.

        Raises:
            SonarQubeError: the task id is empty, or the request failed.
            SonarQubeNotFoundError: the task does not exist (HTTP 404).
        """
        value = str(task_id or "").strip()
        if not value:
            raise SonarQubeError("A compute-engine task id must be provided.")
        url = f"{self.sonar_url}/api/ce/task"
        try:
            response = self._session.get(
                url, params={"id": value}, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise SonarQubeError(f"SonarQube request to '{url}' failed: {exc}") from exc
        if response.status_code == 404:
            raise SonarQubeNotFoundError(
                f"SonarQube compute-engine task '{value}' was not found (HTTP 404)."
            )
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SonarQubeError(f"SonarQube request to '{url}' failed: {exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise SonarQubeError(
                f"SonarQube returned a non-JSON response for '{url}': {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise SonarQubeError(
                f"SonarQube returned an unexpected response for '{url}'."
            )
        return data


    @staticmethod
    def _normalize_search_response(
        data: Dict[str, Any], requested_page_size: int
    ) -> Dict[str, Any]:
        """Normalize issues/search responses across SonarQube API versions."""
        paging = data.get("paging") or {}
        return {
            "total": paging.get("total", data.get("total")),
            "page_index": paging.get("pageIndex", data.get("p")),
            "page_size": paging.get("pageSize", data.get("ps", requested_page_size)),
            "issues": data.get("issues") or [],
        }
