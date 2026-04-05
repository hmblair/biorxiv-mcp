# biorxiv-mcp

Search [bioRxiv](https://www.biorxiv.org) and
[medRxiv](https://www.medrxiv.org) preprints from your AI agent.

A local [MCP](https://modelcontextprotocol.io) server gives Claude
(and other MCP-compatible agents) full-text search across ~400k papers,
paper lookup by DOI, PDF downloads, and category browsing — all backed
by a remote index so queries are fast and don't hit the bioRxiv API
directly.

## Install

You need Python 3.10+, an API key, and the server URL from whoever runs
the backend.

```sh
git clone https://github.com/hmblair/biorxiv-mcp && cd biorxiv-mcp
make install \
  BIORXIV_MCP_ENDPOINT=https://biorxiv.example.com \
  BIORXIV_MCP_ENDPOINT_KEY=<your-api-key>
```

This registers the MCP with Claude Code, Claude Desktop, and OpenCode.
No server, database, or background process runs on your machine — just
a lightweight shim that forwards tool calls to the remote API.

If the server is on the same machine and doesn't require auth:

```sh
make install
```

To unregister:

```sh
make uninstall
```

## Tools

Once installed, your agent has access to these tools:

| Tool | What it does |
|---|---|
| `search_biorxiv` | Full-text search across titles, abstracts, authors, and institutions. Supports `AND`/`OR`/`NEAR`, quoted phrases, prefix matching, category and date filters, sort by relevance or date. |
| `search_biorxiv_count` | Count matches without returning results — useful for narrowing filters before searching. |
| `biorxiv_categories` | List all categories with paper counts. |
| `get_paper` | Get full metadata for a paper by DOI. Falls back to the bioRxiv API for papers not yet synced. |
| `download_paper` | Download a paper's PDF to `~/. local/share/biorxiv-mcp/papers/`. |
| `sync_biorxiv` | Trigger a database sync on the server (admin use). |
| `biorxiv_status` | Check paper count, last sync date, and sync status. |

### Example queries

- `search_biorxiv("CRISPR AND cancer", sort="date", after="2025-01-01")`
- `search_biorxiv("single-cell RNA", category="genomics", limit=20, detail=True)`
- `get_paper("10.1101/2024.01.05.574328")`

## Troubleshooting

**"Connection error" in tool output** — the shim can't reach the server.
Check that the URL is correct and reachable:

```sh
curl https://biorxiv.example.com/health
```

**"HTTP 401" or "HTTP 403"** — your API key is missing, wrong, or
revoked. Re-run `make install` with the correct key, or ask the server
admin for a new one.

**Tools not showing up in Claude** — verify registration:

```sh
claude mcp list
```

If `biorxiv-mcp` isn't listed or shows an error, re-run `make install`.

## Server administration

If you're running the backend, see [deploy/SERVER.md](deploy/SERVER.md).

## License

MIT — see [LICENSE](LICENSE).
