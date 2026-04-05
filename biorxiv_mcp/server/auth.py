"""Bearer-token authentication and per-key rate limiting.

Keys are stored in the ``api_keys`` SQLite table and looked up on each
request. No restart is needed to add or revoke keys.
"""

from __future__ import annotations

import logging
import os
import threading

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import db
from .keys import ApiKey, any_keys_exist, hash_token, load_active
from .ratelimit import TokenBucket

logger = logging.getLogger(__name__)

_UNAUTHED_PATHS = frozenset({"/health"})

_KEY_RATE = float(os.environ.get("BIORXIV_MCP_KEY_RATE", "1.0"))
_KEY_BURST = int(os.environ.get("BIORXIV_MCP_KEY_BURST", "60"))
_ANON_RATE = float(os.environ.get("BIORXIV_MCP_ANON_RATE", "0.5"))
_ANON_BURST = int(os.environ.get("BIORXIV_MCP_ANON_BURST", "30"))


class BearerAuth(BaseHTTPMiddleware):
    """Validate ``Authorization: Bearer <token>`` against the api_keys table."""

    def __init__(self, app):
        super().__init__(app)
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def _load_keys(self) -> tuple[dict[str, ApiKey], bool]:
        """Returns (active_keys, auth_required)."""
        with db.connection() as conn:
            return load_active(conn), any_keys_exist(conn)

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
        active_keys, auth_required = self._load_keys()

        if not auth_required:
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

        key = active_keys.get(hash_token(auth[7:].strip()))
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
