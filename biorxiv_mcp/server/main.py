"""Entry point for the ``biorxiv-mcp-server`` console script."""

import logging
import os


def main() -> None:
    import uvicorn

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        level=getattr(logging, log_level, logging.INFO),
    )

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    forwarded = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")

    from .app import create_app
    app = create_app()

    logging.getLogger(__name__).info("Starting server on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, proxy_headers=True,
                forwarded_allow_ips=forwarded)


if __name__ == "__main__":
    main()
