"""Tests for bearer-token authentication + per-key rate limiting."""

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from biorxiv_mcp.auth import BearerAuth, _hash_key, load_keys


def _ok(request):
    key_id = getattr(request.state, "key_id", None)
    return JSONResponse({"ok": True, "key_id": key_id})


def _app(keys=None):
    app = Starlette(routes=[
        Route("/mcp", _ok, methods=["GET"]),
        Route("/health", _ok, methods=["GET"]),
    ])
    app.add_middleware(BearerAuth, keys=keys)
    return app


# -- load_keys ----------------------------------------------------------------


def test_load_keys_empty():
    assert load_keys("") == set()
    assert load_keys("   ,  ,") == set()


def test_load_keys_hashes_and_dedupes():
    keys = load_keys("alpha, beta, alpha")
    assert len(keys) == 2
    assert _hash_key("alpha") in keys
    assert _hash_key("beta") in keys


# -- open mode (no keys configured) -------------------------------------------


def test_open_mode_allows_requests():
    client = TestClient(_app(keys=set()))
    r = client.get("/mcp")
    assert r.status_code == 200
    assert r.json()["key_id"] == "anonymous"


def test_health_never_requires_auth():
    client = TestClient(_app(keys={_hash_key("secret")}))
    r = client.get("/health")
    assert r.status_code == 200


# -- bearer token validation --------------------------------------------------


def test_missing_token_returns_401():
    client = TestClient(_app(keys={_hash_key("secret")}))
    r = client.get("/mcp")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_wrong_token_returns_403():
    client = TestClient(_app(keys={_hash_key("secret")}))
    r = client.get("/mcp", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


def test_valid_token_allowed():
    client = TestClient(_app(keys={_hash_key("secret")}))
    r = client.get("/mcp", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    assert r.json()["key_id"] == _hash_key("secret")[:8]


def test_bearer_prefix_case_insensitive():
    client = TestClient(_app(keys={_hash_key("secret")}))
    r = client.get("/mcp", headers={"Authorization": "bearer secret"})
    assert r.status_code == 200


def test_whitespace_around_token_stripped():
    client = TestClient(_app(keys={_hash_key("secret")}))
    r = client.get("/mcp", headers={"Authorization": "Bearer  secret  "})
    assert r.status_code == 200


# -- per-key rate limiting ----------------------------------------------------


def test_per_key_rate_limit(monkeypatch):
    # Tight budget: burst=2, refill 0/s for the duration of the test.
    monkeypatch.setenv("BIORXIV_MCP_KEY_RATE", "0")
    monkeypatch.setenv("BIORXIV_MCP_KEY_BURST", "2")
    # Re-import constants by rebuilding the middleware after env change.
    import importlib
    from biorxiv_mcp import auth as auth_mod
    importlib.reload(auth_mod)

    app = Starlette(routes=[Route("/mcp", _ok, methods=["GET"])])
    app.add_middleware(auth_mod.BearerAuth, keys={auth_mod._hash_key("k")})
    client = TestClient(app)
    h = {"Authorization": "Bearer k"}
    assert client.get("/mcp", headers=h).status_code == 200
    assert client.get("/mcp", headers=h).status_code == 200
    r = client.get("/mcp", headers=h)
    assert r.status_code == 429
    assert "Retry-After" in r.headers

    # Restore defaults for other tests.
    monkeypatch.delenv("BIORXIV_MCP_KEY_RATE", raising=False)
    monkeypatch.delenv("BIORXIV_MCP_KEY_BURST", raising=False)
    importlib.reload(auth_mod)


def test_unlimited_key_bypasses_rate_limit(monkeypatch):
    monkeypatch.setenv("BIORXIV_MCP_KEY_RATE", "0")
    monkeypatch.setenv("BIORXIV_MCP_KEY_BURST", "1")
    import importlib
    from biorxiv_mcp import auth as auth_mod
    importlib.reload(auth_mod)

    unlimited = {auth_mod._hash_key("admin")}
    app = Starlette(routes=[Route("/mcp", _ok, methods=["GET"])])
    app.add_middleware(auth_mod.BearerAuth, keys=set(), unlimited_keys=unlimited)
    client = TestClient(app)
    h = {"Authorization": "Bearer admin"}
    # All three requests succeed despite burst=1.
    for _ in range(3):
        assert client.get("/mcp", headers=h).status_code == 200

    monkeypatch.delenv("BIORXIV_MCP_KEY_RATE", raising=False)
    monkeypatch.delenv("BIORXIV_MCP_KEY_BURST", raising=False)
    importlib.reload(auth_mod)


def test_unlimited_key_is_auto_valid():
    # Unlimited keys don't need to also be listed in keys=.
    from biorxiv_mcp.auth import BearerAuth, _hash_key
    app = Starlette(routes=[Route("/mcp", _ok, methods=["GET"])])
    app.add_middleware(BearerAuth, keys=set(), unlimited_keys={_hash_key("admin")})
    client = TestClient(app)
    assert client.get("/mcp", headers={"Authorization": "Bearer admin"}).status_code == 200
    assert client.get("/mcp", headers={"Authorization": "Bearer other"}).status_code == 403
