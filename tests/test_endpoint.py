"""Live integration tests against a deployed biorxiv-mcp REST API.

Skipped unless ``BIORXIV_MCP_ENDPOINT`` is set. Example:

    BIORXIV_MCP_ENDPOINT=https://biorxiv.example.com \\
    BIORXIV_MCP_ENDPOINT_KEY=<bearer-token> \\
    uv run pytest tests/test_endpoint.py -v
"""

from __future__ import annotations

import os

import httpx
import pytest

ENDPOINT = os.environ.get("BIORXIV_MCP_ENDPOINT", "").rstrip("/")
KEY = os.environ.get("BIORXIV_MCP_ENDPOINT_KEY", "")

pytestmark = pytest.mark.skipif(
    not ENDPOINT,
    reason="set BIORXIV_MCP_ENDPOINT to run live endpoint tests",
)


@pytest.fixture(scope="module")
def client():
    headers = {}
    if KEY:
        headers["Authorization"] = f"Bearer {KEY}"
    with httpx.Client(base_url=ENDPOINT, timeout=10.0, headers=headers) as c:
        yield c


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["paper_count"] >= 0
    assert "auth_enabled" in body


def test_requires_auth_when_enabled():
    """Unauthenticated requests to /api/* should be rejected when auth is on."""
    with httpx.Client(base_url=ENDPOINT, timeout=10.0) as c:
        r = c.get("/health")
        if not r.json().get("auth_enabled"):
            pytest.skip("endpoint is in open mode")
        r = c.get("/api/categories")
        assert r.status_code == 401


def test_search(client):
    if not KEY:
        pytest.skip("set BIORXIV_MCP_ENDPOINT_KEY for authed tests")
    r = client.get("/api/search", params={"q": "CRISPR", "limit": "2"})
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) <= 2


def test_search_count(client):
    if not KEY:
        pytest.skip("set BIORXIV_MCP_ENDPOINT_KEY for authed tests")
    r = client.get("/api/search/count", params={"q": "CRISPR"})
    assert r.status_code == 200
    assert r.json()["count"] > 0


def test_categories(client):
    if not KEY:
        pytest.skip("set BIORXIV_MCP_ENDPOINT_KEY for authed tests")
    r = client.get("/api/categories")
    assert r.status_code == 200
    assert len(r.json()) > 0


def test_status(client):
    if not KEY:
        pytest.skip("set BIORXIV_MCP_ENDPOINT_KEY for authed tests")
    r = client.get("/api/status")
    assert r.status_code == 200
    assert r.json()["paper_count"] > 0
