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
    app = Starlette(
        routes=[
            Route("/api/test", _ok, methods=["GET"]),
            Route("/health", _ok, methods=["GET"]),
        ]
    )
    app.add_middleware(BearerAuth)
    return app


# -- /health always unauthenticated ------------------------------------------


def test_health_never_requires_auth(_db):
    keys.generate(_db, label="test")
    client = TestClient(_app())
    assert client.get("/health").status_code == 200


# -- no keys = all requests rejected -----------------------------------------


def test_no_keys_rejects(_db):
    client = TestClient(_app())
    assert client.get("/api/test").status_code == 401


def test_no_keys_rejects_even_with_token(_db):
    client = TestClient(_app())
    r = client.get("/api/test", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 403


# -- bearer token validation --------------------------------------------------


def test_missing_token_returns_401(_db):
    keys.generate(_db, label="test")
    client = TestClient(_app())
    r = client.get("/api/test")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_wrong_token_returns_403(_db):
    keys.generate(_db, label="test")
    client = TestClient(_app())
    r = client.get("/api/test", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


def test_valid_token_allowed(_db):
    raw = keys.generate(_db, label="test")
    client = TestClient(_app())
    r = client.get("/api/test", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200
    assert r.json()["key_id"] == keys.hash_token(raw)[:8]


def test_bearer_prefix_case_insensitive(_db):
    raw = keys.generate(_db, label="test")
    client = TestClient(_app())
    assert client.get("/api/test", headers={"Authorization": f"bearer {raw}"}).status_code == 200


# -- key lifecycle (no restart needed) ----------------------------------------


def test_new_key_works_immediately(_db):
    client = TestClient(_app())
    assert client.get("/api/test").status_code == 401
    raw = keys.generate(_db, label="new")
    assert client.get("/api/test", headers={"Authorization": f"Bearer {raw}"}).status_code == 200


def test_deleted_key_rejected_immediately(_db):
    raw = keys.generate(_db, label="temp")
    client = TestClient(_app())
    assert client.get("/api/test", headers={"Authorization": f"Bearer {raw}"}).status_code == 200
    keys.delete(_db, keys.hash_token(raw)[:8])
    assert client.get("/api/test", headers={"Authorization": f"Bearer {raw}"}).status_code == 403


# -- unlimited keys -----------------------------------------------------------


def test_unlimited_key_bypasses_rate_limit(_db, monkeypatch):
    monkeypatch.setenv("BIORXIV_MCP_KEY_RATE", "0")
    monkeypatch.setenv("BIORXIV_MCP_KEY_BURST", "1")
    import importlib

    from biorxiv_mcp.server import auth as auth_mod

    importlib.reload(auth_mod)

    raw = keys.generate(_db, label="admin", unlimited=True)
    app = Starlette(routes=[Route("/api/test", _ok, methods=["GET"])])
    app.add_middleware(auth_mod.BearerAuth)
    client = TestClient(app)
    for _ in range(3):
        assert (
            client.get("/api/test", headers={"Authorization": f"Bearer {raw}"}).status_code == 200
        )

    monkeypatch.delenv("BIORXIV_MCP_KEY_RATE", raising=False)
    monkeypatch.delenv("BIORXIV_MCP_KEY_BURST", raising=False)
    importlib.reload(auth_mod)


# -- keys module --------------------------------------------------------------


def test_generate_and_list(_db):
    keys.generate(_db, label="laptop", unlimited=True)
    all_keys = keys.list_keys(_db)
    assert len(all_keys) == 1
    assert all_keys[0].label == "laptop"
    assert all_keys[0].unlimited is True


def test_delete_removes_from_list(_db):
    raw = keys.generate(_db, label="temp")
    kid = keys.hash_token(raw)[:8]
    keys.delete(_db, kid)
    assert keys.list_keys(_db) == []


def test_import_rejects_duplicate(_db):
    raw = keys.generate(_db, label="a")
    with pytest.raises(ValueError, match="already exists"):
        keys.import_token(_db, raw, label="b")
