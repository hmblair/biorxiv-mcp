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
    "doi",
    "title",
    "authors",
    "abstract",
    "date",
    "category",
    "version",
    "type",
    "license",
    "published",
    "author_corresponding",
    "author_corresponding_institution",
    "jatsxml",
    "server",
)

# Fields included in FTS index (must be a subset of PAPER_FIELDS).
FTS_FIELDS = ("title", "abstract", "authors", "author_corresponding_institution")

_INSERT_COLS = ", ".join(PAPER_FIELDS)
_INSERT_PARAMS = ", ".join(f":{f}" for f in PAPER_FIELDS)
_FTS_COLS = ", ".join(FTS_FIELDS)
_FTS_NEW = ", ".join(f"new.{f}" for f in FTS_FIELDS)
_FTS_OLD = ", ".join(f"old.{f}" for f in FTS_FIELDS)

def _normalize_category(cat: str | None) -> str:
    """Normalize category to lowercase with stripped whitespace."""
    return (cat or "").strip().lower()


def _paper_dict(row: sqlite3.Row) -> dict:
    """Convert a Row to a dict with normalized category."""
    d = dict(row)
    if "category" in d:
        d["category"] = _normalize_category(d["category"])
    return d


def _compact_authors(authors: str | None) -> str:
    """Truncate to first and last author."""
    if not authors:
        return ""
    parts = [a.strip() for a in authors.split(";") if a.strip()]
    if len(parts) <= 2:
        return "; ".join(parts)
    return f"{parts[0]}; ... ; {parts[-1]}"


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
        # Restrict to owner-only: the DB contains API key hashes.
        DB_DIR.chmod(0o700)
    except OSError:
        pass
    try:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    except sqlite3.OperationalError as e:
        logger.error("Failed to open database at %s: %s", DB_PATH, e)
        raise
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    init_db(conn)
    try:
        if DB_PATH.exists():
            DB_PATH.chmod(0o600)
    except OSError:
        pass
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
        f"{f} TEXT PRIMARY KEY"
        if f == "doi"
        else f"{f} TEXT NOT NULL"
        if f == "title"
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
            created_at TEXT NOT NULL
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
    for p in papers:
        p["category"] = _normalize_category(p.get("category"))
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


# -- Query preparation pipeline -----------------------------------------------
#
# Pipeline: raw query → sanitize → group MeSH phrases → expand → build clauses
#
# The result is a list of FTS5 MATCH expressions joined by SQL AND.
# Each clause is one search term expanded with MeSH synonyms:
#
#   WHERE papers_fts MATCH 'cancer OR tumor OR neoplasm'
#     AND papers_fts MATCH 'CRISPR'


def _sanitize_token(raw: str) -> str:
    """Clean a single whitespace-delimited token for FTS5.

    Hyphenated compounds (``mRNA-seq``) become quoted phrases so they
    match the adjacent tokens FTS5 indexed. Other punctuation is stripped.
    Explicit wildcards (``CRISPR*``) are preserved.
    """
    cleaned = "".join(c if c.isalnum() or c in ("_", "*") else " " for c in raw)
    parts = cleaned.split()
    if "-" in raw and len(parts) > 1:
        return '"' + " ".join(parts) + '"'
    return cleaned


def _sanitize(query: str) -> list[str]:
    """Sanitize a raw query into a flat list of clean words and phrases.

    User-quoted phrases are returned as a single element. Hyphenated
    compounds become quoted phrase tokens via ``_sanitize_token``.
    Everything else is split on whitespace after stripping punctuation.
    """
    if '"' in query:
        return [query]
    words = []
    for raw in query.split():
        sanitized = _sanitize_token(raw)
        if sanitized.startswith('"'):
            # Phrase from hyphen sanitization — keep as one token.
            words.append(sanitized)
        else:
            for w in sanitized.split():
                if w:
                    words.append(w)
    return words


def _expand_term(term: str, mesh_expand) -> str:
    """Expand a single term (word or phrase) with MeSH synonyms.

    Returns an FTS5 OR expression: ``cancer OR tumor OR neoplasm``.
    Multi-word terms and synonyms are quoted for phrase matching.
    """
    parts = [f'"{term}"' if " " in term else term]
    for syn in mesh_expand(term, max_synonyms=3):
        parts.append(f'"{syn}"' if " " in syn else syn)
    return " OR ".join(parts)


def _build_match_clauses(query: str) -> list[str]:
    """Convert a user query into a list of FTS5 MATCH expressions.

    Each clause is one search term expanded with MeSH synonyms.
    Clauses are AND-joined in the SQL WHERE, giving PubMed-like
    implicit AND with per-term synonym expansion.

    Multi-word MeSH terms (e.g. "ribonucleic acid") are detected
    by a greedy longest-match scan and grouped as a single clause.

    Queries with explicit FTS5 operators (AND, OR, NOT, NEAR) are
    passed through as a single clause with no expansion.
    """
    from .mesh import expand as mesh_expand
    from .mesh import find_phrases

    words = _sanitize(query)
    if not words:
        return ["__no_match__"]
    if len(words) == 1 and words[0].startswith('"'):
        return [words[0]]
    if any(w.upper() in _FTS5_OPERATORS for w in words if not w.startswith('"')):
        return [" ".join(words)]

    # Separate pre-existing phrases (from hyphen sanitization) from plain
    # words. Only plain words go through MeSH phrase detection.
    plain_words = [w for w in words if not w.startswith('"')]
    phrase_tokens = [w for w in words if w.startswith('"')]
    grouped = find_phrases(plain_words)
    clauses = []
    for token in phrase_tokens:
        # Already a quoted phrase — use as a MATCH clause directly.
        clauses.append(token)
    for item in grouped:
        if isinstance(item, list):
            clauses.append(_expand_term(" ".join(item), mesh_expand))
        else:
            clauses.append(_expand_term(item, mesh_expand))
    return clauses


def _search_where(
    query: str,
    category: str | list[str] | None,
    after: str | None,
    before: str | None,
):
    """Build the WHERE clause and params for search queries.

    Each search term becomes a separate ``papers_fts MATCH ?`` clause
    joined by AND. Within each clause, the term and its MeSH synonyms
    are OR-joined.

    When *query* is empty, the MATCH clause is omitted and results are
    filtered only by category/date.
    """
    parts: list[str] = []
    params: list = []
    if query.strip():
        clauses = _build_match_clauses(query)
        for clause in clauses:
            parts.append("papers_fts MATCH ?")
            params.append(clause)
    if category:
        if isinstance(category, list):
            placeholders = ", ".join("?" for _ in category)
            parts.append(f"LOWER(TRIM(p.category)) IN ({placeholders})")
            params.extend(_normalize_category(c) for c in category)
        else:
            parts.append("LOWER(TRIM(p.category)) = ?")
            params.append(_normalize_category(category))
    if after:
        parts.append("p.date >= ?")
        params.append(after)
    if before:
        parts.append("p.date <= ?")
        params.append(before)
    where = " AND ".join(parts) if parts else "1"
    return where, params


def search_count(
    conn: sqlite3.Connection,
    query: str,
    category: str | list[str] | None = None,
    after: str | None = None,
    before: str | None = None,
) -> int:
    """Return the number of papers matching a query."""
    has_query = bool(query.strip())
    where, params = _search_where(query, category, after, before)
    if has_query:
        sql = f"""
            SELECT COUNT(*)
            FROM papers_fts
            JOIN papers p ON p.rowid = papers_fts.rowid
            WHERE {where}
        """
    else:
        sql = f"""
            SELECT COUNT(*)
            FROM papers p
            WHERE {where}
        """
    return conn.execute(sql, params).fetchone()[0]


_COMPACT_COLS = "p.doi, p.title, p.authors, p.date, p.category, p.server"


def search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    category: str | list[str] | None = None,
    after: str | None = None,
    before: str | None = None,
    detail: bool = False,
    sort: str = "relevance",
) -> list[dict]:
    """FTS5 search with optional filters.

    When *query* is empty, returns papers filtered only by category/date
    (no full-text matching). Sort defaults to date in that case since
    relevance ranking requires an FTS MATCH.
    """
    has_query = bool(query.strip())
    where, params = _search_where(query, category, after, before)
    columns = "p.*" if detail else _COMPACT_COLS
    order = "rank" if sort == "relevance" and has_query else "p.date DESC"
    if has_query:
        sql = f"""
            SELECT {columns}
            FROM papers_fts
            JOIN papers p ON p.rowid = papers_fts.rowid
            WHERE {where}
            ORDER BY {order} LIMIT ?
        """
    else:
        sql = f"""
            SELECT {columns}
            FROM papers p
            WHERE {where}
            ORDER BY {order} LIMIT ?
        """
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    results = [_paper_dict(r) for r in rows]
    if not detail:
        for r in results:
            if "authors" in r:
                r["authors"] = _compact_authors(r["authors"])
    return results


# -- Single-paper lookup -----------------------------------------------------


def get_paper(conn: sqlite3.Connection, doi: str) -> dict | None:
    """Get a paper by DOI."""
    row = conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
    return _paper_dict(row) if row else None


# -- Metadata ----------------------------------------------------------------


def get_categories(conn: sqlite3.Connection) -> list[dict]:
    """Return all categories with paper counts, sorted by count descending."""
    rows = conn.execute(
        "SELECT LOWER(TRIM(category)) as category, COUNT(*) as count"
        " FROM papers GROUP BY LOWER(TRIM(category)) ORDER BY count DESC"
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
        conn.execute("INSERT OR REPLACE INTO sync_meta (key, value) VALUES (?, ?)", (key, value))
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
