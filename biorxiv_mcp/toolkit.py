"""Shared helpers for MCP tool handlers.

Centralizes rate limiting, error handling, and the list/dict error envelope
shape so each tool body can focus on business logic.
"""

import functools
import inspect
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from .ratelimit import TokenBucket

logger = logging.getLogger(__name__)

Shape = Literal["dict", "list"]


def envelope(shape: Shape, error: str) -> dict | list[dict]:
    """Wrap an error message in the tool's declared return shape."""
    payload = {"error": error}
    return [payload] if shape == "list" else payload


def validate_date(s: str | None, field: str) -> str | None:
    """Ensure ``s`` is ``YYYY-MM-DD`` or None. Raises ValueError otherwise."""
    if s is None:
        return None
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"{field} must be YYYY-MM-DD, got {s!r}") from e
    return s


def tool(
    *,
    shape: Shape = "dict",
    bucket: TokenBucket | None = None,
) -> Callable:
    """Decorate an MCP tool handler with rate limiting + error handling.

    - Consumes a token from ``bucket`` if given; returns a rate-limit error
      envelope when empty.
    - Converts ``ValueError`` to a user-facing error (for input validation).
    - Converts ``sqlite3.Error`` and unexpected exceptions to generic errors,
      logging internally.

    Works with both sync and async handlers.
    """

    def decorator(fn: Callable) -> Callable:
        name = fn.__name__

        def _handle_rate_limit() -> dict | list[dict] | None:
            if bucket is None:
                return None
            wait = bucket.consume()
            if wait is None:
                return None
            return envelope(shape, f"Rate limit exceeded. Try again in {wait:.1f} seconds.")

        def _to_envelope(exc: BaseException) -> dict | list[dict]:
            if isinstance(exc, ValueError):
                return envelope(shape, str(exc))
            if isinstance(exc, sqlite3.Error):
                logger.error("Database error in %s: %s", name, exc)
                return envelope(shape, f"Database error: {exc}")
            logger.exception("Unexpected error in %s", name)
            return envelope(shape, f"Internal error: {exc}")

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_inner(*args, **kwargs):
                limited = _handle_rate_limit()
                if limited is not None:
                    return limited
                try:
                    return await fn(*args, **kwargs)
                except Exception as e:
                    return _to_envelope(e)
            return async_inner

        @functools.wraps(fn)
        def sync_inner(*args, **kwargs):
            limited = _handle_rate_limit()
            if limited is not None:
                return limited
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                return _to_envelope(e)

        return sync_inner

    return decorator
