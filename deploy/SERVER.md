# Server administration

This document covers running and managing the biorxiv-mcp REST API
server. For client (agent) setup, see the [README](../README.md).

## Architecture

```
Agents                                 Your server
──────                                 ───────────
biorxiv-mcp (stdio shim)  ── HTTPS ──▶  reverse proxy (Caddy, nginx, ...)
                                            │
                                            ▼
                                        biorxiv-mcp-server (REST API)
                                            └─ SQLite FTS5 index (~400k papers)
                                            └─ bioRxiv/medRxiv sync
```

The server is a Starlette app that manages the paper database and serves
authenticated JSON endpoints. It knows nothing about MCP — that layer
lives entirely on the client side.

## Setup

```sh
git clone https://github.com/hmblair/biorxiv-mcp && cd biorxiv-mcp
make                        # create venv + install
make install-service        # install systemd units (needs sudo)
```

The server binds to `127.0.0.1:8000` by default. Put a reverse proxy
in front to expose it over HTTPS.

### Initial sync

The database starts empty. The first sync fetches the entire
bioRxiv/medRxiv catalog (~400k papers, several hours):

```sh
.venv/bin/biorxiv-mcp-sync
```

Subsequent syncs are fast deltas. The systemd timer runs one daily at
04:00.

## API key management

Keys are stored in the SQLite database. Changes take effect immediately
with no service restart.

```sh
# Create a new key (token is shown once — save it)
biorxiv-mcp-server keys add --label "alice-laptop"

# Create an unlimited key (bypasses per-key rate limit)
biorxiv-mcp-server keys add --label "admin" --unlimited

# Import an existing token
biorxiv-mcp-server keys import --label "legacy" --token <raw-token> --unlimited

# List all active keys
biorxiv-mcp-server keys list

# Delete a key by its ID prefix
biorxiv-mcp-server keys delete <key_id>
```

When any keys exist in the database, all `/api/*` requests require
`Authorization: Bearer <token>`. `/health` is always unauthenticated.

Deleting all keys means no one can authenticate. Auth is only
disabled on a fresh install before any keys are created.

## REST API

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check (unauthed) |
| `GET` | `/api/search?q=...` | Full-text search |
| `GET` | `/api/search/count?q=...` | Count matches |
| `GET` | `/api/categories` | Categories with counts |
| `GET` | `/api/paper/{doi}` | Paper by DOI |
| `GET` | `/api/paper/{doi}/pdf` | Stream PDF |
| `GET` | `/api/status` | Database status |
| `POST` | `/api/sync` | Trigger background sync |

All `/api/*` endpoints return JSON. Errors use standard HTTP status
codes with a `{"error": "..."}` body.

## Service management

```sh
make start              # sudo systemctl start biorxiv-mcp
make stop               # sudo systemctl stop biorxiv-mcp
make restart            # sudo systemctl restart biorxiv-mcp
make status             # systemctl status biorxiv-mcp

journalctl -u biorxiv-mcp -f       # follow logs
journalctl -u biorxiv-mcp | grep "auth "   # auth failures
```

## Configuration

All settings are env vars. Copy `deploy/biorxiv-mcp.env.example` to
`deploy/biorxiv-mcp.env` — the systemd unit loads it automatically.

| Env var | Default | Purpose |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address |
| `PORT` | `8000` | Bind port |
| `CORS_ORIGINS` | `*` | Allowed CORS origins |
| `BIORXIV_MCP_KEY_RATE` | `1.0` | Per-key rate limit refill (req/s) |
| `BIORXIV_MCP_KEY_BURST` | `60` | Per-key rate limit bucket size |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Trusted proxy IPs for `X-Forwarded-For` |
| `LOG_LEVEL` | `INFO` | Log level |
| `BIORXIV_MCP_DATA` | `~/.local/share/biorxiv-mcp/data` | Database directory |

## Database

- **Location:** `~/.local/share/biorxiv-mcp/data/biorxiv.db`
- **Permissions:** directory 700, files 600 (enforced in code)
- **Contents:** papers table, FTS5 index, sync metadata, API keys
- **API key storage:** SHA-256 hashes only — raw tokens are never stored

## Project layout

```
biorxiv_mcp/
  server/
    app.py          # Starlette REST API routes
    auth.py         # Bearer-token middleware (reads keys from DB)
    db.py           # SQLite schema, FTS5 index, connection management
    mesh.py         # MeSH synonym expansion (auto-downloaded from NLM)
    keys.py         # API key CRUD (generate, import, list, delete)
    sync.py         # bioRxiv API client (bulk, delta, auto, resolve)
    ratelimit.py    # Token bucket
    sync_runner.py  # Standalone sync CLI
    main.py         # Server entry point + key management CLI
  client/
    tools.py        # MCP tool definitions
    api.py          # HTTP client for the REST API
    main.py         # stdio MCP entry point
deploy/
  biorxiv-mcp.service.in  # Templated systemd unit
  biorxiv-sync.service.in # Sync oneshot unit
  biorxiv-sync.timer      # Daily sync schedule
  install_mcp.py          # Register stdio shim with agent tools
  biorxiv-mcp.env.example # Env var reference
tests/                    # Unit + live integration tests
Makefile
```

## Development

```sh
uv pip install -e '.[test]'
make test                        # unit tests (66 tests)
BIORXIV_MCP_ENDPOINT=https://biorxiv.example.com \
BIORXIV_MCP_ENDPOINT_KEY=<token> \
make test-endpoint               # live tests against a deployed server
```
