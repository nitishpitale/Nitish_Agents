"""
Yahoo Finance News MCP Server (FastMCP).

Exposes three tools:

  get_ticker_news(ticker, max_articles, lookback_hours)
      → JSON array of NormalizedNewsItem-compatible dicts for a stock.

  get_market_news(max_articles)
      → aggregated market-wide news from major index proxies.

  search_news(query, max_articles)
      → search Yahoo Finance by free-text query.

Transport:
  stdio  (default) — for MCP clients that launch the server as a subprocess.
  streamable-HTTP  — pass --http to serve over HTTP instead.

All returned articles are sanitized through the shared sanitizer before
being sent to the client.  No credentials required — Yahoo Finance data
is publicly accessible via the yfinance library.

Example MCP client config (Claude Desktop / Cursor):
  {
    "mcpServers": {
      "yahoo-finance-news": {
        "command": "python3",
        "args": ["-m", "mcp_servers.yahoo_finance_news"],
        "cwd": "<path-to-workspace>"
      }
    }
  }
"""

import json
import logging
import re

from mcp.server.fastmcp import FastMCP

from .fetcher import fetch_market_news, fetch_search_news, fetch_ticker_news

# ---------------------------------------------------------------------------
# Server definition
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="yahoo-finance-news",
    instructions=(
        "Provides recent financial news from Yahoo Finance. "
        "Use get_ticker_news for company-specific news, "
        "get_market_news for broad market conditions, "
        "and search_news for topic-based queries. "
        "All articles are sanitized — no HTML, no scripts."
    ),
)

logger = logging.getLogger(__name__)

# Ticker validation: 1-10 alphanumeric chars + '.', '-', '^'
_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-\^]{1,10}$")


# ---------------------------------------------------------------------------
# Tool: get_ticker_news
# ---------------------------------------------------------------------------


@mcp.tool()
def get_ticker_news(
    ticker: str,
    max_articles: int = 20,
    lookback_hours: int = 72,
) -> str:
    """
    Fetch recent financial news articles for a specific stock ticker from Yahoo Finance.

    Returns a JSON array of article objects, sorted newest-first.
    Each object contains: ticker, published_at (ISO-8601 UTC), source,
    title, url, summary (optional), and raw metadata.

    Only STORY / ARTICLE / RESEARCHREPORT content types are included;
    videos and live-coverage items are filtered out.

    Args:
        ticker: Stock ticker symbol (e.g. 'AAPL', 'MSFT', 'TSLA', '^VIX').
                1-10 characters, alphanumeric plus '.', '-', '^'.
        max_articles: Maximum number of articles to return. Range: 1-50. Default: 20.
        lookback_hours: Return only articles published within this many hours. Range: 1-720. Default: 72.
    """
    # Input validation (guard-rail within the tool body)
    ticker = ticker.strip().upper()
    if not _TICKER_RE.match(ticker):
        return json.dumps({"error": f"Invalid ticker {ticker!r}. Must be 1-10 alphanumeric chars."})
    max_articles = max(1, min(int(max_articles), 50))
    lookback_hours = max(1, min(int(lookback_hours), 720))

    try:
        articles = fetch_ticker_news(
            ticker=ticker,
            lookback_hours=lookback_hours,
            max_articles=max_articles,
        )
        logger.info("get_ticker_news: %s → %d articles", ticker, len(articles))
        return json.dumps(articles, ensure_ascii=False, default=str)
    except ValueError as exc:
        return json.dumps({"error": f"Invalid input: {exc}"})
    except Exception as exc:
        logger.error("get_ticker_news error: %s", exc, exc_info=True)
        return json.dumps({"error": f"Failed to fetch news for {ticker!r}: {exc}"})


# ---------------------------------------------------------------------------
# Tool: get_market_news
# ---------------------------------------------------------------------------


@mcp.tool()
def get_market_news(max_articles: int = 20) -> str:
    """
    Fetch broad market news aggregated from major index proxies
    (SPY, QQQ, DIA, VIX, 10-yr Treasury).

    Deduplicates across proxies and returns the most recent unique articles.
    Useful for understanding macro conditions that may affect option pricing.

    Returns a JSON array sorted newest-first.

    Args:
        max_articles: Maximum number of articles to return. Range: 1-50. Default: 20.
    """
    max_articles = max(1, min(int(max_articles), 50))
    try:
        articles = fetch_market_news(max_articles=max_articles)
        logger.info("get_market_news: %d articles", len(articles))
        return json.dumps(articles, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.error("get_market_news error: %s", exc, exc_info=True)
        return json.dumps({"error": f"Failed to fetch market news: {exc}"})


# ---------------------------------------------------------------------------
# Tool: search_news
# ---------------------------------------------------------------------------


@mcp.tool()
def search_news(query: str, max_articles: int = 10) -> str:
    """
    Search Yahoo Finance for news matching a free-text query.

    Useful for topic-based research (macro themes, sector events, etc.)
    rather than company-specific news.  Requires yfinance >= 0.2.50
    for the yf.Search API; returns an empty list on older versions.

    Returns a JSON array sorted newest-first.

    Args:
        query: Free-text search query (e.g. 'Fed rate cut', 'AAPL earnings beat').
               Must be 2-300 characters.
        max_articles: Maximum number of articles to return. Range: 1-30. Default: 10.
    """
    query = query.strip()
    if not query or len(query) < 2:
        return json.dumps({"error": "Query must be at least 2 characters."})
    if len(query) > 300:
        return json.dumps({"error": "Query must be at most 300 characters."})
    max_articles = max(1, min(int(max_articles), 30))

    try:
        articles = fetch_search_news(query=query, max_articles=max_articles)
        logger.info("search_news: %r → %d articles", query, len(articles))
        return json.dumps(articles, ensure_ascii=False, default=str)
    except ValueError as exc:
        return json.dumps({"error": f"Invalid input: {exc}"})
    except Exception as exc:
        logger.error("search_news error: %s", exc, exc_info=True)
        return json.dumps({"error": f"Search failed for {query!r}: {exc}"})
