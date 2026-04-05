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
make              # create venv and install package in editable mode
make install-service   # install systemd user units (server + daily sync timer)
make install           # register the HTTP endpoint with Claude Code,
                       # Claude Desktop, and OpenCode
```

The server runs as a systemd user service at `http://localhost:8000/mcp`
with a `/health` endpoint. The sync timer runs a delta sync daily at 04:00.

First run performs a bulk sync of the entire catalog (several hours).
Subsequent syncs are fast deltas. You can trigger a sync manually:

```sh
.venv/bin/biorxiv-mcp-sync
```

## Configuration

All settings are env vars, defaulted in `deploy/biorxiv-mcp.env.example`.
Copy it to `deploy/biorxiv-mcp.env` and edit — the systemd unit loads it
automatically.

| Env var | Default | Purpose |
|---|---|---|
| `HOST` | `0.0.0.0` | HTTP bind address |
| `PORT` | `8000` | HTTP port |
| `TRANSPORT` | `http` | `http` (streamable HTTP) or `stdio` |
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `BIORXIV_MCP_DATA` | `~/.local/share/biorxiv-mcp` | DB + PDF directory |

## Project layout

```
biorxiv_mcp/
  server.py       # MCP tool handlers + FastMCP + Starlette app
  sync.py         # bioRxiv API client: bulk, delta, auto, resolve, pdf_url
  db.py           # SQLite schema, FTS5 index, connection management
  ratelimit.py    # Token bucket rate limiter
  toolkit.py      # Shared tool decorator (rate limit, errors, envelope)
  sync_runner.py  # Standalone CLI sync entry point
deploy/
  biorxiv-mcp.service.in  # Templated systemd unit
  biorxiv-sync.service.in # Sync oneshot unit
  biorxiv-sync.timer      # Daily schedule
  install_mcp.py          # Register with agent tools
  biorxiv-mcp.env.example # Env var reference
tests/            # pytest suite (50 tests)
Makefile          # install / service / start / stop / restart / status
```

## Development

```sh
uv pip install -e '.[test]'
uv run pytest -q
```

## License

MIT — see [LICENSE](LICENSE).
