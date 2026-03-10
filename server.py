"""bioRxiv MCP server -- search and sync bioRxiv/medRxiv papers."""

import asyncio

from mcp.server.fastmcp import FastMCP

from biorxiv_mcp import db, sync

mcp = FastMCP("biorxiv")


@mcp.tool()
def search_biorxiv(
    query: str,
    limit: int = 10,
    category: str | None = None,
    after: str | None = None,
    before: str | None = None,
    detail: bool = False,
) -> list[dict]:
    """Search bioRxiv/medRxiv papers by keyword.

    Uses full-text search on titles, abstracts, and authors.
    Returns compact results (doi, title, authors, date, category) by default.
    Set detail=True to include abstract, institution, license, and other fields.

    Args:
        query: Search query (supports prefix matching and FTS5 syntax, e.g. "CRISPR AND cancer")
        limit: Max results to return (default 10)
        category: Filter by category (e.g. "neuroscience", "genomics"). Use biorxiv_categories() to list available categories.
        after: Only papers on or after this date (YYYY-MM-DD)
        before: Only papers on or before this date (YYYY-MM-DD)
        detail: If True, return all fields including abstract (default False)
    """
    conn = db.get_connection()
    try:
        db.init_db(conn)
        results = db.search(conn, query, limit=limit, category=category, after=after, before=before, detail=detail)
        if not results:
            count = db.get_paper_count(conn)
            if count == 0:
                return [{"message": "Database is empty. Run sync_biorxiv() first to populate it."}]
            return [{"message": f"No results for '{query}' (searched {count} papers)."}]
        return results
    finally:
        conn.close()


@mcp.tool()
def biorxiv_categories() -> list[dict]:
    """List all bioRxiv/medRxiv categories with paper counts."""
    conn = db.get_connection()
    try:
        db.init_db(conn)
        return db.get_categories(conn)
    finally:
        conn.close()


@mcp.tool()
async def sync_biorxiv() -> dict:
    """Sync papers from bioRxiv/medRxiv API.

    Runs delta sync if the database has been synced before, otherwise bulk sync.
    Bulk sync fetches all papers from 2013 to today and takes several hours.
    """
    conn = db.get_connection()
    try:
        db.init_db(conn)
        last = db.get_last_sync_date(conn)
        if last:
            count = await sync.delta_sync(conn)
            return {
                "status": "delta_sync_complete",
                "new_papers": count,
                "total_papers": db.get_paper_count(conn),
                "last_sync": db.get_last_sync_date(conn),
            }
        else:
            count = await sync.bulk_sync(conn)
            return {
                "status": "bulk_sync_complete",
                "total_papers": count,
                "last_sync": db.get_last_sync_date(conn),
            }
    finally:
        conn.close()


@mcp.tool()
def biorxiv_status() -> dict:
    """Get the status of the local bioRxiv database."""
    conn = db.get_connection()
    try:
        db.init_db(conn)
        return {
            "paper_count": db.get_paper_count(conn),
            "last_sync": db.get_last_sync_date(conn),
            "db_size_mb": round(db.get_db_size_mb(), 2),
            "db_path": str(db.DB_PATH),
        }
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
