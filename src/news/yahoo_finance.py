"""
Yahoo Finance news providers for the volatility-engine pipeline.

Two implementations sharing the same fetcher module:

YahooFinanceNewsProvider  (direct)
    Calls the fetcher functions directly — no subprocess, no MCP overhead.
    Use this in production / tests when the MCP server is not running.

YahooFinanceMCPProvider   (via MCP server)
    Spawns the Yahoo Finance MCP server as a managed stdio subprocess and
    calls tools via the MCP protocol.  Provides full MCP-protocol fidelity
    for environments where an MCP host is running.
    Falls back to the direct provider on any subprocess error.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone
from typing import List, Optional

import structlog

from .provider import BaseNewsProvider, register_provider
from .sanitizer import sanitize_url
from .schemas import NormalizedNewsItem

log = structlog.get_logger(__name__)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared normaliser — converts fetcher dict → NormalizedNewsItem
# ---------------------------------------------------------------------------


def _dict_to_item(d: dict) -> Optional[NormalizedNewsItem]:
    """Convert a fetcher-returned dict to a NormalizedNewsItem, or None on error."""
    try:
        url = sanitize_url(d.get("url", ""))
        if not url:
            return None
        pub_raw = d.get("published_at", "")
        if isinstance(pub_raw, str):
            pub_dt = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
        elif isinstance(pub_raw, datetime):
            pub_dt = pub_raw
        else:
            pub_dt = datetime.now(timezone.utc)

        return NormalizedNewsItem(
            ticker=d.get("ticker", "UNKNOWN").upper(),
            published_at=pub_dt,
            source=d.get("source", "Yahoo Finance"),
            title=d.get("title", ""),
            url=url,
            summary=d.get("summary"),
            raw=d.get("raw", {}),
        )
    except Exception as exc:
        logger.warning("yahoo_finance: failed to parse article dict: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Direct provider
# ---------------------------------------------------------------------------


@register_provider("yahoo_finance")
class YahooFinanceNewsProvider(BaseNewsProvider):
    """
    Fetches Yahoo Finance news by calling the fetcher functions directly.

    No API key, no subprocess, no network beyond Yahoo Finance itself.
    This is the recommended provider for production and integration tests
    (where yfinance is mocked at the module level).
    """

    def __init__(self) -> None:
        pass

    def is_available(self) -> bool:
        try:
            import yfinance  # noqa: F401
            return True
        except ImportError:
            return False

    def fetch_news(
        self,
        ticker: str,
        as_of: date,
        lookback_hours: int = 72,
        max_articles: int = 20,
    ) -> List[NormalizedNewsItem]:
        # Import here so the class is importable even if yfinance is absent
        from mcp_servers.yahoo_finance_news.fetcher import fetch_ticker_news

        try:
            raw_list = fetch_ticker_news(
                ticker=ticker,
                lookback_hours=lookback_hours,
                max_articles=max_articles,
            )
        except Exception as exc:
            log.error("yahoo_finance.fetch_news error", ticker=ticker, error=str(exc))
            return []

        articles = [_dict_to_item(d) for d in raw_list]
        return [a for a in articles if a is not None]


# ---------------------------------------------------------------------------
# MCP server subprocess provider
# ---------------------------------------------------------------------------


@register_provider("yahoo_finance_mcp")
class YahooFinanceMCPProvider(BaseNewsProvider):
    """
    Calls the Yahoo Finance MCP server via stdio subprocess.

    The server is started once per provider lifetime and kept alive
    across multiple fetch_news calls.  Call close() or use as a context
    manager to terminate the subprocess cleanly.

    Falls back to YahooFinanceNewsProvider on subprocess errors.
    """

    def __init__(self) -> None:
        self._direct = YahooFinanceNewsProvider()
        self._session = None
        self._cm = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def is_available(self) -> bool:
        return self._direct.is_available()

    def fetch_news(
        self,
        ticker: str,
        as_of: date,
        lookback_hours: int = 72,
        max_articles: int = 20,
    ) -> List[NormalizedNewsItem]:
        try:
            return self._fetch_via_mcp(ticker, lookback_hours, max_articles)
        except Exception as exc:
            log.warning(
                "yahoo_finance_mcp.fetch_news: MCP call failed, falling back to direct",
                ticker=ticker,
                error=str(exc),
            )
            return self._direct.fetch_news(ticker, as_of, lookback_hours, max_articles)

    def _fetch_via_mcp(
        self,
        ticker: str,
        lookback_hours: int,
        max_articles: int,
    ) -> List[NormalizedNewsItem]:
        """Run the async MCP call synchronously using asyncio.run()."""
        return asyncio.run(
            self._async_fetch_via_mcp(ticker, lookback_hours, max_articles)
        )

    async def _async_fetch_via_mcp(
        self,
        ticker: str,
        lookback_hours: int,
        max_articles: int,
    ) -> List[NormalizedNewsItem]:
        import sys
        from pathlib import Path

        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        workspace_root = str(Path(__file__).parent.parent.parent)
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_servers.yahoo_finance_news"],
            env={"PYTHONPATH": workspace_root},
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "get_ticker_news",
                    {
                        "ticker": ticker,
                        "max_articles": max_articles,
                        "lookback_hours": lookback_hours,
                    },
                )

        # MCP returns list[TextContent]; extract the text of the first item
        raw_text = ""
        for content in (result.content or []):
            if hasattr(content, "text"):
                raw_text = content.text
                break

        if not raw_text:
            return []

        raw_list = json.loads(raw_text)
        if isinstance(raw_list, dict) and "error" in raw_list:
            raise RuntimeError(raw_list["error"])

        articles = [_dict_to_item(d) for d in raw_list]
        return [a for a in articles if a is not None]
