# Yahoo Finance News MCP Server

A Model Context Protocol (MCP) server that exposes financial news from
Yahoo Finance as structured, sanitized tools.

---

## Tools

| Tool | Description |
|---|---|
| `get_ticker_news` | Recent news for a specific stock ticker |
| `get_market_news` | Broad market news (SPY, QQQ, DIA, VIX, TNX) |
| `search_news` | Free-text search across Yahoo Finance news |

### `get_ticker_news`

```
get_ticker_news(ticker: str, max_articles: int = 20, lookback_hours: int = 72) -> str
```

Returns a JSON array of articles for a stock, newest-first.

**Parameters:**
- `ticker` — Stock symbol, e.g. `"AAPL"` (1–10 chars, alphanumeric + `.`, `-`, `^`)
- `max_articles` — 1–50, default 20
- `lookback_hours` — 1–720, default 72 (3 days)

**Example output (one article):**
```json
[
  {
    "ticker": "AAPL",
    "published_at": "2026-03-11T14:22:00Z",
    "source": "Reuters",
    "title": "Apple reports record Q2 on iPhone sales surge",
    "url": "https://finance.yahoo.com/news/apple-q2-record...",
    "summary": "Apple Inc. reported its strongest quarter...",
    "raw": {
      "uuid": "abc123",
      "type": "STORY",
      "related_tickers": ["AAPL", "MSFT"]
    }
  }
]
```

### `get_market_news`

```
get_market_news(max_articles: int = 20) -> str
```

Aggregates news from SPY, QQQ, DIA, ^VIX, ^TNX and deduplicates.
Useful for macro context before option scanning.

### `search_news`

```
search_news(query: str, max_articles: int = 10) -> str
```

Free-text search, e.g. `"Fed rate cut"`, `"semiconductor AI chips"`.
Requires `yfinance >= 0.2.50` (uses `yf.Search`).

---

## Running the server

### Prerequisites

```bash
pip install "mcp[cli]>=1.3.0" yfinance>=0.2.50
```

No API key required — Yahoo Finance data is publicly accessible.

### Stdio mode (for MCP clients)

```bash
# From the workspace root:
python3 -m mcp_servers.yahoo_finance_news
```

### HTTP mode (streamable-HTTP)

```bash
python3 -m mcp_servers.yahoo_finance_news --http
# Optionally: --host 0.0.0.0 --port 9000
```

---

## Connecting from an MCP client

### Claude Desktop / Cursor (`mcp.json`)

```json
{
  "mcpServers": {
    "yahoo-finance-news": {
      "command": "python3",
      "args": ["-m", "mcp_servers.yahoo_finance_news"],
      "cwd": "/path/to/workspace"
    }
  }
}
```

### Cursor Secrets (optional env vars)

The server requires no secrets.  The following are available for tuning:

| Variable | Default | Description |
|---|---|---|
| `YFN_LOG_LEVEL` | `INFO` | Logging verbosity |
| `YFN_MAX_ARTICLES` | `50` | Hard cap on any tool's max_articles |

---

## Integrating with the volatility-engine pipeline

The engine's news pipeline supports `yahoo_finance` as a provider:

```yaml
# config.yaml
news:
  provider: "yahoo_finance"
```

Or set the environment variable:

```bash
export NEWS_PROVIDER=yahoo_finance
```

The `YahooFinanceNewsProvider` class in `src/news/yahoo_finance.py` calls
the fetcher directly (no subprocess overhead).  `YahooFinanceMCPProvider`
calls this MCP server via a managed stdio subprocess for full MCP-protocol
fidelity.

---

## Security

- All URLs from Yahoo Finance are validated (must be `https://`).
- All text fields are stripped of HTML tags and script blocks.
- Prompt-injection patterns in article text are detected and removed.
- The server appears in `MCP_SERVER_ALLOWLIST` under name
  `"yahoo-finance-news"` at version `">=1.0.0"`.
- Only `STORY`, `ARTICLE`, and `RESEARCHREPORT` content types are
  returned; videos and live-coverage items are filtered.

---

## Architecture

```
mcp_servers/yahoo_finance_news/
├── __init__.py
├── __main__.py      Entry point (stdio / HTTP)
├── server.py        FastMCP tool definitions
├── fetcher.py       yfinance calls + normalization + sanitization
└── README.md        This file

src/news/
├── yahoo_finance.py YahooFinanceNewsProvider (direct) +
│                    YahooFinanceMCPProvider  (via MCP subprocess)
└── ...

tests/
├── unit/
│   ├── test_yahoo_finance_fetcher.py
│   └── test_yahoo_mcp_server.py
└── integration/
    └── test_yahoo_finance_provider.py
```
