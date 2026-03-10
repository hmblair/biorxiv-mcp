"""Standalone sync script for running bulk or delta sync."""

import asyncio
import logging

from biorxiv_mcp import db, sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def progress(i, n, total):
    log.info(f"Interval {i}/{n} complete — {total} papers in db")


async def main():
    conn = db.get_connection()
    db.init_db(conn)

    last = db.get_last_sync_date(conn)
    cursor = db.get_bulk_sync_cursor(conn)
    count = db.get_paper_count(conn)

    if last:
        log.info(f"Delta sync from {last} ({count} papers in db)")
        new = await sync.delta_sync(conn)
        log.info(f"Delta sync complete — {new} new papers, {db.get_paper_count(conn)} total")
    elif cursor:
        log.info(f"Resuming bulk sync from {cursor} ({count} papers in db)")
        total = await sync.bulk_sync(conn, progress_callback=progress)
        log.info(f"Bulk sync complete — {total} papers")
    else:
        log.info("Starting fresh bulk sync")
        total = await sync.bulk_sync(conn, progress_callback=progress)
        log.info(f"Bulk sync complete — {total} papers")

    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
