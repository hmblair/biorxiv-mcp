# biorxiv-mcp

Search [bioRxiv](https://www.biorxiv.org) and
[medRxiv](https://www.medrxiv.org) preprints from your AI agent.

The official bioRxiv API has no keyword search — it can only list papers
by date range or category. This project syncs the full catalog into a
local SQLite FTS5 index and exposes it as an
[MCP](https://modelcontextprotocol.io) server, giving Claude (and other
MCP-compatible agents) fast full-text search with relevance ranking,
MeSH synonym expansion, paper lookup by DOI, PDF downloads, and
category browsing.

## Install

You need Python 3.10+, an API key, and the server URL from whoever runs
the backend.

```sh
git clone https://github.com/hmblair/biorxiv-mcp && cd biorxiv-mcp
make install \
  BIORXIV_MCP_ENDPOINT=https://biorxiv.example.com \
  BIORXIV_MCP_ENDPOINT_KEY=<your-api-key>
```

This registers the MCP with Claude Code, Claude Desktop, and OpenCode,
and installs a Claude Code [agent](#agent) for literature search.
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
| `search_biorxiv` | Full-text search across titles, abstracts, authors, and institutions. PubMed-like: implicit AND, MeSH synonym expansion, quoted phrases, hyphenated terms. Filter by category (or list of categories), date range. Omit the query to browse by date/category. |
| `biorxiv_categories` | List all categories with paper counts. |
| `get_paper` | Get full metadata for a paper by DOI (title, authors, abstract, institution, license, etc.). |
| `download_paper` | Download a paper's PDF to `~/.local/share/biorxiv-mcp/papers/`. |

### Example queries

- `search_biorxiv("CRISPR cancer")` — finds papers with both (AND)
- `search_biorxiv("heart attack")` — also finds "myocardial infarction" via MeSH
- `search_biorxiv("mRNA-seq", category="genomics", after="2024-01-01")`
- `search_biorxiv("CRISPR OR cancer")` — explicit OR for either term
- `search_biorxiv(category=["bioinformatics", "biophysics"], after="2025-04-01")` — browse recent papers
- `get_paper("10.1101/2024.01.05.574328")`

## Agent

`make install` also installs a Claude Code agent (`biorxiv`) that can
search for papers relevant to a research topic. It works by browsing
recent titles across categories and using its judgment to identify
relevant papers, rather than relying solely on keyword matching.

Invoke it with `@agent-biorxiv` or let Claude delegate automatically:

```
@agent-biorxiv Find papers from the past week about RNA structure prediction
```

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

If you're running the backend, see [Server administration](https://github.com/hmblair/biorxiv-mcp/blob/main/deploy/SERVER.md).

## License

MIT — see [LICENSE](LICENSE).
