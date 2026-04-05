"""Standalone sync script for running bulk or delta sync."""

import asyncio
import logging

from . import db, sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


async def _run() -> None:
    conn = db.get_connection()
    try:
        count = db.get_paper_count(conn)
        last = db.get_last_sync_date(conn)
        log.info(f"{'Delta' if last else 'Bulk'} sync starting ({count} papers in db)")
        result = await sync.auto_sync(conn)
        log.info(f"{result['kind']} sync complete — {result['count']} papers, "
                 f"{db.get_paper_count(conn)} total")
    finally:
        conn.close()


def main() -> None:
    """Entry point for the ``biorxiv-mcp-sync`` console script."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
