"""Thin HTTP client for the biorxiv-mcp REST API.

Each public method maps 1:1 to a server endpoint. Raises ``ApiError``
for non-2xx responses so the MCP tool layer can surface the status code
and server message.
"""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    """An HTTP error from the biorxiv-mcp REST API."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


class BiorxivApi:
    """Synchronous client for the biorxiv-mcp REST API."""

    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 30.0):
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, **params: Any) -> Any:
        # Drop None-valued params so the server sees them as absent.
        params = {k: v for k, v in params.items() if v is not None}
        r = self._client.get(path, params=params)
        if r.status_code >= 400:
            detail = (
                r.json().get("error", r.text)
                if r.headers.get("content-type", "").startswith("application/json")
                else r.text
            )
            raise ApiError(r.status_code, detail)
        return r.json()

    def _post(self, path: str) -> Any:
        r = self._client.post(path)
        if r.status_code >= 400:
            detail = (
                r.json().get("error", r.text)
                if r.headers.get("content-type", "").startswith("application/json")
                else r.text
            )
            raise ApiError(r.status_code, detail)
        return r.json()

    # -- endpoints ------------------------------------------------------------

    def health(self) -> dict:
        return self._get("/health")

    def search(
        self,
        query: str,
        limit: int = 10,
        category: str | None = None,
        after: str | None = None,
        before: str | None = None,
        detail: bool = False,
        sort: str = "relevance",
    ) -> list[dict]:
        return self._get(
            "/api/search",
            q=query,
            limit=str(limit),
            category=category,
            after=after,
            before=before,
            detail="true" if detail else None,
            sort=sort,
        )

    def search_count(
        self,
        query: str,
        category: str | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> dict:
        return self._get(
            "/api/search/count", q=query, category=category, after=after, before=before
        )

    def categories(self) -> list[dict]:
        return self._get("/api/categories")

    def get_paper(self, doi: str) -> dict:
        return self._get(f"/api/paper/{doi}")

    def download_pdf(self, doi: str) -> bytes:
        """Download a PDF. Returns raw bytes."""
        r = self._client.get(f"/api/paper/{doi}/pdf", timeout=120.0)
        if r.status_code >= 400:
            raise ApiError(r.status_code, r.text[:200])
        if not r.content.startswith(b"%PDF"):
            raise ApiError(502, "Server did not return a valid PDF")
        return r.content

    def status(self) -> dict:
        return self._get("/api/status")

    def sync(self) -> dict:
        return self._post("/api/sync")
