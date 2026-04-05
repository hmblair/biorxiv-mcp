"""SQLite FTS5 index for bioRxiv papers."""

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

DB_DIR = Path(os.environ.get("BIORXIV_MCP_DATA", Path.home() / ".local/share/biorxiv-mcp"))
DB_PATH = DB_DIR / "biorxiv.db"
PAPERS_DIR = DB_DIR / "papers"

# Authoritative list of paper fields, in schema order.
# sync.py and upsert_papers derive column names from this.
PAPER_FIELDS = (
    "doi", "title", "authors", "abstract", "date", "category", "version",
    "type", "license", "published", "author_corresponding",
    "author_corresponding_institution", "jatsxml", "server",
)

# Fields included in FTS index (must be a subset of PAPER_FIELDS).
FTS_FIELDS = ("title", "abstract", "authors", "author_corresponding_institution")

_INSERT_COLS = ", ".join(PAPER_FIELDS)
_INSERT_PARAMS = ", ".join(f":{f}" for f in PAPER_FIELDS)
_FTS_COLS = ", ".join(FTS_FIELDS)
_FTS_NEW = ", ".join(f"new.{f}" for f in FTS_FIELDS)
_FTS_OLD = ", ".join(f"old.{f}" for f in FTS_FIELDS)

_writer_lock = threading.Lock()
_thread_local = threading.local()
# sqlite3.Connection disallows arbitrary attrs, so track init state by id.
# Entries leak if connections are GC'd without close(), but for our long-lived
# per-thread connections this is fine.
_initialized_ids: set[int] = set()


def get_connection() -> sqlite3.Connection:
    """Create a new SQLite connection with WAL + pragmas and init schema."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    except sqlite3.OperationalError as e:
        logger.error("Failed to open database at %s: %s", DB_PATH, e)
        raise
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    init_db(conn)
    logger.debug("Opened database connection to %s", DB_PATH)
    return conn


@contextmanager
def connection():
    """Yield a per-thread DB connection.

    WAL mode allows multiple concurrent readers alongside a single writer,
    so we keep one connection per thread rather than serializing all access
    through a global lock. Writers should additionally acquire
    ``writer_lock()`` to serialize write transactions.
    """
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = get_connection()
        _thread_local.conn = conn
    yield conn


@contextmanager
def writer_lock():
    """Serialize write transactions across threads."""
    with _writer_lock:
        yield


def init_db(conn: sqlite3.Connection) -> None:
    # Avoid re-running DDL on the same connection.
    if id(conn) in _initialized_ids:
        return
    cols = ",\n            ".join(
        f"{f} TEXT PRIMARY KEY" if f == "doi"
        else f"{f} TEXT NOT NULL" if f == "title"
        else f"{f} TEXT"
        for f in PAPER_FIELDS
    )
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS papers (
            {cols}
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
            {_FTS_COLS},
            content='papers',
            content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS papers_ai AFTER INSERT ON papers BEGIN
            INSERT INTO papers_fts(rowid, {_FTS_COLS})
            VALUES (new.rowid, {_FTS_NEW});
        END;

        CREATE TRIGGER IF NOT EXISTS papers_ad AFTER DELETE ON papers BEGIN
            INSERT INTO papers_fts(papers_fts, rowid, {_FTS_COLS})
            VALUES ('delete', old.rowid, {_FTS_OLD});
        END;

        CREATE TRIGGER IF NOT EXISTS papers_au AFTER UPDATE ON papers BEGIN
            INSERT INTO papers_fts(papers_fts, rowid, {_FTS_COLS})
            VALUES ('delete', old.rowid, {_FTS_OLD});
            INSERT INTO papers_fts(rowid, {_FTS_COLS})
            VALUES (new.rowid, {_FTS_NEW});
        END;

        CREATE TABLE IF NOT EXISTS sync_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            hash TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            unlimited INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            disabled INTEGER NOT NULL DEFAULT 0
        );
    """)
    _initialized_ids.add(id(conn))


def upsert_papers(conn: sqlite3.Connection, papers: list[dict]) -> int:
    """Insert or replace papers. Returns number of papers upserted."""
    if not papers:
        return 0
    # Deduplicate by DOI within the batch, keeping the highest version.
    def _version(p: dict) -> int:
        v = p.get("version") or "0"
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    by_doi: dict[str, dict] = {}
    for p in papers:
        existing = by_doi.get(p["doi"])
        if existing is None or _version(p) > _version(existing):
            by_doi[p["doi"]] = p
    papers = list(by_doi.values())
    dois = [(p["doi"],) for p in papers]
    with _writer_lock:
        conn.executemany("DELETE FROM papers WHERE doi = ?", dois)
        conn.executemany(
            f"INSERT INTO papers ({_INSERT_COLS}) VALUES ({_INSERT_PARAMS})",
            papers,
        )
        conn.commit()
    return len(papers)


# -- Search ------------------------------------------------------------------

_FTS5_OPERATORS = {"AND", "OR", "NOT", "NEAR"}


def _sanitize_token(token: str) -> str:
    """Strip FTS5 special characters from a raw token.

    Keeps alphanumerics and underscores; replaces other punctuation with
    spaces so a token like ``mRNA-seq`` becomes ``mRNA seq``.
    """
    return "".join(c if c.isalnum() or c == "_" else " " for c in token)


def _add_prefix_matching(query: str) -> str:
    """Append * to tokens >= 3 chars that aren't FTS5 operators or already prefixed.

    Also strips FTS5 special characters from bare tokens so agent-typed input
    with punctuation (hyphens, parentheses, colons) doesn't raise a syntax
    error. Quoted phrases are passed through unchanged.
    """
    if '"' in query:
        return query
    # Re-tokenize after sanitization so ``mRNA-seq`` becomes two tokens.
    sanitized = " ".join(_sanitize_token(t) for t in query.split())
    tokens = sanitized.split()
    result = []
    for token in tokens:
        if not token:
            continue
        if token.upper() in _FTS5_OPERATORS:
            result.append(token)
        elif len(token) >= 3:
            result.append(token + "*")
        else:
            result.append(token)
    return " ".join(result)


def _search_where(query: str, category: str | None, after: str | None, before: str | None):
    """Build the WHERE clause and params for search queries."""
    fts_query = _add_prefix_matching(query)
    if not fts_query.strip():
        # FTS5 rejects empty MATCH strings; use a token that never matches.
        fts_query = "__no_match__"
    where = "papers_fts MATCH ?"
    params: list = [fts_query]
    if category:
        where += " AND p.category = ?"
        params.append(category)
    if after:
        where += " AND p.date >= ?"
        params.append(after)
    if before:
        where += " AND p.date <= ?"
        params.append(before)
    return where, params


def search_count(
    conn: sqlite3.Connection,
    query: str,
    category: str | None = None,
    after: str | None = None,
    before: str | None = None,
) -> int:
    """Return the number of papers matching a query."""
    where, params = _search_where(query, category, after, before)
    sql = f"""
        SELECT COUNT(*)
        FROM papers_fts f
        JOIN papers p ON p.rowid = f.rowid
        WHERE {where}
    """
    return conn.execute(sql, params).fetchone()[0]


_COMPACT_COLS = "p.doi, p.title, p.authors, p.date, p.category, p.server"


def search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    category: str | None = None,
    after: str | None = None,
    before: str | None = None,
    detail: bool = False,
    sort: str = "relevance",
) -> list[dict]:
    """FTS5 search with optional filters."""
    where, params = _search_where(query, category, after, before)
    columns = "p.*" if detail else _COMPACT_COLS
    order = "rank" if sort == "relevance" else "p.date DESC"
    sql = f"""
        SELECT {columns}
        FROM papers_fts f
        JOIN papers p ON p.rowid = f.rowid
        WHERE {where}
        ORDER BY {order} LIMIT ?
    """
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# -- Single-paper lookup -----------------------------------------------------

def get_paper(conn: sqlite3.Connection, doi: str) -> dict | None:
    """Get a paper by DOI."""
    row = conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
    return dict(row) if row else None


# -- Metadata ----------------------------------------------------------------

def get_categories(conn: sqlite3.Connection) -> list[dict]:
    """Return all categories with paper counts, sorted by count descending."""
    rows = conn.execute(
        "SELECT category, COUNT(*) as count FROM papers GROUP BY category ORDER BY count DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_paper_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]


def get_db_size_mb() -> float:
    if DB_PATH.exists():
        return DB_PATH.stat().st_size / (1024 * 1024)
    return 0.0


# -- Sync state ---------------------------------------------------------------

_LAST_SYNC_DATE = "last_sync_date"
_BULK_SYNC_CURSOR = "bulk_sync_cursor"


def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with _writer_lock:
        conn.execute(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES (?, ?)", (key, value)
        )
        conn.commit()


def _del_meta(conn: sqlite3.Connection, key: str) -> None:
    with _writer_lock:
        conn.execute("DELETE FROM sync_meta WHERE key = ?", (key,))
        conn.commit()


def get_last_sync_date(conn: sqlite3.Connection) -> str | None:
    return _get_meta(conn, _LAST_SYNC_DATE)


def set_last_sync_date(conn: sqlite3.Connection, date: str) -> None:
    _set_meta(conn, _LAST_SYNC_DATE, date)


def get_bulk_sync_cursor(conn: sqlite3.Connection) -> str | None:
    return _get_meta(conn, _BULK_SYNC_CURSOR)


def set_bulk_sync_cursor(conn: sqlite3.Connection, date: str) -> None:
    _set_meta(conn, _BULK_SYNC_CURSOR, date)


def clear_bulk_sync_cursor(conn: sqlite3.Connection) -> None:
    _del_meta(conn, _BULK_SYNC_CURSOR)
