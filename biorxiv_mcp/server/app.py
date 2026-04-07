"""REST API for the bioRxiv/medRxiv index.

The server is tool-unaware: it exposes a small set of JSON endpoints and
authenticates requests with a bearer token. The MCP tool layer lives in
``biorxiv_mcp.client`` and calls into this API.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import db, sync
from .auth import BearerAuth

logger = logging.getLogger(__name__)

# -- Policy -------------------------------------------------------------------

MAX_SEARCH_LIMIT = 100_000
MAX_PDF_BYTES = 100 * 1024 * 1024  # 100 MB
_DOI_RE = re.compile(r"^10\.\d{4,9}/[A-Za-z0-9._\-;()/:]+$")
_CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]


# -- Validation ---------------------------------------------------------------


def _date(s: str | None, field: str) -> str | None:
    if s is None or s == "":
        return None
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{field} must be YYYY-MM-DD, got {s!r}")
    return s


def _int(s: str | None, field: str, default: int, lo: int, hi: int) -> int:
    if s is None or s == "":
        return default
    try:
        v = int(s)
    except ValueError:
        raise ValueError(f"{field} must be an integer, got {s!r}")
    return max(lo, min(v, hi))


def _bool(s: str | None) -> bool:
    return s is not None and s.lower() in ("1", "true", "yes")


def _validate_doi(doi: str) -> str:
    if not _DOI_RE.match(doi):
        raise ValueError(f"Invalid DOI format: {doi!r}")
    return doi


def _error(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


# -- Background sync ----------------------------------------------------------

_sync_task: asyncio.Task | None = None
_sync_state: dict = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "error": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _run_sync() -> None:
    conn = db.get_connection()
    try:
        result = await sync.auto_sync(conn)
        _sync_state.update(status="idle", finished_at=_now(), last_result=result, error=None)
    except Exception as e:
        logger.exception("Background sync failed")
        _sync_state.update(status="idle", finished_at=_now(), error=str(e))
    finally:
        conn.close()


# -- Route handlers -----------------------------------------------------------


async def health(request: Request) -> Response:
    try:
        with db.connection() as conn:
            return JSONResponse(
                {
                    "status": "ok",
                    "paper_count": db.get_paper_count(conn),
                    "last_sync": db.get_last_sync_date(conn),
                }
            )
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)


async def search(request: Request) -> Response:
    q = request.query_params
    try:
        query = q.get("q", "")
        limit = _int(q.get("limit"), "limit", default=10, lo=1, hi=MAX_SEARCH_LIMIT)
        after = _date(q.get("after"), "after")
        before = _date(q.get("before"), "before")
        detail = _bool(q.get("detail"))
        sort = q.get("sort", "relevance")
        raw_cat = q.get("category") or None
        category: str | list[str] | None = None
        if raw_cat:
            parts = [c.strip() for c in raw_cat.split(",") if c.strip()]
            category = parts if len(parts) > 1 else parts[0] if parts else None
    except ValueError as e:
        return _error(str(e))

    try:
        with db.connection() as conn:
            results = db.search(
                conn,
                query,
                limit=limit,
                category=category,
                after=after,
                before=before,
                detail=detail,
                sort=sort,
            )
        return JSONResponse(results)
    except sqlite3.Error as e:
        logger.error("search db error: %s", e)
        return _error(f"Database error: {e}", 500)


async def search_count(request: Request) -> Response:
    q = request.query_params
    try:
        after = _date(q.get("after"), "after")
        before = _date(q.get("before"), "before")
        raw_cat = q.get("category") or None
        category: str | list[str] | None = None
        if raw_cat:
            parts = [c.strip() for c in raw_cat.split(",") if c.strip()]
            category = parts if len(parts) > 1 else parts[0] if parts else None
    except ValueError as e:
        return _error(str(e))
    try:
        with db.connection() as conn:
            n = db.search_count(
                conn,
                q.get("q", ""),
                category=category,
                after=after,
                before=before,
            )
        return JSONResponse({"count": n})
    except sqlite3.Error as e:
        return _error(f"Database error: {e}", 500)


async def categories(request: Request) -> Response:
    try:
        with db.connection() as conn:
            return JSONResponse(db.get_categories(conn))
    except sqlite3.Error as e:
        return _error(f"Database error: {e}", 500)


async def get_paper(request: Request) -> Response:
    try:
        doi = _validate_doi(request.path_params["doi"])
    except ValueError as e:
        return _error(str(e))
    try:
        with db.connection() as conn:
            paper = sync.resolve_paper(conn, doi)
    except sqlite3.Error as e:
        return _error(f"Database error: {e}", 500)
    if paper is None:
        return _error(f"DOI {doi} not found", 404)
    return JSONResponse(paper)


async def download_pdf(request: Request) -> Response:
    try:
        doi = _validate_doi(request.path_params["doi"])
    except ValueError as e:
        return _error(str(e))
    try:
        with db.connection() as conn:
            paper = sync.resolve_paper(conn, doi)
    except sqlite3.Error as e:
        return _error(f"Database error: {e}", 500)
    if paper is None:
        return _error(f"DOI {doi} not found", 404)

    url = sync.pdf_url(doi, paper.get("server") or sync.DEFAULT_SERVER, paper.get("version") or 1)

    async def _stream():
        total = 0
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        return
                    clen = resp.headers.get("content-length")
                    if clen and int(clen) > MAX_PDF_BYTES:
                        return
                    first = True
                    async for chunk in resp.aiter_bytes():
                        if first:
                            if not chunk.startswith(b"%PDF"):
                                return
                            first = False
                        total += len(chunk)
                        if total > MAX_PDF_BYTES:
                            return
                        yield chunk
        except httpx.HTTPError as e:
            logger.error("PDF fetch failed for %s: %s", doi, e)

    return StreamingResponse(_stream(), media_type="application/pdf")


async def status_endpoint(request: Request) -> Response:
    try:
        with db.connection() as conn:
            return JSONResponse(
                {
                    "paper_count": db.get_paper_count(conn),
                    "last_sync": db.get_last_sync_date(conn),
                    "bulk_sync_cursor": db.get_bulk_sync_cursor(conn),
                    "db_size_mb": round(db.get_db_size_mb(), 2),
                    "db_path": str(db.DB_PATH),
                    "sync": dict(_sync_state),
                }
            )
    except sqlite3.Error as e:
        return _error(f"Database error: {e}", 500)


async def start_sync(request: Request) -> Response:
    global _sync_task
    if _sync_task is not None and not _sync_task.done():
        return JSONResponse({"status": "already_running", "started_at": _sync_state["started_at"]})
    _sync_state.update(status="running", started_at=_now(), finished_at=None, error=None)
    _sync_task = asyncio.create_task(_run_sync())
    logger.info("sync scheduled")
    return JSONResponse({"status": "started", "started_at": _sync_state["started_at"]})


# -- Homepage -----------------------------------------------------------------


def _render_homepage() -> str:
    """Render README.md to an HTML page at startup."""
    from pathlib import Path

    readme_path = Path(__file__).resolve().parent.parent.parent / "README.md"
    try:
        from markdown_it import MarkdownIt

        md = MarkdownIt()
        body = md.render(readme_path.read_text())
    except Exception:
        body = (
            f"<pre>{readme_path.read_text()}</pre>"
            if readme_path.exists()
            else "<p>biorxiv-mcp</p>"
        )
    return (
        "<!doctype html><html><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>biorxiv-mcp</title>"
        "<style>"
        "body { max-width: 50rem; margin: 2rem auto; padding: 0 1rem; "
        "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; "
        "line-height: 1.6; color: #333; }"
        "pre, code { background: #f5f5f5; border-radius: 4px; }"
        "pre { padding: 1rem; overflow-x: auto; }"
        "code { padding: 0.15em 0.3em; }"
        "pre code { padding: 0; background: none; }"
        "table { border-collapse: collapse; width: 100%; }"
        "th, td { border: 1px solid #ddd; padding: 0.5rem; text-align: left; }"
        "th { background: #f5f5f5; }"
        "a { color: #0366d6; }"
        "</style>"
        f"</head><body>{body}</body></html>"
    )


_HOMEPAGE_HTML: str | None = None


async def homepage(request: Request) -> Response:
    global _HOMEPAGE_HTML
    if _HOMEPAGE_HTML is None:
        _HOMEPAGE_HTML = _render_homepage()
    from starlette.responses import HTMLResponse

    return HTMLResponse(_HOMEPAGE_HTML)


# -- App factory --------------------------------------------------------------


def create_app() -> Starlette:
    routes = [
        Route("/", homepage, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/api/search", search, methods=["GET"]),
        Route("/api/search/count", search_count, methods=["GET"]),
        Route("/api/categories", categories, methods=["GET"]),
        Route("/api/paper/{doi:path}/pdf", download_pdf, methods=["GET"]),
        Route("/api/paper/{doi:path}", get_paper, methods=["GET"]),
        Route("/api/status", status_endpoint, methods=["GET"]),
        Route("/api/sync", start_sync, methods=["POST"]),
    ]
    middleware = [
        Middleware(BearerAuth),
        Middleware(
            CORSMiddleware,
            allow_origins=_CORS_ORIGINS,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        ),
    ]
    return Starlette(routes=routes, middleware=middleware)
