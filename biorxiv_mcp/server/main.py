"""Entry point for the ``biorxiv-mcp-server`` console script.

Subcommands:
    biorxiv-mcp-server              Start the REST API server (default)
    biorxiv-mcp-server keys add     Generate a new API key
    biorxiv-mcp-server keys list    List all keys
    biorxiv-mcp-server keys revoke  Revoke a key
"""

from __future__ import annotations

import argparse
import logging
import os
import sys


def _serve() -> None:
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


def _keys_add(args: argparse.Namespace) -> None:
    from . import db, keys
    conn = db.get_connection()
    raw = keys.generate(conn, label=args.label, unlimited=args.unlimited)
    conn.close()
    print(f"\nKey created. Save it now — it will not be shown again.\n")
    print(f"  Token:     {raw}")
    print(f"  Label:     {args.label}")
    print(f"  Unlimited: {'yes' if args.unlimited else 'no'}")
    print(f"  Key ID:    {keys.hash_token(raw)[:8]}")
    print()


def _keys_list(args: argparse.Namespace) -> None:
    from . import db, keys
    conn = db.get_connection()
    all_keys = keys.list_keys(conn, include_disabled=args.all)
    conn.close()
    if not all_keys:
        print("\nNo API keys configured. Create one with:\n  biorxiv-mcp-server keys add --label <name>\n")
        return
    print(f"\n{'ID':<10} {'Label':<25} {'Unlimited':<11} {'Created':<22} {'Status'}")
    print("-" * 78)
    for k in all_keys:
        status = "disabled" if k.disabled else "active"
        print(f"{k.key_id:<10} {k.label:<25} {'yes' if k.unlimited else 'no':<11} {k.created_at[:19]:<22} {status}")
    print()


def _keys_import(args: argparse.Namespace) -> None:
    from . import db, keys
    conn = db.get_connection()
    try:
        key_id = keys.import_token(conn, raw=args.token, label=args.label, unlimited=args.unlimited)
    except ValueError as e:
        print(f"\n{e}\n")
        sys.exit(1)
    finally:
        conn.close()
    print(f"\n  Imported as key ID {key_id} (label: {args.label}, unlimited: {'yes' if args.unlimited else 'no'})\n")


def _keys_revoke(args: argparse.Namespace) -> None:
    from . import db, keys
    conn = db.get_connection()
    key = keys.revoke(conn, args.key_id)
    conn.close()
    if key is None:
        print(f"\nNo key found matching '{args.key_id}'.\n")
        sys.exit(1)
    print(f"\nRevoked key {key.key_id} ({key.label}).\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="bioRxiv MCP REST API server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # Default: start server (no subcommand)

    # keys subcommand
    keys_parser = sub.add_parser("keys", help="Manage API keys")
    keys_sub = keys_parser.add_subparsers(dest="keys_action")

    add_p = keys_sub.add_parser("add", help="Generate a new API key")
    add_p.add_argument("--label", required=True, help="Human-readable label (e.g. 'hamish-macbook')")
    add_p.add_argument("--unlimited", action="store_true", help="Bypass rate limiting")

    list_p = keys_sub.add_parser("list", help="List all API keys")
    list_p.add_argument("--all", action="store_true", help="Include disabled keys")

    import_p = keys_sub.add_parser("import", help="Import an existing token")
    import_p.add_argument("--label", required=True, help="Human-readable label")
    import_p.add_argument("--token", required=True, help="Raw bearer token to import")
    import_p.add_argument("--unlimited", action="store_true", help="Bypass rate limiting")

    revoke_p = keys_sub.add_parser("revoke", help="Revoke an API key")
    revoke_p.add_argument("key_id", help="Key ID prefix (from 'keys list')")

    args = parser.parse_args()

    if args.command is None:
        _serve()
    elif args.command == "keys":
        if args.keys_action == "add":
            _keys_add(args)
        elif args.keys_action == "import":
            _keys_import(args)
        elif args.keys_action == "list":
            _keys_list(args)
        elif args.keys_action == "revoke":
            _keys_revoke(args)
        else:
            keys_parser.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
