"""MCP tool definitions for biorxiv-mcp.

Each tool delegates to the REST API via ``BiorxivApi``. HTTP errors
surface as structured tool errors (``{"error": "HTTP 403: ..."}``),
not connection-level MCP failures.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .api import ApiError, BiorxivApi

logger = logging.getLogger(__name__)

PAPERS_DIR = Path(
    os.environ.get("BIORXIV_MCP_PAPERS", Path.home() / ".local/share/biorxiv-mcp/papers")
)

mcp = FastMCP("biorxiv")


def _api() -> BiorxivApi:
    """Construct a client from env vars on each call.

    Using a fresh client per call avoids stale-connection issues over
    long-lived stdio sessions. httpx.Client handles connection pooling
    internally.
    """
    base_url = os.environ.get("BIORXIV_API_URL", "http://localhost:8000")
    api_key = os.environ.get("BIORXIV_API_KEY", "")
    return BiorxivApi(base_url, api_key=api_key or None)


def _api_call(fn):
    """Turn ApiError / connection errors into tool-level error dicts."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ApiError as e:
            return {"error": str(e)}
        except Exception as e:
            logger.exception("Unexpected error in %s", fn.__name__)
            return {"error": f"Connection error: {e}"}

    return wrapper


# -- Tools --------------------------------------------------------------------


@mcp.tool()
@_api_call
def search_biorxiv(
    query: str,
    limit: int = 10,
    category: str | None = None,
    after: str | None = None,
    before: str | None = None,
    detail: bool = False,
    sort: str = "relevance",
) -> list[dict] | dict:
    """Search bioRxiv/medRxiv papers by keyword.

    Searches titles, abstracts, authors, and institutions. Keywords are
    joined with OR by default, so more keywords improve ranking without
    eliminating results. Use explicit AND for strict matching.

    Returns compact results (doi, title, authors, date, category) by
    default. Set detail=True to include abstract, institution, license,
    and other fields.

    Query syntax:
        - Multiple words: "CRISPR mRNA degradation" (OR, ranked by relevance)
        - Require all terms: "CRISPR AND cancer"
        - Exclude terms: "CRISPR NOT cas9"
        - Exact phrase: '"single cell RNA"'
        - Proximity: "NEAR(CRISPR cancer, 5)"
        - Prefix matching is automatic for words >= 3 characters

    Tips:
        - Use distinctive keywords (author names, specific methods)
        - Prefer fewer specific terms over many generic ones
        - Combine with category/date filters to narrow results

    Args:
        query: Search keywords or FTS5 query expression
        limit: Max results to return (default 10, capped at 100)
        category: Filter by category (e.g. "neuroscience", "genomics").
            Use biorxiv_categories() to list available categories.
        after: Only papers on or after this date (YYYY-MM-DD)
        before: Only papers on or before this date (YYYY-MM-DD)
        detail: If True, return all fields including abstract (default False)
        sort: "relevance" (default) or "date" (newest first)
    """
    api = _api()
    results = api.search(
        query,
        limit=limit,
        category=category,
        after=after,
        before=before,
        detail=detail,
        sort=sort,
    )
    if not results:
        status = api.status()
        count = status.get("paper_count", 0)
        if count == 0:
            return {"message": "Database is empty. Run sync_biorxiv() first to populate it."}
        return {"message": f"No results for '{query}' (searched {count} papers)."}
    return results


@mcp.tool()
@_api_call
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
    return _api().search_count(query, category=category, after=after, before=before)


@mcp.tool()
@_api_call
def biorxiv_categories() -> list[dict] | dict:
    """List all bioRxiv/medRxiv categories with paper counts."""
    return _api().categories()


@mcp.tool()
@_api_call
def get_paper(doi: str) -> dict:
    """Get detailed information for a paper by DOI.

    Checks the local database first, then falls back to the bioRxiv API
    for papers that haven't been synced yet.

    Args:
        doi: The paper DOI (e.g. "10.1101/2024.01.05.574328")
    """
    return _api().get_paper(doi)


@mcp.tool()
@_api_call
def download_paper(doi: str) -> dict:
    """Download a bioRxiv/medRxiv paper PDF by DOI.

    Saves to ~/.local/share/biorxiv-mcp/papers/{doi}.pdf

    Args:
        doi: The paper DOI (e.g. "10.1101/2024.01.05.574328")
    """
    pdf = _api().download_pdf(doi)
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    safe = doi.replace("/", "_")
    out = PAPERS_DIR / f"{safe}.pdf"
    out.write_bytes(pdf)
    return {"path": str(out), "size_mb": round(len(pdf) / (1024 * 1024), 2)}


@mcp.tool()
@_api_call
async def sync_biorxiv() -> dict:
    """Start a background sync of papers from bioRxiv/medRxiv.

    Returns immediately. Poll ``biorxiv_status()`` to see progress.
    Delta sync runs if the DB has been synced before; otherwise bulk sync
    (which can take several hours).
    """
    return _api().sync()


@mcp.tool()
@_api_call
def biorxiv_status() -> dict:
    """Get the status of the local bioRxiv database."""
    return _api().status()
