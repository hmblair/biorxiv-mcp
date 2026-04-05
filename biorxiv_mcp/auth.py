"""Bearer-token authentication and per-key rate limiting.

API keys are supplied via environment variables, hashed at startup, and
looked up in constant time on each request. Each key carries an
``unlimited`` flag controlling whether the per-key rate limit applies.

If no keys are configured, the middleware allows all requests through
but logs a warning — this preserves the local/stdio workflow where auth
is unnecessary.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .ratelimit import TokenBucket

logger = logging.getLogger(__name__)

# Paths that bypass auth (liveness checks).
_UNAUTHED_PATHS = frozenset({"/health"})

# Per-key request budget. Generous enough for normal agent usage; tight
# enough to make brute-forcing infeasible. Configurable via env vars.
_KEY_RATE = float(os.environ.get("BIORXIV_MCP_KEY_RATE", "1.0"))       # req/sec
_KEY_BURST = int(os.environ.get("BIORXIV_MCP_KEY_BURST", "60"))        # bucket size
# Shared bucket for unauthenticated (open-mode) requests, keyed by client IP.
_ANON_RATE = float(os.environ.get("BIORXIV_MCP_ANON_RATE", "0.5"))
_ANON_BURST = int(os.environ.get("BIORXIV_MCP_ANON_BURST", "30"))


@dataclass(frozen=True, slots=True)
class ApiKey:
    """An authenticated API key.

    ``hash`` is the SHA-256 hex digest of the raw token. ``unlimited``
    keys bypass the per-key rate limit.
    """
    hash: str
    unlimited: bool = False

    @property
    def key_id(self) -> str:
        """Short, safe-to-log identifier (first 8 chars of the hash)."""
        return self.hash[:8]


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _split(env_value: str) -> list[str]:
    return [t.strip() for t in env_value.split(",") if t.strip()]


def load_keys(
    api_env: str | None = None,
    unlimited_env: str | None = None,
) -> dict[str, ApiKey]:
    """Load API keys from environment variables.

    Reads ``BIORXIV_MCP_API_KEYS`` (rate-limited) and
    ``BIORXIV_MCP_UNLIMITED_KEYS`` (bypass rate limiting). If a token
    appears in both, the unlimited flag wins.

    Returns a dict keyed by SHA-256 hex digest. An empty dict means
    auth is disabled.
    """
    if api_env is None:
        api_env = os.environ.get("BIORXIV_MCP_API_KEYS", "")
    if unlimited_env is None:
        unlimited_env = os.environ.get("BIORXIV_MCP_UNLIMITED_KEYS", "")
    keys: dict[str, ApiKey] = {}
    for raw in _split(api_env):
        h = _hash_token(raw)
        keys[h] = ApiKey(hash=h, unlimited=False)
    for raw in _split(unlimited_env):
        h = _hash_token(raw)
        keys[h] = ApiKey(hash=h, unlimited=True)
    return keys


def hash_token(raw: str) -> str:
    """Public helper: hash a raw token the same way keys are hashed."""
    return _hash_token(raw)


class BearerAuth(BaseHTTPMiddleware):
    """Validate ``Authorization: Bearer <token>`` and enforce per-key limits."""

    def __init__(self, app, keys: Mapping[str, ApiKey] | None = None):
        super().__init__(app)
        self._keys: dict[str, ApiKey] = dict(keys) if keys is not None else load_keys()
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        if not self._keys:
            logger.warning(
                "No API keys configured (BIORXIV_MCP_API_KEYS unset); "
                "HTTP transport is OPEN. Set keys before exposing publicly."
            )
        else:
            n_unlimited = sum(1 for k in self._keys.values() if k.unlimited)
            logger.info("Loaded %d API key(s) (%d unlimited)",
                        len(self._keys), n_unlimited)

    @property
    def auth_enabled(self) -> bool:
        return bool(self._keys)

    def _bucket(self, identity: str, rate: float, burst: int) -> TokenBucket:
        with self._lock:
            b = self._buckets.get(identity)
            if b is None:
                b = TokenBucket(rate=rate, burst=burst)
                self._buckets[identity] = b
            return b

    def _rate_limit(self, identity: str, rate: float, burst: int):
        wait = self._bucket(identity, rate, burst).consume()
        if wait is None:
            return None
        # Cap Retry-After to something reasonable; clients treat overly large
        # values as "never retry" which we don't want.
        retry_after = 3600 if wait == float("inf") else max(1, int(wait) + 1)
        detail = "never" if wait == float("inf") else f"{wait:.1f} seconds"
        return JSONResponse(
            {"error": f"Rate limit exceeded. Try again in {detail}."},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _UNAUTHED_PATHS:
            return await call_next(request)

        client_ip = request.client.host if request.client else "-"

        if not self._keys:
            # Open mode: still enforce a per-IP rate limit to deter abuse.
            limited = self._rate_limit(f"ip:{client_ip}", _ANON_RATE, _ANON_BURST)
            if limited is not None:
                return limited
            request.state.key_id = "anonymous"
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            logger.warning("auth missing ip=%s path=%s", client_ip, request.url.path)
            return JSONResponse(
                {"error": "missing bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="biorxiv-mcp"'},
            )

        key = self._keys.get(_hash_token(auth[7:].strip()))
        if key is None:
            logger.warning("auth invalid ip=%s path=%s", client_ip, request.url.path)
            return JSONResponse({"error": "invalid token"}, status_code=403)

        if not key.unlimited:
            limited = self._rate_limit(f"key:{key.key_id}", _KEY_RATE, _KEY_BURST)
            if limited is not None:
                logger.info("rate_limited key=%s ip=%s", key.key_id, client_ip)
                return limited

        request.state.key_id = key.key_id
        return await call_next(request)
