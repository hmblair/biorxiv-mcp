"""bioRxiv MCP server -- search and sync bioRxiv/medRxiv papers."""

import asyncio
import logging
import os
import sqlite3

import httpx
from mcp.server.fastmcp import FastMCP

from biorxiv_mcp import db, sync
from biorxiv_mcp.ratelimit import TokenBucket

# -- Logging ------------------------------------------------------------------

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    level=getattr(logging, log_level, logging.INFO),
)
logger = logging.getLogger(__name__)

# -- Rate limiting ------------------------------------------------------------

_search_bucket = TokenBucket(rate=10, burst=20)
_sync_bucket = TokenBucket(rate=1 / 60, burst=1)

# -- Server -------------------------------------------------------------------

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
TRANSPORT = os.environ.get("TRANSPORT", "sse")

mcp = FastMCP("biorxiv", host=HOST, port=PORT)


@mcp.tool()
def search_biorxiv(
    query: str,
    limit: int = 10,
    category: str | None = None,
    after: str | None = None,
    before: str | None = None,
    detail: bool = False,
    sort: str = "relevance",
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
        sort: "relevance" (default) or "date" (newest first)
    """
    wait = _search_bucket.consume()
    if wait is not None:
        return [{"error": f"Rate limit exceeded. Try again in {wait:.1f} seconds."}]

    logger.info("search_biorxiv query=%r limit=%d category=%s sort=%s", query, limit, category, sort)
    try:
        with db.connection() as conn:
            results = db.search(conn, query, limit=limit, category=category, after=after, before=before, detail=detail, sort=sort)
            if not results:
                count = db.get_paper_count(conn)
                if count == 0:
                    return [{"message": "Database is empty. Run sync_biorxiv() first to populate it."}]
                return [{"message": f"No results for '{query}' (searched {count} papers)."}]
            return results
    except sqlite3.Error as e:
        logger.error("Database error in search_biorxiv: %s", e)
        return [{"error": f"Database error: {e}"}]
    except Exception as e:
        logger.exception("Unexpected error in search_biorxiv")
        return [{"error": f"Internal error: {e}"}]


@mcp.tool()
def search_biorxiv_count(
    query: str,
    category: str | None = None,
    after: str | None = None,
    before: str | None = None,
) -> dict:
    """Count how many papers match a query without returning them.

    Useful for gauging result size before searching, or for narrowing filters.

    Args:
        query: Search query (same syntax as search_biorxiv)
        category: Filter by category
        after: Only papers on or after this date (YYYY-MM-DD)
        before: Only papers on or before this date (YYYY-MM-DD)
    """
    wait = _search_bucket.consume()
    if wait is not None:
        return {"error": f"Rate limit exceeded. Try again in {wait:.1f} seconds."}

    logger.info("search_biorxiv_count query=%r", query)
    try:
        with db.connection() as conn:
            count = db.search_count(conn, query, category=category, after=after, before=before)
            return {"query": query, "count": count}
    except sqlite3.Error as e:
        logger.error("Database error in search_biorxiv_count: %s", e)
        return {"error": f"Database error: {e}"}
    except Exception as e:
        logger.exception("Unexpected error in search_biorxiv_count")
        return {"error": f"Internal error: {e}"}


@mcp.tool()
def biorxiv_categories() -> list[dict] | dict:
    """List all bioRxiv/medRxiv categories with paper counts."""
    wait = _search_bucket.consume()
    if wait is not None:
        return {"error": f"Rate limit exceeded. Try again in {wait:.1f} seconds."}

    logger.info("biorxiv_categories")
    try:
        with db.connection() as conn:
            return db.get_categories(conn)
    except sqlite3.Error as e:
        logger.error("Database error in biorxiv_categories: %s", e)
        return {"error": f"Database error: {e}"}


@mcp.tool()
async def sync_biorxiv() -> dict:
    """Sync papers from bioRxiv/medRxiv API.

    Runs delta sync if the database has been synced before, otherwise bulk sync.
    Bulk sync fetches all papers from 2013 to today and takes several hours.
    """
    wait = _sync_bucket.consume()
    if wait is not None:
        return {"error": f"Rate limit exceeded. Try again in {wait:.0f} seconds."}

    logger.info("sync_biorxiv starting")
    try:
        with db.connection() as conn:
            last = db.get_last_sync_date(conn)
            if last:
                count = await sync.delta_sync(conn)
                result = {
                    "status": "delta_sync_complete",
                    "new_papers": count,
                    "total_papers": db.get_paper_count(conn),
                    "last_sync": db.get_last_sync_date(conn),
                }
            else:
                count = await sync.bulk_sync(conn)
                result = {
                    "status": "bulk_sync_complete",
                    "total_papers": count,
                    "last_sync": db.get_last_sync_date(conn),
                }
            logger.info("sync_biorxiv complete: %s", result)
            return result
    except sqlite3.Error as e:
        logger.error("Database error in sync_biorxiv: %s", e)
        return {"error": f"Database error: {e}"}
    except RuntimeError as e:
        logger.error("Sync interrupted: %s", e)
        return {"error": str(e)}
    except Exception as e:
        logger.exception("Unexpected error in sync_biorxiv")
        return {"error": f"Internal error: {e}"}


@mcp.tool()
def biorxiv_status() -> dict:
    """Get the status of the local bioRxiv database."""
    logger.info("biorxiv_status")
    try:
        with db.connection() as conn:
            return {
                "paper_count": db.get_paper_count(conn),
                "last_sync": db.get_last_sync_date(conn),
                "db_size_mb": round(db.get_db_size_mb(), 2),
                "db_path": str(db.DB_PATH),
            }
    except sqlite3.Error as e:
        logger.error("Database error in biorxiv_status: %s", e)
        return {"error": f"Database error: {e}"}


@mcp.tool()
def get_paper(doi: str) -> dict:
    """Get detailed information for a paper by DOI.

    Checks the local database first, then falls back to the bioRxiv API
    for papers that haven't been synced yet.

    Args:
        doi: The paper DOI (e.g. "10.1101/2024.01.05.574328")
    """
    wait = _search_bucket.consume()
    if wait is not None:
        return {"error": f"Rate limit exceeded. Try again in {wait:.1f} seconds."}

    logger.info("get_paper doi=%r", doi)
    try:
        with db.connection() as conn:
            paper = db.get_paper(conn, doi)
            if paper:
                return paper
    except sqlite3.Error as e:
        logger.error("Database error in get_paper: %s", e)
        return {"error": f"Database error: {e}"}

    try:
        paper = sync.fetch_paper_by_doi(doi)
        if paper:
            paper["_source"] = "api"
            return paper
        return {"error": f"DOI {doi} not found in local database or bioRxiv API."}
    except httpx.HTTPError as e:
        logger.error("API error fetching DOI %s: %s", doi, e)
        return {"error": f"API error: {e}"}


@mcp.tool()
def download_paper(doi: str) -> dict:
    """Download a bioRxiv/medRxiv paper PDF by DOI.

    Saves to ~/.local/share/biorxiv-mcp/papers/{doi}.pdf

    Args:
        doi: The paper DOI (e.g. "10.1101/2024.01.05.574328")
    """
    wait = _search_bucket.consume()
    if wait is not None:
        return {"error": f"Rate limit exceeded. Try again in {wait:.1f} seconds."}

    logger.info("download_paper doi=%r", doi)
    try:
        with db.connection() as conn:
            paper = db.get_paper(conn, doi)
    except sqlite3.Error as e:
        logger.error("Database error in download_paper: %s", e)
        return {"error": f"Database error: {e}"}

    if not paper:
        paper = sync.fetch_paper_by_doi(doi)
    if not paper:
        return {"error": f"DOI {doi} not found on bioRxiv/medRxiv."}

    server = paper.get("server", "biorxiv")
    version = paper.get("version", "1")
    pdf_url = f"https://www.{server}.org/content/{doi}v{version}.full.pdf"

    try:
        with httpx.Client(follow_redirects=True, timeout=60.0) as client:
            resp = client.get(pdf_url)
            resp.raise_for_status()

            if not resp.content.startswith(b"%PDF"):
                return {"error": f"Response from {pdf_url} was not a PDF."}

            download_dir = db.DB_DIR / "papers"
            download_dir.mkdir(parents=True, exist_ok=True)
            safe_doi = doi.replace("/", "_")
            output = download_dir / f"{safe_doi}.pdf"
            output.write_bytes(resp.content)
            logger.info("Downloaded %s (%.2f MB)", output, len(resp.content) / (1024 * 1024))
            return {"path": str(output), "size_mb": round(len(resp.content) / (1024 * 1024), 2)}

    except httpx.HTTPError as e:
        logger.error("Download failed for %s: %s", doi, e)
        return {"error": f"Download failed: {e}"}


def health(request):
    """Health check handler for HTTP deployments."""
    from starlette.responses import JSONResponse
    try:
        with db.connection() as conn:
            return JSONResponse({
                "status": "ok",
                "paper_count": db.get_paper_count(conn),
                "last_sync": db.get_last_sync_date(conn),
            })
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)


if __name__ == "__main__":
    if TRANSPORT == "sse":
        import uvicorn
        from starlette.routing import Route

        app = mcp.sse_app()
        app.routes.append(Route("/health", health, methods=["GET"]))
        logger.info("Starting SSE server on %s:%d", HOST, PORT)
        uvicorn.run(app, host=HOST, port=PORT)
    else:
        mcp.run(transport="stdio")
