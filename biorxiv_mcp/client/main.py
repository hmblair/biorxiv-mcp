"""Entry point for the ``biorxiv-mcp`` stdio MCP client."""

from __future__ import annotations

import logging
import os


def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        level=getattr(logging, log_level, logging.WARNING),
    )
    from .tools import mcp
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
