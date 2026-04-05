"""Tests for bearer-token authentication + per-key rate limiting."""

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from biorxiv_mcp.server.auth import ApiKey, BearerAuth, hash_token, load_keys


def _ok(request):
    key_id = getattr(request.state, "key_id", None)
    return JSONResponse({"ok": True, "key_id": key_id})


def _keyset(*raw_tokens, unlimited: tuple[str, ...] = ()):
    keys = {}
    for t in raw_tokens:
        h = hash_token(t)
        keys[h] = ApiKey(hash=h, unlimited=False)
    for t in unlimited:
        h = hash_token(t)
        keys[h] = ApiKey(hash=h, unlimited=True)
    return keys


def _app(keys=None):
    app = Starlette(routes=[
        Route("/mcp", _ok, methods=["GET"]),
        Route("/health", _ok, methods=["GET"]),
    ])
    app.add_middleware(BearerAuth, keys=keys)
    return app


# -- load_keys ----------------------------------------------------------------


def test_load_keys_empty():
    assert load_keys("", "") == {}
    assert load_keys("   ,  ,", "") == {}


def test_load_keys_hashes_and_dedupes():
    keys = load_keys("alpha, beta, alpha", "")
    assert len(keys) == 2
    assert all(not k.unlimited for k in keys.values())
    assert hash_token("alpha") in keys
    assert hash_token("beta") in keys


def test_load_keys_merges_unlimited():
    keys = load_keys("alpha", "admin")
    assert len(keys) == 2
    assert keys[hash_token("alpha")].unlimited is False
    assert keys[hash_token("admin")].unlimited is True


def test_load_keys_unlimited_overrides_api():
    # A token in both lists is unlimited.
    keys = load_keys("key", "key")
    assert len(keys) == 1
    assert keys[hash_token("key")].unlimited is True


def test_api_key_key_id():
    k = ApiKey(hash="abcdef1234567890" * 4, unlimited=False)
    assert k.key_id == "abcdef12"


# -- open mode (no keys configured) -------------------------------------------


def test_open_mode_allows_requests():
    client = TestClient(_app(keys={}))
    r = client.get("/mcp")
    assert r.status_code == 200
    assert r.json()["key_id"] == "anonymous"


def test_health_never_requires_auth():
    client = TestClient(_app(keys=_keyset("secret")))
    r = client.get("/health")
    assert r.status_code == 200


# -- bearer token validation --------------------------------------------------


def test_missing_token_returns_401():
    client = TestClient(_app(keys=_keyset("secret")))
    r = client.get("/mcp")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_wrong_token_returns_403():
    client = TestClient(_app(keys=_keyset("secret")))
    r = client.get("/mcp", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


def test_valid_token_allowed():
    client = TestClient(_app(keys=_keyset("secret")))
    r = client.get("/mcp", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    assert r.json()["key_id"] == hash_token("secret")[:8]


def test_bearer_prefix_case_insensitive():
    client = TestClient(_app(keys=_keyset("secret")))
    r = client.get("/mcp", headers={"Authorization": "bearer secret"})
    assert r.status_code == 200


def test_whitespace_around_token_stripped():
    client = TestClient(_app(keys=_keyset("secret")))
    r = client.get("/mcp", headers={"Authorization": "Bearer  secret  "})
    assert r.status_code == 200


# -- per-key rate limiting ----------------------------------------------------


def test_per_key_rate_limit(monkeypatch):
    monkeypatch.setenv("BIORXIV_MCP_KEY_RATE", "0")
    monkeypatch.setenv("BIORXIV_MCP_KEY_BURST", "2")
    import importlib
    from biorxiv_mcp.server import auth as auth_mod
    importlib.reload(auth_mod)

    app = Starlette(routes=[Route("/mcp", _ok, methods=["GET"])])
    app.add_middleware(
        auth_mod.BearerAuth,
        keys={auth_mod.hash_token("k"): auth_mod.ApiKey(hash=auth_mod.hash_token("k"))},
    )
    client = TestClient(app)
    h = {"Authorization": "Bearer k"}
    assert client.get("/mcp", headers=h).status_code == 200
    assert client.get("/mcp", headers=h).status_code == 200
    r = client.get("/mcp", headers=h)
    assert r.status_code == 429
    assert "Retry-After" in r.headers

    monkeypatch.delenv("BIORXIV_MCP_KEY_RATE", raising=False)
    monkeypatch.delenv("BIORXIV_MCP_KEY_BURST", raising=False)
    importlib.reload(auth_mod)


def test_unlimited_key_bypasses_rate_limit(monkeypatch):
    monkeypatch.setenv("BIORXIV_MCP_KEY_RATE", "0")
    monkeypatch.setenv("BIORXIV_MCP_KEY_BURST", "1")
    import importlib
    from biorxiv_mcp.server import auth as auth_mod
    importlib.reload(auth_mod)

    h = auth_mod.hash_token("admin")
    keys = {h: auth_mod.ApiKey(hash=h, unlimited=True)}
    app = Starlette(routes=[Route("/mcp", _ok, methods=["GET"])])
    app.add_middleware(auth_mod.BearerAuth, keys=keys)
    client = TestClient(app)
    for _ in range(3):
        assert client.get("/mcp", headers={"Authorization": "Bearer admin"}).status_code == 200

    monkeypatch.delenv("BIORXIV_MCP_KEY_RATE", raising=False)
    monkeypatch.delenv("BIORXIV_MCP_KEY_BURST", raising=False)
    importlib.reload(auth_mod)
