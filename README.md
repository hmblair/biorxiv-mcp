# biorxiv-mcp

An [MCP](https://modelcontextprotocol.io) server that lets agents search,
read, and download [bioRxiv](https://www.biorxiv.org) and
[medRxiv](https://www.medrxiv.org) preprints from a local SQLite FTS5 index.

A background job syncs the full bioRxiv/medRxiv catalog (roughly 400k
papers from 2013 onward) into `~/.local/share/biorxiv-mcp/biorxiv.db`.
Agent queries run entirely against the local index — fast, deterministic,
and with no bioRxiv API rate-limit exposure during search.

## Tools

| Tool | Description |
|---|---|
| `search_biorxiv` | Full-text search across titles, abstracts, authors, institutions. Supports FTS5 syntax (`AND`/`OR`/`NEAR`, quoted phrases, prefix matching), category and date filters, `sort=relevance\|date`. |
| `search_biorxiv_count` | Count matches without returning rows — useful for narrowing filters. |
| `biorxiv_categories` | List all categories with paper counts. |
| `get_paper` | Fetch a paper by DOI. Falls back to the bioRxiv API for unsynced DOIs. |
| `download_paper` | Stream the PDF for a DOI to `~/.local/share/biorxiv-mcp/papers/`. |
| `sync_biorxiv` | Kick off a background delta (or bulk) sync. Returns immediately; poll `biorxiv_status`. |
| `biorxiv_status` | DB size, paper count, last sync date, in-flight sync state. |

## Install

```sh
make                    # create venv and install package in editable mode
make install-service    # install systemd system units (needs sudo)
make install            # register the HTTP endpoint with Claude Code,
                        # Claude Desktop, and OpenCode
```

The server runs as a systemd system service at `http://localhost:8000/mcp`
with a `/health` endpoint. The sync timer runs a delta sync daily at 04:00.
The service runs as `$USER` by default; override with
`make install-service RUN_USER=someone`.

First run performs a bulk sync of the entire catalog (several hours).
Subsequent syncs are fast deltas. You can trigger a sync manually:

```sh
.venv/bin/biorxiv-mcp-sync
```

## Deploying publicly (HTTPS + API keys)

For a public deployment, put a reverse proxy (Caddy, nginx, cloudflared)
in front to terminate TLS and enable bearer-token auth in the MCP server
itself.

**1. Generate API keys** and put them in `deploy/biorxiv-mcp.env`:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
# BIORXIV_MCP_API_KEYS=key1,key2,key3
```

When `BIORXIV_MCP_API_KEYS` is set, every request to `/mcp` must send
`Authorization: Bearer <key>`. Keys are hashed at startup and compared in
constant time. `/health` remains unauthenticated. A per-key rate limit
(default 60-request burst, 1/s refill) is enforced by the middleware.

**2. Terminate TLS at a reverse proxy** that forwards to
`127.0.0.1:8000`. The MCP server stays bound to localhost; only the
proxy is exposed. Most proxies (Caddy, cloudflared) will handle Let's
Encrypt certificate issuance and renewal automatically.

**3. Register with an agent.** For Claude Code:

```sh
claude mcp add --transport http --scope user biorxiv-mcp \
  --header "Authorization: Bearer <your-key>" \
  https://biorxiv-mcp.yourdomain.com/mcp
```

Or, from a clone of this repo on any machine:

```sh
export BIORXIV_MCP_ENDPOINT=https://biorxiv-mcp.yourdomain.com
export BIORXIV_MCP_ENDPOINT_KEY=<your-key>
make install
```

This registers the endpoint + auth header with Claude Code, Claude
Desktop, and OpenCode in one shot. The client machine does not need
the server installed — only `python3` and this repo.

For a single trusted operator/agent that should bypass rate limiting,
put the key in `BIORXIV_MCP_UNLIMITED_KEYS` instead of
`BIORXIV_MCP_API_KEYS`. Unlimited keys are implicitly valid — no need
to list them in both.

Revoke a key by removing it from the env file and `make restart`.

## Configuration

All settings are env vars, defaulted in `deploy/biorxiv-mcp.env.example`.
Copy it to `deploy/biorxiv-mcp.env` and edit — the systemd unit loads it
automatically.

| Env var | Default | Purpose |
|---|---|---|
| `HOST` | `127.0.0.1` | HTTP bind address (set to `0.0.0.0` only without a reverse proxy) |
| `PORT` | `8000` | HTTP port |
| `TRANSPORT` | `http` | `http` (streamable HTTP) or `stdio` |
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins |
| `BIORXIV_MCP_API_KEYS` | *(unset)* | Comma-separated bearer tokens. Unset = open mode. |
| `BIORXIV_MCP_UNLIMITED_KEYS` | *(unset)* | Bearer tokens that bypass rate limiting. Implicitly valid. |
| `BIORXIV_MCP_KEY_RATE` | `1.0` | Per-key token refill (req/s) |
| `BIORXIV_MCP_KEY_BURST` | `60` | Per-key bucket size |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Trusted proxy IPs for `X-Forwarded-For` |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `BIORXIV_MCP_DATA` | `~/.local/share/biorxiv-mcp` | DB + PDF directory |

## Project layout

```
biorxiv_mcp/
  server.py       # MCP tool handlers + FastMCP + Starlette app
  sync.py         # bioRxiv API client: bulk, delta, auto, resolve, pdf_url
  db.py           # SQLite schema, FTS5 index, connection management
  auth.py         # Bearer-token middleware + per-key rate limiting
  ratelimit.py    # Token bucket rate limiter
  toolkit.py      # Shared tool decorator (rate limit, errors, envelope)
  sync_runner.py  # Standalone CLI sync entry point
deploy/
  biorxiv-mcp.service.in  # Templated systemd unit
  biorxiv-sync.service.in # Sync oneshot unit
  biorxiv-sync.timer      # Daily schedule
  install_mcp.py          # Register with agent tools
  biorxiv-mcp.env.example # Env var reference
tests/            # pytest suite (unit + live endpoint tests)
Makefile          # install / service / start / stop / restart / status / test
```

## Development

```sh
uv pip install -e '.[test]'
make test                        # unit tests
BIORXIV_MCP_ENDPOINT=https://biorxiv-mcp.yourdomain.com \
BIORXIV_MCP_ENDPOINT_KEY=<token> \
make test-endpoint               # live tests against a deployed server
```

## License

MIT — see [LICENSE](LICENSE).
