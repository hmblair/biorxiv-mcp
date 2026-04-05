"""bioRxiv API client for bulk and delta sync."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from datetime import date, datetime, timedelta

import httpx

from . import db
from .db import PAPER_FIELDS

logger = logging.getLogger(__name__)

BASE_URL = "https://api.biorxiv.org/details"
SERVERS = ("biorxiv", "medrxiv")
DEFAULT_SERVER = "biorxiv"


def pdf_url(doi: str, server: str = DEFAULT_SERVER, version: str | int = 1) -> str:
    """Construct the public PDF URL for a paper."""
    return f"https://www.{server}.org/content/{doi}v{version}.full.pdf"
PAGE_SIZE = 100
MAX_RETRIES = 5
RETRY_DELAY = 10  # seconds


async def fetch_page(
    client: httpx.AsyncClient, server: str, start: str, end: str, cursor: int
) -> dict:
    """Fetch a single page from the bioRxiv API with retries."""
    url = f"{BASE_URL}/{server}/{start}/{end}/{cursor}/json"
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.get(url, timeout=90)
            resp.raise_for_status()
            data = resp.json()
            if "collection" not in data:
                raise ValueError(f"Unexpected API response: {list(data.keys())}")
            return data
        except Exception as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_DELAY * (2 ** attempt)
                logger.warning(f"Retry {attempt + 1}/{MAX_RETRIES} for {url}: {e} (waiting {wait}s)")
                await asyncio.sleep(wait)
    assert last_exc is not None
    raise last_exc


def normalize_paper(paper: dict, server: str) -> dict:
    """Extract the fields we store from a raw API response dict."""
    return {f: paper.get(f, "") for f in PAPER_FIELDS if f != "server"} | {"server": server}


def fetch_paper_by_doi(doi: str) -> dict | None:
    """Fetch a single paper's metadata from the API by DOI (synchronous)."""
    with httpx.Client(timeout=30) as client:
        for server in SERVERS:
            try:
                resp = client.get(f"{BASE_URL}/{server}/{doi}")
                data = resp.json()
                if data.get("collection"):
                    return normalize_paper(data["collection"][-1], server)
            except httpx.HTTPError:
                continue
    return None


async def fetch_range(
    client: httpx.AsyncClient, server: str, start: str, end: str
) -> AsyncGenerator[list[dict], None]:
    """Yield pages of papers for a date range."""
    cursor = 0
    while True:
        data = await fetch_page(client, server, start, end, cursor)
        papers = data.get("collection", [])
        if not papers:
            break
        yield [normalize_paper(p, server) for p in papers]
        total = int(data.get("messages", [{}])[0].get("total", 0))
        cursor += PAGE_SIZE
        if cursor >= total:
            break


async def _sync_interval(
    client: httpx.AsyncClient, conn, server: str, start: str, end: str
) -> int:
    """Sync a single date interval. Returns paper count."""
    count = 0
    async for page in fetch_range(client, server, start, end):
        count += db.upsert_papers(conn, page)
    return count


async def bulk_sync(conn, progress_callback=None) -> int:
    """Fetch all papers from 2013-01-01 to today. Returns total paper count."""
    cursor = db.get_bulk_sync_cursor(conn)
    start_date = date(2013, 1, 1)
    if cursor:
        start_date = datetime.strptime(cursor, "%Y-%m-%d").date() + timedelta(days=1)
        logger.info(f"Resuming bulk sync from {start_date} ({db.get_paper_count(conn)} papers in db)")

    today = date.today()
    intervals = []
    d = start_date
    while d < today:
        end_d = min(d + timedelta(days=29), today)
        intervals.append((d.isoformat(), end_d.isoformat()))
        d = end_d + timedelta(days=1)

    if not intervals:
        return db.get_paper_count(conn)

    total_new = 0

    async with httpx.AsyncClient() as client:
        for i, (start, end) in enumerate(intervals):
            try:
                for server in SERVERS:
                    total_new += await _sync_interval(client, conn, server, start, end)
                db.set_bulk_sync_cursor(conn, end)
            except Exception:
                logger.exception(f"Failed interval {start} to {end} after {MAX_RETRIES} retries")
                raise RuntimeError(
                    f"Bulk sync stopped at interval {start}-{end}. "
                    f"{db.get_paper_count(conn)} papers saved. Will resume on restart."
                )

            if progress_callback:
                progress_callback(i + 1, len(intervals), db.get_paper_count(conn))

    db.set_last_sync_date(conn, today.isoformat())
    db.clear_bulk_sync_cursor(conn)
    return db.get_paper_count(conn)


async def auto_sync(conn) -> dict:
    """Run delta sync if the DB has been synced before, otherwise bulk sync.

    Returns a dict describing the run: ``{"kind": "delta"|"bulk", "count": N}``.
    """
    if db.get_last_sync_date(conn):
        count = await delta_sync(conn)
        return {"kind": "delta", "count": count}
    count = await bulk_sync(conn)
    return {"kind": "bulk", "count": count}


def resolve_paper(conn, doi: str) -> dict | None:
    """Look up a paper locally, falling back to the bioRxiv API."""
    paper = db.get_paper(conn, doi)
    if paper:
        return paper
    api_paper = fetch_paper_by_doi(doi)
    if api_paper:
        api_paper["_source"] = "api"
    return api_paper


async def delta_sync(conn) -> int:
    """Fetch papers from last_sync_date to today."""
    last = db.get_last_sync_date(conn)
    if not last:
        return await bulk_sync(conn)

    today = date.today().isoformat()
    total = 0
    async with httpx.AsyncClient() as client:
        for server in SERVERS:
            total += await _sync_interval(client, conn, server, last, today)

    db.set_last_sync_date(conn, today)
    return total
