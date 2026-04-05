# biorxiv-mcp

An [MCP](https://modelcontextprotocol.io) server that lets agents search,
read, and download [bioRxiv](https://www.biorxiv.org) and
[medRxiv](https://www.medrxiv.org) preprints from a local SQLite FTS5 index.

## Architecture

```
Agent (Claude Code, etc.)           Server (your machine or a remote host)
─────────────────────────           ──────────────────────────────────────
Claude Code / Claude Desktop
    │  stdio
    ▼
biorxiv-mcp (local MCP shim)  ── HTTPS ──▶  biorxiv-mcp-server (REST API)
   defines all 7 tools                         └─ SQLite FTS5 index
   each tool calls the REST API                └─ bioRxiv sync
```

The **client** (`biorxiv-mcp`) is a local stdio MCP process that defines
the tools and makes HTTP calls to the REST API. It runs on the agent's
machine and never touches the database directly.

The **server** (`biorxiv-mcp-server`) manages the SQLite index and serves
a small set of authenticated JSON endpoints. It is unaware of MCP tools.

A reverse proxy (Caddy, nginx, cloudflared) terminates TLS between
the two. Both sides are packaged in this repo.

## Tools

| Tool | Description |
|---|---|
| `search_biorxiv` | Full-text search across titles, abstracts, authors, institutions. Supports FTS5 syntax (`AND`/`OR`/`NEAR`, quoted phrases, prefix matching), category and date filters, `sort=relevance\|date`. |
| `search_biorxiv_count` | Count matches without returning rows. |
| `biorxiv_categories` | List all categories with paper counts. |
| `get_paper` | Fetch a paper by DOI. Falls back to the bioRxiv API for unsynced DOIs. |
| `download_paper` | Download the PDF for a DOI to `~/.local/share/biorxiv-mcp/papers/`. |
| `sync_biorxiv` | Kick off a background delta (or bulk) sync on the server. |
| `biorxiv_status` | Paper count, last sync date, in-flight sync state. |

## Client install (agent's machine)

```sh
git clone https://github.com/hmblair/biorxiv-mcp && cd biorxiv-mcp
python3 -m venv .venv && .venv/bin/pip install -e .

export BIORXIV_MCP_ENDPOINT=https://biorxiv.example.com
export BIORXIV_MCP_ENDPOINT_KEY=<your-api-key>
make install
```

This registers a local stdio shim with Claude Code, Claude Desktop, and
OpenCode. The shim reads `BIORXIV_API_URL` and `BIORXIV_API_KEY` at
runtime. If the server is unreachable or returns an error, tools report
the HTTP status code instead of failing silently.

For localhost (server on the same machine, no auth):

```sh
make install
```

## Server install (admin)

```sh
make                        # create venv + install
make install-service        # systemd units (needs sudo)
```

The server runs at `http://127.0.0.1:8000` with a `/health` endpoint
and daily sync timer. Put a reverse proxy in front to expose it over
HTTPS. The server binds to localhost by default.

First run performs a bulk sync of the entire bioRxiv/medRxiv catalog
(~400k papers, several hours). Subsequent syncs are fast deltas.

```sh
.venv/bin/biorxiv-mcp-sync    # manual sync
make start / stop / restart / status
```

### API keys

Keys are stored in the SQLite database and managed via CLI. No restart
is needed to add or revoke keys.

```sh
biorxiv-mcp-server keys add --label "alice-laptop" --unlimited
biorxiv-mcp-server keys add --label "ci-bot"
biorxiv-mcp-server keys list
biorxiv-mcp-server keys revoke <key_id>
biorxiv-mcp-server keys import --label "admin" --token <raw> --unlimited
```

When any keys exist in the database, all `/api/*` requests require
`Authorization: Bearer <key>`. `/health` remains unauthenticated.
Revoking all keys locks out everyone (does not revert to open mode).

Keys with `--unlimited` bypass the per-key rate limit. Use sparingly
for trusted operators.

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

## Configuration

### Server env vars (`deploy/biorxiv-mcp.env`)

| Env var | Default | Purpose |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address |
| `PORT` | `8000` | Bind port |
| `CORS_ORIGINS` | `*` | Allowed CORS origins |
| `BIORXIV_MCP_KEY_RATE` | `1.0` | Per-key rate limit refill (req/s) |
| `BIORXIV_MCP_KEY_BURST` | `60` | Per-key rate limit bucket size |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Trusted proxy IPs |
| `LOG_LEVEL` | `INFO` | Log level |
| `BIORXIV_MCP_DATA` | `~/.local/share/biorxiv-mcp` | DB directory |

### Client env vars (set at install time)

| Env var | Default | Purpose |
|---|---|---|
| `BIORXIV_API_URL` | `http://localhost:8000` | REST API base URL |
| `BIORXIV_API_KEY` | *(unset)* | Bearer token |

## Project layout

```
biorxiv_mcp/
  server/
    app.py          # Starlette REST API
    auth.py         # Bearer-token middleware (reads keys from DB)
    db.py           # SQLite schema, FTS5 index, connection management
    keys.py         # API key CRUD (generate, import, list, revoke)
    sync.py         # bioRxiv API client (bulk, delta, auto, resolve)
    ratelimit.py    # Token bucket
    sync_runner.py  # Standalone sync CLI
    main.py         # Server entry point + key management CLI
  client/
    tools.py        # MCP tool definitions
    api.py          # HTTP client for the REST API
    main.py         # stdio MCP entry point
deploy/
  biorxiv-mcp.service.in  # Templated systemd unit (server)
  biorxiv-sync.service.in # Sync oneshot unit
  biorxiv-sync.timer      # Daily schedule
  install_mcp.py          # Register stdio shim with agent tools
  biorxiv-mcp.env.example # Server env var reference
tests/                    # Unit + integration tests
Makefile
```

## Development

```sh
uv pip install -e '.[test]'
make test                        # unit tests
BIORXIV_MCP_ENDPOINT=https://biorxiv.example.com \
BIORXIV_MCP_ENDPOINT_KEY=<token> \
make test-endpoint               # live tests against a deployed server
```

## License

MIT — see [LICENSE](LICENSE).
