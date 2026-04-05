"""Live integration tests against a deployed biorxiv-mcp endpoint.

Skipped unless ``BIORXIV_MCP_ENDPOINT`` is set. Example:

    BIORXIV_MCP_ENDPOINT=https://biorxiv.example.com \\
    BIORXIV_MCP_ENDPOINT_KEY=<bearer-token> \\
    uv run pytest tests/test_endpoint.py -v

These tests verify the full stack: TLS, reverse proxy, bearer auth, and
MCP initialize handshake. They do not exercise the database — for that,
see test_db.py / test_server.py.
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

_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "1"},
    },
}
_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=ENDPOINT, timeout=10.0) as c:
        yield c


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["paper_count"] >= 0
    assert "auth_enabled" in body


def test_mcp_requires_auth_when_enabled(client):
    r = client.get("/health")
    if not r.json().get("auth_enabled"):
        pytest.skip("endpoint is in open mode; auth tests not applicable")
    r = client.post("/mcp", json=_INIT_BODY, headers=_MCP_HEADERS)
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_mcp_rejects_wrong_key(client):
    r = client.get("/health")
    if not r.json().get("auth_enabled"):
        pytest.skip("endpoint is in open mode")
    r = client.post(
        "/mcp",
        json=_INIT_BODY,
        headers={**_MCP_HEADERS, "Authorization": "Bearer not-a-real-key"},
    )
    assert r.status_code == 403


def test_mcp_initialize_with_valid_key(client):
    if not KEY:
        pytest.skip("set BIORXIV_MCP_ENDPOINT_KEY to exercise authed /mcp")
    r = client.post(
        "/mcp",
        json=_INIT_BODY,
        headers={**_MCP_HEADERS, "Authorization": f"Bearer {KEY}"},
    )
    assert r.status_code == 200
    # Streamable HTTP returns an SSE frame for initialize; the "data:" line
    # carries the JSON-RPC response.
    body = r.text
    assert "serverInfo" in body
    assert "biorxiv" in body
