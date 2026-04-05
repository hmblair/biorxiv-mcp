"""Tests for bearer-token authentication + per-key rate limiting."""

import sqlite3
from contextlib import contextmanager

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from biorxiv_mcp.server import db, keys
from biorxiv_mcp.server.auth import BearerAuth


def _ok(request):
    key_id = getattr(request.state, "key_id", None)
    return JSONResponse({"ok": True, "key_id": key_id})


@pytest.fixture()
def _db(monkeypatch):
    """Provide an in-memory DB with the api_keys table, patched as the shared connection."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db._initialized_ids.discard(id(conn))
    db.init_db(conn)

    @contextmanager
    def fake():
        yield conn

    monkeypatch.setattr(db, "connection", fake)
    yield conn
    conn.close()


def _app():
    app = Starlette(routes=[
        Route("/mcp", _ok, methods=["GET"]),
        Route("/health", _ok, methods=["GET"]),
    ])
    app.add_middleware(BearerAuth)
    return app


# -- open mode (no keys in DB) ------------------------------------------------

def test_open_mode_allows_requests(_db):
    client = TestClient(_app())
    r = client.get("/mcp")
    assert r.status_code == 200
    assert r.json()["key_id"] == "anonymous"


def test_health_never_requires_auth(_db):
    conn = _db
    raw = keys.generate(conn, label="test", unlimited=False)
    client = TestClient(_app())
    r = client.get("/health")
    assert r.status_code == 200


# -- bearer token validation --------------------------------------------------

def test_missing_token_returns_401(_db):
    keys.generate(_db, label="test")
    client = TestClient(_app())
    r = client.get("/mcp")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_wrong_token_returns_403(_db):
    keys.generate(_db, label="test")
    client = TestClient(_app())
    r = client.get("/mcp", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


def test_valid_token_allowed(_db):
    raw = keys.generate(_db, label="test")
    client = TestClient(_app())
    r = client.get("/mcp", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200
    assert r.json()["key_id"] == keys.hash_token(raw)[:8]


def test_bearer_prefix_case_insensitive(_db):
    raw = keys.generate(_db, label="test")
    client = TestClient(_app())
    r = client.get("/mcp", headers={"Authorization": f"bearer {raw}"})
    assert r.status_code == 200


def test_revoked_key_rejected(_db):
    raw = keys.generate(_db, label="revokeme")
    kid = keys.hash_token(raw)[:8]
    keys.revoke(_db, kid)
    client = TestClient(_app())
    r = client.get("/mcp", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 403


def test_key_added_without_restart(_db):
    """New keys are visible immediately — no restart needed."""
    client = TestClient(_app())
    # Start in open mode.
    r = client.get("/mcp")
    assert r.status_code == 200
    assert r.json()["key_id"] == "anonymous"
    # Add a key — now auth is required.
    raw = keys.generate(_db, label="new")
    r = client.get("/mcp")
    assert r.status_code == 401
    r = client.get("/mcp", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200


# -- unlimited keys -----------------------------------------------------------

def test_unlimited_key_bypasses_rate_limit(_db, monkeypatch):
    monkeypatch.setenv("BIORXIV_MCP_KEY_RATE", "0")
    monkeypatch.setenv("BIORXIV_MCP_KEY_BURST", "1")
    import importlib
    from biorxiv_mcp.server import auth as auth_mod
    importlib.reload(auth_mod)

    raw = keys.generate(_db, label="admin", unlimited=True)
    app = Starlette(routes=[Route("/mcp", _ok, methods=["GET"])])
    app.add_middleware(auth_mod.BearerAuth)
    client = TestClient(app)
    for _ in range(3):
        assert client.get("/mcp", headers={"Authorization": f"Bearer {raw}"}).status_code == 200

    monkeypatch.delenv("BIORXIV_MCP_KEY_RATE", raising=False)
    monkeypatch.delenv("BIORXIV_MCP_KEY_BURST", raising=False)
    importlib.reload(auth_mod)


# -- keys module --------------------------------------------------------------

def test_generate_and_list(_db):
    raw = keys.generate(_db, label="laptop", unlimited=True)
    all_keys = keys.list_keys(_db)
    assert len(all_keys) == 1
    assert all_keys[0].label == "laptop"
    assert all_keys[0].unlimited is True
    assert all_keys[0].disabled is False


def test_revoke_hides_from_list(_db):
    raw = keys.generate(_db, label="temp")
    kid = keys.hash_token(raw)[:8]
    keys.revoke(_db, kid)
    assert keys.list_keys(_db) == []
    assert len(keys.list_keys(_db, include_disabled=True)) == 1


def test_load_active_excludes_disabled(_db):
    r1 = keys.generate(_db, label="a")
    r2 = keys.generate(_db, label="b")
    keys.revoke(_db, keys.hash_token(r1)[:8])
    active = keys.load_active(_db)
    assert keys.hash_token(r1) not in active
    assert keys.hash_token(r2) in active
