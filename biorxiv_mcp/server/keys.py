"""API key management backed by the api_keys SQLite table."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


def hash_token(raw: str) -> str:
    """SHA-256 hex digest of a raw bearer token."""
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ApiKey:
    """An API key as stored in the database."""
    hash: str
    label: str
    unlimited: bool
    created_at: str
    disabled: bool

    @property
    def key_id(self) -> str:
        """Short, safe-to-log identifier (first 8 chars of the hash)."""
        return self.hash[:8]


def _insert(conn: sqlite3.Connection, h: str, label: str, unlimited: bool) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO api_keys (hash, label, unlimited, created_at, disabled) VALUES (?, ?, ?, ?, 0)",
        (h, label, int(unlimited), now),
    )
    conn.commit()


def generate(conn: sqlite3.Connection, label: str, unlimited: bool = False) -> str:
    """Create a new API key. Returns the raw token (shown once, never stored)."""
    raw = secrets.token_urlsafe(32)
    _insert(conn, hash_token(raw), label, unlimited)
    return raw


def import_token(conn: sqlite3.Connection, raw: str, label: str, unlimited: bool = False) -> str:
    """Import an existing raw token into the database. Returns the key ID."""
    h = hash_token(raw)
    if conn.execute("SELECT 1 FROM api_keys WHERE hash = ?", (h,)).fetchone():
        raise ValueError(f"Key already exists (key ID {h[:8]})")
    _insert(conn, h, label, unlimited)
    return h[:8]


def list_keys(conn: sqlite3.Connection, include_disabled: bool = False) -> list[ApiKey]:
    """Return all keys, optionally including disabled ones."""
    sql = "SELECT hash, label, unlimited, created_at, disabled FROM api_keys"
    if not include_disabled:
        sql += " WHERE disabled = 0"
    sql += " ORDER BY created_at"
    rows = conn.execute(sql).fetchall()
    return [ApiKey(hash=r[0], label=r[1], unlimited=bool(r[2]),
                   created_at=r[3], disabled=bool(r[4])) for r in rows]


def revoke(conn: sqlite3.Connection, key_id: str) -> ApiKey | None:
    """Disable a key by its key_id prefix. Returns the key if found."""
    row = conn.execute(
        "SELECT hash, label, unlimited, created_at, disabled FROM api_keys WHERE hash LIKE ?",
        (key_id + "%",),
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE api_keys SET disabled = 1 WHERE hash = ?", (row[0],))
    conn.commit()
    return ApiKey(hash=row[0], label=row[1], unlimited=bool(row[2]),
                  created_at=row[3], disabled=True)


def load_active(conn: sqlite3.Connection) -> dict[str, ApiKey]:
    """Load all active keys as a dict keyed by hash (for auth middleware)."""
    return {k.hash: k for k in list_keys(conn, include_disabled=False)}


def any_keys_exist(conn: sqlite3.Connection) -> bool:
    """True if any keys have ever been created (even if all are now disabled)."""
    row = conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()
    return row[0] > 0
