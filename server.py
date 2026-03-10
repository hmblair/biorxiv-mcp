"""bioRxiv MCP server -- search and sync bioRxiv/medRxiv papers."""

import asyncio
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from biorxiv_mcp import db, sync

DOWNLOAD_DIR = Path.home() / ".local/share/biorxiv-mcp/papers"

mcp = FastMCP("biorxiv")


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
    conn = db.get_connection()
    try:
        db.init_db(conn)
        results = db.search(conn, query, limit=limit, category=category, after=after, before=before, detail=detail, sort=sort)
        if not results:
            count = db.get_paper_count(conn)
            if count == 0:
                return [{"message": "Database is empty. Run sync_biorxiv() first to populate it."}]
            return [{"message": f"No results for '{query}' (searched {count} papers)."}]
        return results
    finally:
        conn.close()


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
    conn = db.get_connection()
    try:
        db.init_db(conn)
        count = db.search_count(conn, query, category=category, after=after, before=before)
        return {"query": query, "count": count}
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


def _fetch_paper_from_api(doi: str) -> dict | None:
    """Fetch paper metadata from bioRxiv API by DOI."""
    with httpx.Client(timeout=30) as client:
        for server in ("biorxiv", "medrxiv"):
            try:
                resp = client.get(f"https://api.biorxiv.org/details/{server}/{doi}")
                data = resp.json()
                if data.get("collection"):
                    from biorxiv_mcp.sync import _normalize_paper
                    return _normalize_paper(data["collection"][-1], server)
            except httpx.HTTPError:
                continue
    return None


@mcp.tool()
def get_paper(doi: str) -> dict:
    """Get detailed information for a paper by DOI.

    Checks the local database first, then falls back to the bioRxiv API
    for papers that haven't been synced yet.

    Args:
        doi: The paper DOI (e.g. "10.1101/2024.01.05.574328")
    """
    conn = db.get_connection()
    try:
        db.init_db(conn)
        paper = db.get_paper(conn, doi)
        if paper:
            return paper
    finally:
        conn.close()

    paper = _fetch_paper_from_api(doi)
    if paper:
        paper["_source"] = "api"
        return paper
    return {"error": f"DOI {doi} not found in local database or bioRxiv API."}


@mcp.tool()
def download_paper(doi: str) -> dict:
    """Download a bioRxiv/medRxiv paper PDF by DOI.

    Saves to ~/.local/share/biorxiv-mcp/papers/{doi}.pdf

    Args:
        doi: The paper DOI (e.g. "10.1101/2024.01.05.574328")
    """
    conn = db.get_connection()
    try:
        db.init_db(conn)
        paper = db.get_paper(conn, doi)
    finally:
        conn.close()

    if not paper:
        # Try fetching metadata from API directly
        server = "biorxiv"
    else:
        server = paper.get("server", "biorxiv")

    # Resolve version and PDF URL
    try:
        with httpx.Client(follow_redirects=True, timeout=60.0) as client:
            if not paper:
                # Fetch from API to get version info
                for srv in ("biorxiv", "medrxiv"):
                    resp = client.get(f"https://api.biorxiv.org/details/{srv}/{doi}")
                    data = resp.json()
                    if data.get("collection"):
                        server = srv
                        version = data["collection"][-1].get("version", "1")
                        break
                else:
                    return {"error": f"DOI {doi} not found on bioRxiv/medRxiv."}
            else:
                version = paper.get("version", "1")

            pdf_url = f"https://www.{server}.org/content/{doi}v{version}.full.pdf"
            resp = client.get(pdf_url)
            resp.raise_for_status()

            if not resp.content.startswith(b"%PDF"):
                return {"error": f"Response from {pdf_url} was not a PDF."}

            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            safe_doi = doi.replace("/", "_")
            output = DOWNLOAD_DIR / f"{safe_doi}.pdf"
            output.write_bytes(resp.content)
            return {"path": str(output), "size_mb": round(len(resp.content) / (1024 * 1024), 2)}

    except httpx.HTTPError as e:
        return {"error": f"Download failed: {e}"}


if __name__ == "__main__":
    mcp.run()
