"""bioRxiv MCP server -- search and sync bioRxiv/medRxiv papers."""

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

import httpx
from mcp.server.fastmcp import FastMCP

from . import db, sync
from .ratelimit import TokenBucket
from .toolkit import tool, validate_date

# -- Logging ------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
)
logger = logging.getLogger(__name__)

# -- Tool-level policy --------------------------------------------------------

MAX_SEARCH_LIMIT = 100
MAX_PDF_BYTES = 100 * 1024 * 1024  # 100 MB
_DOI_RE = re.compile(r"^10\.\d{4,9}/[A-Za-z0-9._\-;()/:]+$")

_search_bucket = TokenBucket(rate=10, burst=20)
_sync_bucket = TokenBucket(rate=1 / 60, burst=1)


def validate_doi(doi: str) -> str:
    if not _DOI_RE.match(doi):
        raise ValueError(f"Invalid DOI format: {doi!r}")
    return doi


def validate_date_range(after: str | None, before: str | None) -> tuple[str | None, str | None]:
    return validate_date(after, "after"), validate_date(before, "before")


# -- Server -------------------------------------------------------------------

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
TRANSPORT = os.environ.get("TRANSPORT", "http")

mcp = FastMCP("biorxiv", host=HOST, port=PORT)


@mcp.tool()
@tool(shape="list", bucket=_search_bucket)
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
        limit: Max results to return (default 10, capped at 100)
        category: Filter by category (e.g. "neuroscience", "genomics"). Use biorxiv_categories() to list available categories.
        after: Only papers on or after this date (YYYY-MM-DD)
        before: Only papers on or before this date (YYYY-MM-DD)
        detail: If True, return all fields including abstract (default False)
        sort: "relevance" (default) or "date" (newest first)
    """
    after, before = validate_date_range(after, before)
    limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    logger.info("search_biorxiv query=%r limit=%d category=%s sort=%s", query, limit, category, sort)

    with db.connection() as conn:
        results = db.search(conn, query, limit=limit, category=category,
                            after=after, before=before, detail=detail, sort=sort)
        if results:
            return results
        count = db.get_paper_count(conn)
        if count == 0:
            return [{"message": "Database is empty. Run sync_biorxiv() first to populate it."}]
        return [{"message": f"No results for '{query}' (searched {count} papers)."}]


@mcp.tool()
@tool(shape="dict", bucket=_search_bucket)
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
    after, before = validate_date_range(after, before)
    logger.info("search_biorxiv_count query=%r", query)
    with db.connection() as conn:
        return {"query": query, "count": db.search_count(conn, query, category=category,
                                                         after=after, before=before)}


@mcp.tool()
@tool(shape="dict", bucket=_search_bucket)
def biorxiv_categories() -> list[dict]:
    """List all bioRxiv/medRxiv categories with paper counts."""
    logger.info("biorxiv_categories")
    with db.connection() as conn:
        return db.get_categories(conn)


# -- Background sync ----------------------------------------------------------

class SyncState(TypedDict, total=False):
    status: str  # "idle" | "running"
    started_at: str | None
    finished_at: str | None
    error: str | None
    last_result: dict


_sync_task: asyncio.Task | None = None
_sync_state: SyncState = {"status": "idle", "started_at": None, "finished_at": None, "error": None}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _run_sync() -> None:
    """Background sync worker — uses a dedicated connection."""
    conn = db.get_connection()
    try:
        result = await sync.auto_sync(conn)
        _sync_state.update(status="idle", finished_at=_now(), last_result=result, error=None)
    except Exception as e:
        logger.exception("Background sync failed")
        _sync_state.update(status="idle", finished_at=_now(), error=str(e))
    finally:
        conn.close()


@mcp.tool()
@tool(shape="dict", bucket=_sync_bucket)
async def sync_biorxiv() -> dict:
    """Start a background sync of papers from bioRxiv/medRxiv.

    Returns immediately. Poll ``biorxiv_status()`` to see progress.
    Delta sync runs if the DB has been synced before; otherwise bulk sync
    (which can take several hours).
    """
    global _sync_task
    if _sync_task is not None and not _sync_task.done():
        return {"status": "already_running", "started_at": _sync_state["started_at"]}
    _sync_state.update(status="running", started_at=_now(), finished_at=None, error=None)
    _sync_task = asyncio.create_task(_run_sync())
    logger.info("sync_biorxiv scheduled")
    return {"status": "started", "started_at": _sync_state["started_at"]}


@mcp.tool()
@tool(shape="dict")
def biorxiv_status() -> dict:
    """Get the status of the local bioRxiv database."""
    logger.info("biorxiv_status")
    with db.connection() as conn:
        return {
            "paper_count": db.get_paper_count(conn),
            "last_sync": db.get_last_sync_date(conn),
            "bulk_sync_cursor": db.get_bulk_sync_cursor(conn),
            "db_size_mb": round(db.get_db_size_mb(), 2),
            "db_path": str(db.DB_PATH),
            "sync": dict(_sync_state),
        }


@mcp.tool()
@tool(shape="dict", bucket=_search_bucket)
def get_paper(doi: str) -> dict:
    """Get detailed information for a paper by DOI.

    Checks the local database first, then falls back to the bioRxiv API
    for papers that haven't been synced yet.

    Args:
        doi: The paper DOI (e.g. "10.1101/2024.01.05.574328")
    """
    doi = validate_doi(doi)
    logger.info("get_paper doi=%r", doi)
    with db.connection() as conn:
        paper = sync.resolve_paper(conn, doi)
    if paper:
        return paper
    return {"error": f"DOI {doi} not found in local database or bioRxiv API."}


@mcp.tool()
@tool(shape="dict", bucket=_search_bucket)
def download_paper(doi: str) -> dict:
    """Download a bioRxiv/medRxiv paper PDF by DOI.

    Saves to ~/.local/share/biorxiv-mcp/papers/{doi}.pdf

    Args:
        doi: The paper DOI (e.g. "10.1101/2024.01.05.574328")
    """
    doi = validate_doi(doi)
    logger.info("download_paper doi=%r", doi)
    with db.connection() as conn:
        paper = sync.resolve_paper(conn, doi)
    if not paper:
        return {"error": f"DOI {doi} not found on bioRxiv/medRxiv."}

    url = sync.pdf_url(doi, paper.get("server") or sync.DEFAULT_SERVER, paper.get("version") or 1)
    output = _pdf_output_path(doi)
    return _download_pdf(url, output)


def _pdf_output_path(doi: str) -> Path:
    """Compute the on-disk path for a DOI's PDF, ensuring it stays inside PAPERS_DIR."""
    db.PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    safe_doi = doi.replace("/", "_")
    output = db.PAPERS_DIR / f"{safe_doi}.pdf"
    if db.PAPERS_DIR.resolve() not in output.resolve().parents:
        raise ValueError("Refusing to write outside download directory.")
    return output


def _download_pdf(url: str, output: Path) -> dict:
    """Stream a PDF to ``output`` with size and content-type guards."""
    tmp = output.with_suffix(".pdf.part")
    try:
        with httpx.Client(follow_redirects=True, timeout=60.0) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                clen = resp.headers.get("content-length")
                if clen and int(clen) > MAX_PDF_BYTES:
                    return {"error": f"PDF exceeds size limit ({int(clen)} > {MAX_PDF_BYTES} bytes)."}
                total = 0
                with tmp.open("wb") as f:
                    for i, chunk in enumerate(resp.iter_bytes()):
                        if i == 0 and not chunk.startswith(b"%PDF"):
                            return {"error": f"Response from {url} was not a PDF."}
                        total += len(chunk)
                        if total > MAX_PDF_BYTES:
                            return {"error": f"PDF exceeded size limit of {MAX_PDF_BYTES} bytes."}
                        f.write(chunk)
        tmp.replace(output)
        logger.info("Downloaded %s (%.2f MB)", output, total / (1024 * 1024))
        return {"path": str(output), "size_mb": round(total / (1024 * 1024), 2)}
    except httpx.HTTPError as e:
        logger.error("Download failed for %s: %s", url, e)
        return {"error": f"Download failed: {e}"}
    finally:
        tmp.unlink(missing_ok=True)


# -- HTTP health endpoint -----------------------------------------------------

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


def main() -> None:
    """Entry point for the ``biorxiv-mcp`` console script."""
    if TRANSPORT == "stdio":
        mcp.run(transport="stdio")
        return
    import uvicorn
    from starlette.middleware.cors import CORSMiddleware
    from starlette.routing import Route

    # Streamable HTTP (modern MCP transport, also supports SSE clients).
    mcp.settings.streamable_http_path = "/mcp"
    app = mcp.streamable_http_app()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
    )
    app.routes.append(Route("/health", health, methods=["GET"]))
    logger.info("Starting server on %s:%d", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
