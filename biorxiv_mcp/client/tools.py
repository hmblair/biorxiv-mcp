"""MCP tool definitions for biorxiv-mcp.

Each tool delegates to the REST API via ``BiorxivApi``. HTTP errors
surface as structured tool errors (``{"error": "HTTP 403: ..."}``),
not connection-level MCP failures.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from .api import ApiError, BiorxivApi
from .config import get_api_key, get_url

logger = logging.getLogger(__name__)

PAPERS_DIR = Path(
    os.environ.get("BIORXIV_MCP_PAPERS", Path.home() / ".local/share/biorxiv-mcp/papers")
)

mcp = FastMCP("biorxiv")


def _api() -> BiorxivApi:
    """Construct a client from config on each call.

    Using a fresh client per call avoids stale-connection issues over
    long-lived stdio sessions. httpx.Client handles connection pooling
    internally.
    """
    return BiorxivApi(get_url(), api_key=get_api_key())


def _api_call(fn):
    """Turn ApiError / connection errors into tool-level error dicts."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ApiError as e:
            return {"error": str(e)}
        except (OSError, httpx.HTTPError) as e:
            logger.exception("Unexpected error in %s", fn.__name__)
            return {"error": f"Connection error: {e}"}

    return wrapper


# -- Tools --------------------------------------------------------------------


@mcp.tool()
@_api_call
def search_biorxiv(
    query: str = "",
    limit: int = 50,
    category: str | list[str] | None = None,
    after: str | None = None,
    before: str | None = None,
    detail: bool = False,
    sort: str = "relevance",
) -> list[dict] | dict:
    """Search bioRxiv/medRxiv papers by keyword, or browse by date/category.

    Searches titles, abstracts, authors, and institutions. Works like
    PubMed: all keywords must match (implicit AND), and each keyword is
    automatically expanded with MeSH synonyms so "cancer" also finds
    "tumor" and "neoplasm".

    Returns compact results (doi, title, authors, date, category) by
    default. Set detail=True to include abstract, institution, license,
    and other fields.

    Browsing mode:
        Omit query to list papers by date/category without keyword
        matching. Useful for scanning recent preprints in a time window.
        Results are sorted by date (newest first) when no query is given.

    Query syntax:
        - Multiple words: "CRISPR cancer" (implicit AND — both required)
        - Explicit OR: "CRISPR OR cancer" (either matches)
        - Exclude terms: "CRISPR NOT cas9"
        - Exact phrase: '"single cell RNA"'
        - Hyphenated terms: "mRNA-seq" (matched as a phrase)
        - Prefix/truncation: "CRISPR*" (matches CRISPRi, etc.)
        - MeSH expansion is automatic (cancer → tumor, neoplasm, ...)
        - Multi-word MeSH terms are recognized: "ribonucleic acid"
          is grouped and expanded to include "RNA"

    Tips:
        - Use distinctive keywords (author names, specific methods)
        - More keywords narrow results (AND behavior), unlike OR search
        - Combine with category/date filters to narrow further

    Args:
        query: Search keywords or FTS5 query expression (optional —
            omit to browse by date/category)
        limit: Max results to return (default 50)
        category: Filter by category — a single string (e.g. "neuroscience")
            or a list of strings (e.g. ["bioinformatics", "biophysics"]).
            Use biorxiv_categories() to list available categories.
        after: Only papers on or after this date (YYYY-MM-DD)
        before: Only papers on or before this date (YYYY-MM-DD)
        detail: If True, return all fields including abstract (default False)
        sort: "relevance" (default) or "date" (newest first).
            Automatically uses "date" when no query is given.
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
        desc = f"for '{query}'" if query.strip() else "matching those filters"
        return {"message": f"No results {desc} (searched {count} papers)."}
    return results


@mcp.tool()
@_api_call
def biorxiv_categories() -> list[dict] | dict:
    """List all bioRxiv/medRxiv categories with paper counts."""
    return _api().categories()


@mcp.tool()
@_api_call
def get_paper(doi: str) -> dict:
    """Get detailed metadata for a paper by DOI.

    Returns all fields: title, authors, abstract, date, category,
    institution, license, version, etc. Falls back to the bioRxiv API
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
