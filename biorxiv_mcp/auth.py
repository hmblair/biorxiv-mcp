"""Bearer-token authentication and per-key rate limiting.

API keys are supplied via ``BIORXIV_MCP_API_KEYS`` (comma-separated). They
are hashed at startup and compared in constant time on each request.

If no keys are configured, the middleware allows all requests through but
logs a warning — this preserves the local/stdio workflow where auth is
unnecessary.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
from collections.abc import Iterable

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


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def load_keys(env_value: str | None = None) -> set[str]:
    """Load and hash API keys from the environment.

    Returns a set of SHA-256 hex digests. An empty set means auth is open.
    """
    if env_value is None:
        env_value = os.environ.get("BIORXIV_MCP_API_KEYS", "")
    keys = {_hash_key(k.strip()) for k in env_value.split(",") if k.strip()}
    return keys


def _key_id(digest: str) -> str:
    """Short, safe-to-log identifier for a key (first 8 chars of its hash)."""
    return digest[:8]


class BearerAuth(BaseHTTPMiddleware):
    """Validate ``Authorization: Bearer <token>`` and enforce per-key limits."""

    def __init__(self, app, keys: Iterable[str] | None = None):
        super().__init__(app)
        self._keys: set[str] = set(keys) if keys is not None else load_keys()
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        if not self._keys:
            logger.warning(
                "No API keys configured (BIORXIV_MCP_API_KEYS unset); "
                "HTTP transport is OPEN. Set keys before exposing publicly."
            )
        else:
            logger.info("Loaded %d API key(s)", len(self._keys))

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

        presented = _hash_key(auth[7:].strip())
        if not any(hmac.compare_digest(presented, k) for k in self._keys):
            logger.warning("auth invalid ip=%s path=%s", client_ip, request.url.path)
            return JSONResponse({"error": "invalid token"}, status_code=403)

        key_id = _key_id(presented)
        limited = self._rate_limit(f"key:{key_id}", _KEY_RATE, _KEY_BURST)
        if limited is not None:
            logger.info("rate_limited key=%s ip=%s", key_id, client_ip)
            return limited

        request.state.key_id = key_id
        return await call_next(request)
