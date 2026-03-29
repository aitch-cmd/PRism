"""
Thin async wrapper around httpx for GitHub API calls.

All GitHub REST API logic lives here. MCP tools NEVER construct
API URLs or handle pagination directly — they call methods on
GitHubClient instead.

Swap PAT → OAuth later by changing only this file.
"""

from __future__ import annotations

from typing import Any

import httpx

from logger import get_logger

logger = get_logger("prism.github_client")

GITHUB_API = "https://api.github.com"


class GitHubClientError(Exception):
    """Raised when a GitHub API call fails."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"GitHub API {status_code}: {message}")


class GitHubClient:
    """Async GitHub REST API client with connection pooling & pagination."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Fire a single request; raise on non-2xx."""
        resp = await self._client.request(method, path, params=params, headers=headers)
        if resp.status_code >= 400:
            body = resp.text[:300]
            logger.error(
                "GitHub API error  method=%s path=%s status=%s body=%s",
                method,
                path,
                resp.status_code,
                body,
            )
            raise GitHubClientError(resp.status_code, body)
        return resp

    async def _get_json(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> Any:
        resp = await self._request("GET", path, params=params)
        return resp.json()

    async def _get_paginated(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        max_pages: int = 3,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Follow GitHub's Link-header pagination up to *max_pages* pages.
        Returns a flat list of all items across pages.
        """
        params = dict(params or {})
        params.setdefault("per_page", per_page)

        results: list[dict[str, Any]] = []
        url: str | None = path

        for page_num in range(1, max_pages + 1):
            logger.debug("Fetching page %d of %s", page_num, path)
            resp = await self._request("GET", url, params=params if page_num == 1 else None)
            data = resp.json()

            if isinstance(data, list):
                results.extend(data)
            else:
                # Some endpoints wrap items in a key (e.g. search)
                results.append(data)

            # Follow rel="next" link if present
            link = resp.headers.get("link", "")
            next_url = self._parse_next_link(link)
            if next_url is None:
                break
            url = next_url
            params = None  # params are baked into the next URL

        return results

    @staticmethod
    def _parse_next_link(link_header: str) -> str | None:
        """Extract the URL for rel='next' from a GitHub Link header."""
        for part in link_header.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                return url
        return None

    # ------------------------------------------------------------------
    # Auth check
    # ------------------------------------------------------------------
    async def get_authenticated_user(self) -> dict[str, Any]:
        """GET /user — validates the token and returns profile info."""
        return await self._get_json("/user")

    # ------------------------------------------------------------------
    # Repos
    # ------------------------------------------------------------------
    async def get_repos(
        self,
        *,
        sort: str = "updated",
        direction: str = "desc",
        repo_type: str = "all",
        max_pages: int = 3,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """
        GET /user/repos — all repos accessible to the authenticated user.

        Paginates up to *max_pages* pages of *per_page* results each.
        Default: up to 300 repos sorted by most-recently-updated.
        """
        params = {
            "sort": sort,
            "direction": direction,
            "type": repo_type,
        }
        return await self._get_paginated(
            "/user/repos", params=params, max_pages=max_pages, per_page=per_page
        )

    # ------------------------------------------------------------------
    # Issues  (placeholder — will be fleshed out in Step 5)
    # ------------------------------------------------------------------
    async def get_issues(
        self,
        repo: str,
        *,
        state: str = "open",
        labels: str | None = None,
        milestone: str | None = None,
        max_pages: int = 3,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """
        GET /repos/{owner}/{repo}/issues

        *repo* must be in "owner/name" format.
        """
        params: dict[str, Any] = {"state": state}
        if labels:
            params["labels"] = labels
        if milestone:
            params["milestone"] = milestone

        return await self._get_paginated(
            f"/repos/{repo}/issues",
            params=params,
            max_pages=max_pages,
            per_page=per_page,
        )

    # ------------------------------------------------------------------
    # Pull Requests  (placeholder — will be fleshed out in Step 6-7)
    # ------------------------------------------------------------------
    async def get_prs(
        self,
        repo: str,
        *,
        state: str = "open",
        max_pages: int = 3,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /repos/{owner}/{repo}/pulls"""
        params: dict[str, Any] = {"state": state}
        return await self._get_paginated(
            f"/repos/{repo}/pulls",
            params=params,
            max_pages=max_pages,
            per_page=per_page,
        )

    async def get_pr_diff(
        self, repo: str, pr_number: int
    ) -> str:
        """GET /repos/{owner}/{repo}/pulls/{pr_number} with diff media type."""
        resp = await self._request(
            "GET",
            f"/repos/{repo}/pulls/{pr_number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        return resp.text

    # ------------------------------------------------------------------
    # Orgs
    # ------------------------------------------------------------------
    async def get_user_orgs(self, max_pages: int = 3) -> list[dict[str, Any]]:
        """GET /user/orgs"""
        return await self._get_paginated("/user/orgs", max_pages=max_pages)
