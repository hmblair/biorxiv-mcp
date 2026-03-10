"""SQLite FTS5 index for bioRxiv papers."""

import os
import sqlite3
from pathlib import Path

DB_DIR = Path(os.environ.get("BIORXIV_MCP_DATA", Path.home() / ".local/share/biorxiv-mcp"))
DB_PATH = DB_DIR / "biorxiv.db"


def get_connection() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS papers (
            doi TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            authors TEXT,
            abstract TEXT,
            date TEXT,
            category TEXT,
            version TEXT,
            type TEXT,
            license TEXT,
            published TEXT,
            author_corresponding TEXT,
            author_corresponding_institution TEXT,
            jatsxml TEXT,
            server TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
            title, abstract, authors, author_corresponding_institution,
            content='papers',
            content_rowid='rowid'
        );

        -- Triggers to keep FTS in sync
        CREATE TRIGGER IF NOT EXISTS papers_ai AFTER INSERT ON papers BEGIN
            INSERT INTO papers_fts(rowid, title, abstract, authors, author_corresponding_institution)
            VALUES (new.rowid, new.title, new.abstract, new.authors, new.author_corresponding_institution);
        END;

        CREATE TRIGGER IF NOT EXISTS papers_ad AFTER DELETE ON papers BEGIN
            INSERT INTO papers_fts(papers_fts, rowid, title, abstract, authors, author_corresponding_institution)
            VALUES ('delete', old.rowid, old.title, old.abstract, old.authors, old.author_corresponding_institution);
        END;

        CREATE TRIGGER IF NOT EXISTS papers_au AFTER UPDATE ON papers BEGIN
            INSERT INTO papers_fts(papers_fts, rowid, title, abstract, authors, author_corresponding_institution)
            VALUES ('delete', old.rowid, old.title, old.abstract, old.authors, old.author_corresponding_institution);
            INSERT INTO papers_fts(rowid, title, abstract, authors, author_corresponding_institution)
            VALUES (new.rowid, new.title, new.abstract, new.authors, new.author_corresponding_institution);
        END;

        CREATE TABLE IF NOT EXISTS sync_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)


def upsert_papers(conn: sqlite3.Connection, papers: list[dict]) -> int:
    """Insert or replace papers. Returns number of papers upserted."""
    if not papers:
        return 0
    # Deduplicate by DOI within the batch, keeping the highest version
    by_doi: dict[str, dict] = {}
    for p in papers:
        existing = by_doi.get(p["doi"])
        if existing is None:
            by_doi[p["doi"]] = p
        else:
            try:
                new_ver = int(p.get("version", "0"))
                old_ver = int(existing.get("version", "0"))
                if new_ver > old_ver:
                    by_doi[p["doi"]] = p
            except ValueError:
                by_doi[p["doi"]] = p
    papers = list(by_doi.values())
    dois = [(p["doi"],) for p in papers]
    conn.executemany("DELETE FROM papers WHERE doi = ?", dois)
    conn.executemany(
        """INSERT INTO papers (doi, title, authors, abstract, date, category, version,
                               type, license, published, author_corresponding,
                               author_corresponding_institution, jatsxml, server)
           VALUES (:doi, :title, :authors, :abstract, :date, :category, :version,
                   :type, :license, :published, :author_corresponding,
                   :author_corresponding_institution, :jatsxml, :server)""",
        papers,
    )
    conn.commit()
    return len(papers)


_FTS5_OPERATORS = {"AND", "OR", "NOT", "NEAR"}


def _add_prefix_matching(query: str) -> str:
    """Append * to tokens that are 3+ chars and aren't FTS5 operators or already prefixed."""
    # Don't modify queries containing quoted phrases
    if '"' in query:
        return query
    tokens = query.split()
    result = []
    for token in tokens:
        if (
            token.upper() in _FTS5_OPERATORS
            or token.endswith("*")
            or ":" in token
        ):
            result.append(token)
        elif len(token) >= 3:
            result.append(token + "*")
        else:
            result.append(token)
    return " ".join(result)


def _search_where(query: str, category: str | None, after: str | None, before: str | None):
    """Build the WHERE clause and params for search queries."""
    fts_query = _add_prefix_matching(query)
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


def search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    category: str | None = None,
    after: str | None = None,
    before: str | None = None,
    detail: bool = False,
) -> list[dict]:
    """FTS5 search with optional filters."""
    where, params = _search_where(query, category, after, before)
    columns = "p.doi, p.title, p.authors, p.date, p.category, p.server"
    if detail:
        columns = "p.*"
    sql = f"""
        SELECT {columns}
        FROM papers_fts f
        JOIN papers p ON p.rowid = f.rowid
        WHERE {where}
        ORDER BY rank LIMIT ?
    """
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_last_sync_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM sync_meta WHERE key = 'last_sync_date'").fetchone()
    return row["value"] if row else None


def set_last_sync_date(conn: sqlite3.Connection, date: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('last_sync_date', ?)", (date,)
    )
    conn.commit()


def get_bulk_sync_cursor(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM sync_meta WHERE key = 'bulk_sync_cursor'").fetchone()
    return row["value"] if row else None


def set_bulk_sync_cursor(conn: sqlite3.Connection, date: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('bulk_sync_cursor', ?)", (date,)
    )
    conn.commit()


def clear_bulk_sync_cursor(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM sync_meta WHERE key = 'bulk_sync_cursor'")
    conn.commit()


def get_paper_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]


def get_paper(conn: sqlite3.Connection, doi: str) -> dict | None:
    """Get a paper by DOI."""
    row = conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
    return dict(row) if row else None


def get_categories(conn: sqlite3.Connection) -> list[dict]:
    """Return all categories with paper counts, sorted by count descending."""
    rows = conn.execute(
        "SELECT category, COUNT(*) as count FROM papers GROUP BY category ORDER BY count DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_db_size_mb() -> float:
    if DB_PATH.exists():
        return DB_PATH.stat().st_size / (1024 * 1024)
    return 0.0
