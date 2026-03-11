"""
Concrete news provider implementations.

Providers:
  FMPNewsProvider          — Financial Modeling Prep REST API (FMP MCP fallback)
  FinancialDatasetsProvider — Financial Datasets MCP (stub, not yet wired)
  FinnhubNewsProvider      — Finnhub MCP (stub, not yet wired)
  MockNewsProvider         — Deterministic synthetic news for tests

FMP is the recommended primary provider.  The MCP transport layer
(stdio / HTTP SSE) would be wired here when an MCP host is running;
currently we use FMP's REST API directly, which exposes the same data
as the official MCP server tools (get_stock_news, get_company_news).

To switch from REST to full MCP transport, replace the httpx call in
FMPNewsProvider.fetch_news with an MCP tool call using the tool names
declared in provider.MCP_SERVER_REGISTRY["fmp"]["tools"].
"""

from __future__ import annotations

import logging
import random
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import httpx

from .provider import BaseNewsProvider, register_provider
from .sanitizer import sanitize_article_dict, sanitize_title, sanitize_url
from .schemas import NormalizedNewsItem

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FMP (Financial Modeling Prep)
# ---------------------------------------------------------------------------

FMP_NEWS_URL = "https://financialmodelingprep.com/api/v3/stock_news"


@register_provider("fmp")
class FMPNewsProvider(BaseNewsProvider):
    """
    Financial Modeling Prep news provider.

    MCP server: mcp-server-financialmodelingprep
    REST fallback: GET /api/v3/stock_news

    Requires FMP_API_KEY environment variable or api_key constructor arg.
    Falls back to empty list (not mock) if no key is set — callers should
    use MockNewsProvider in development/tests.
    """

    def __init__(self, api_key: str = "", timeout: int = 15) -> None:
        self._api_key = api_key
        self._http = httpx.Client(timeout=timeout)

    def is_available(self) -> bool:
        return bool(self._api_key)

    def fetch_news(
        self,
        ticker: str,
        as_of: date,
        lookback_hours: int = 72,
        max_articles: int = 20,
    ) -> List[NormalizedNewsItem]:
        if not self._api_key:
            logger.warning("FMP API key not set — returning no news for %s", ticker)
            return []

        from_dt = datetime.combine(as_of, datetime.min.time(), tzinfo=timezone.utc) - timedelta(hours=lookback_hours)

        try:
            resp = self._http.get(
                FMP_NEWS_URL,
                params={
                    "tickers": ticker.upper(),
                    "limit": max_articles,
                    "apikey": self._api_key,
                },
            )
            resp.raise_for_status()
            raw_items = resp.json()
            if not isinstance(raw_items, list):
                logger.warning("FMP returned unexpected type: %s", type(raw_items))
                return []

            articles: List[NormalizedNewsItem] = []
            for item in raw_items[:max_articles]:
                # Sanitise all fields from untrusted provider response
                safe = sanitize_article_dict(item, provider="fmp")

                url = sanitize_url(safe.get("url", ""))
                if not url:
                    continue

                pub_str = safe.get("publishedDate") or safe.get("published_at", "")
                try:
                    pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                except Exception:
                    pub_dt = datetime.now(timezone.utc)

                if pub_dt < from_dt:
                    continue

                articles.append(
                    NormalizedNewsItem(
                        ticker=ticker.upper(),
                        published_at=pub_dt,
                        source=safe.get("site", safe.get("source", "FMP")),
                        title=sanitize_title(safe.get("title", "")),
                        url=url,
                        summary=safe.get("text", None),
                        raw=safe,
                    )
                )

            articles.sort(key=lambda a: a.published_at, reverse=True)
            logger.info("FMP: fetched %d articles for %s", len(articles), ticker)
            return articles

        except Exception as exc:
            logger.error("FMP fetch error for %s: %s", ticker, exc)
            return []

    def close(self) -> None:
        self._http.close()


# ---------------------------------------------------------------------------
# Financial Datasets (stub — interface ready, not yet wired to live MCP)
# ---------------------------------------------------------------------------


@register_provider("financial_datasets")
class FinancialDatasetsProvider(BaseNewsProvider):
    """
    Financial Datasets MCP stub.

    MCP server: financial-datasets-mcp
    REST fallback: https://api.financialdatasets.ai/news

    Wire up by replacing the stub body with an httpx call to the
    REST endpoint or an MCP tool call using tools["get_market_news"].
    """

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key

    def is_available(self) -> bool:
        return bool(self._api_key)

    def fetch_news(
        self,
        ticker: str,
        as_of: date,
        lookback_hours: int = 72,
        max_articles: int = 20,
    ) -> List[NormalizedNewsItem]:
        # Not yet implemented — return empty
        logger.info("FinancialDatasetsProvider: not yet implemented, returning []")
        return []


# ---------------------------------------------------------------------------
# Finnhub (stub — interface ready, not yet wired to live MCP)
# ---------------------------------------------------------------------------


@register_provider("finnhub")
class FinnhubNewsProvider(BaseNewsProvider):
    """
    Finnhub-backed finance news MCP stub.

    MCP server: finance-news-mcp
    REST fallback: https://finnhub.io/api/v1/company-news

    Wire up by replacing the stub body with an httpx call or MCP tool call
    using tools["search_financial_news"].
    """

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key

    def is_available(self) -> bool:
        return bool(self._api_key)

    def fetch_news(
        self,
        ticker: str,
        as_of: date,
        lookback_hours: int = 72,
        max_articles: int = 20,
    ) -> List[NormalizedNewsItem]:
        logger.info("FinnhubNewsProvider: not yet implemented, returning []")
        return []


# ---------------------------------------------------------------------------
# Mock provider — deterministic synthetic articles for tests
# ---------------------------------------------------------------------------

_MOCK_ARTICLES: dict[str, list[dict]] = {
    "AAPL": [
        {
            "title": "Apple Q2 earnings beat expectations on strong iPhone sales",
            "source": "Reuters",
            "url": "https://reuters.com/aapl-q2-earnings",
            "summary": "Apple reported Q2 earnings that exceeded analyst estimates, driven by iPhone sales growth in emerging markets. Services revenue also hit a record.",
            "sentiment_hint": "positive",
            "catalyst_hint": "earnings",
        },
        {
            "title": "Apple faces EU regulatory probe over App Store practices",
            "source": "WSJ",
            "url": "https://wsj.com/aapl-eu-probe",
            "summary": "European regulators have opened a formal inquiry into Apple's App Store fee structure under the Digital Markets Act.",
            "sentiment_hint": "negative",
            "catalyst_hint": "regulatory",
        },
        {
            "title": "Analyst upgrades Apple to Buy, raises price target",
            "source": "Bloomberg",
            "url": "https://bloomberg.com/aapl-upgrade",
            "summary": "A major investment bank raised its AAPL price target citing AI integration momentum.",
            "sentiment_hint": "positive",
            "catalyst_hint": "analyst",
        },
    ],
    "MSFT": [
        {
            "title": "Microsoft Azure cloud revenue surges 35% YoY on AI demand",
            "source": "CNBC",
            "url": "https://cnbc.com/msft-azure-growth",
            "summary": "Microsoft's Azure segment delivered 35% growth year-over-year, beating estimates as enterprise AI workload adoption accelerates.",
            "sentiment_hint": "positive",
            "catalyst_hint": "guidance",
        },
        {
            "title": "Microsoft earnings call scheduled — analysts expect strong guidance",
            "source": "MarketWatch",
            "url": "https://marketwatch.com/msft-earnings-preview",
            "summary": "Microsoft will report quarterly results next week. Analysts are broadly bullish on AI-driven revenue.",
            "sentiment_hint": "positive",
            "catalyst_hint": "earnings",
        },
    ],
    "TSLA": [
        {
            "title": "Tesla recalls vehicles over software defect in autopilot system",
            "source": "Reuters",
            "url": "https://reuters.com/tsla-recall",
            "summary": "Tesla issued a voluntary recall affecting over 200,000 vehicles due to a firmware bug in the autopilot engagement system.",
            "sentiment_hint": "negative",
            "catalyst_hint": "legal",
        },
        {
            "title": "Tesla Q1 deliveries miss forecasts; stock tumbles pre-market",
            "source": "Bloomberg",
            "url": "https://bloomberg.com/tsla-deliveries-miss",
            "summary": "Tesla delivered fewer vehicles than expected in Q1, raising concerns about demand weakness amid intensifying EV competition.",
            "sentiment_hint": "negative",
            "catalyst_hint": "guidance",
        },
    ],
    "NVDA": [
        {
            "title": "Nvidia Blackwell GPU demand exceeds supply — analysts raise targets",
            "source": "Barron's",
            "url": "https://barrons.com/nvda-blackwell-demand",
            "summary": "Multiple analysts raised their NVDA price targets after Nvidia indicated Blackwell architecture GPUs are sold out through year-end.",
            "sentiment_hint": "positive",
            "catalyst_hint": "analyst",
        },
        {
            "title": "US export restrictions may limit Nvidia chip sales to China",
            "source": "WSJ",
            "url": "https://wsj.com/nvda-export-restrictions",
            "summary": "The Biden administration is considering additional restrictions on AI chip exports, which could impact Nvidia's H100 sales in China.",
            "sentiment_hint": "negative",
            "catalyst_hint": "regulatory",
        },
    ],
    "SPY": [
        {
            "title": "Fed signals potential rate cuts in 2026 amid cooling inflation",
            "source": "FT",
            "url": "https://ft.com/fed-rate-cuts-2026",
            "summary": "Federal Reserve officials indicated they see room for rate reductions if inflation continues declining toward the 2% target.",
            "sentiment_hint": "positive",
            "catalyst_hint": "macro",
        },
    ],
    "META": [
        {
            "title": "Meta AI assistant reaches 1 billion users milestone",
            "source": "TechCrunch",
            "url": "https://techcrunch.com/meta-ai-1b",
            "summary": "Meta announced that its AI assistant has surpassed 1 billion monthly active users across WhatsApp, Instagram, and Facebook.",
            "sentiment_hint": "positive",
            "catalyst_hint": "product",
        },
    ],
    "QQQ": [
        {
            "title": "Tech sector faces macro headwinds from rising yields",
            "source": "Reuters",
            "url": "https://reuters.com/tech-macro-yields",
            "summary": "Rising long-term Treasury yields are creating multiple compression pressure on high-P/E technology stocks.",
            "sentiment_hint": "negative",
            "catalyst_hint": "macro",
        },
    ],
    "GOOGL": [
        {
            "title": "Google Search market share holds steady despite AI competition",
            "source": "Bloomberg",
            "url": "https://bloomberg.com/googl-search-share",
            "summary": "Despite predictions of AI-driven disruption, Google's search market share remains above 90%, defying analyst concerns.",
            "sentiment_hint": "positive",
            "catalyst_hint": "product",
        },
    ],
    "AMZN": [
        {
            "title": "Amazon AWS unveils new AI inference chips at re:Invent",
            "source": "CNBC",
            "url": "https://cnbc.com/amazon-aws-ai-chips",
            "summary": "Amazon unveiled Trainium3 AI training chips and Inferentia3 for inference, targeting cost reduction in AI workloads.",
            "sentiment_hint": "positive",
            "catalyst_hint": "product",
        },
    ],
    "IWM": [
        {
            "title": "Small-cap stocks lag as credit spreads widen on recession fears",
            "source": "FT",
            "url": "https://ft.com/small-cap-credit",
            "summary": "Small-cap equities are underperforming large-caps as widening credit spreads signal investor concern about the economic cycle.",
            "sentiment_hint": "negative",
            "catalyst_hint": "macro",
        },
    ],
}

_MOCK_EARNINGS_IMMINENT_TICKERS = {"AAPL", "MSFT", "TSLA", "GOOGL", "META"}


@register_provider("mock")
class MockNewsProvider(BaseNewsProvider):
    """
    Deterministic mock news provider for tests and offline development.

    Uses a fixed seed and pre-defined article templates to produce
    reproducible NormalizedNewsItem lists for any configured ticker.
    """

    def __init__(
        self,
        seed: int = 42,
        reference_date: Optional[date] = None,
    ) -> None:
        self._seed = seed
        self._ref_date = reference_date or date(2026, 3, 11)

    def fetch_news(
        self,
        ticker: str,
        as_of: date,
        lookback_hours: int = 72,
        max_articles: int = 20,
    ) -> List[NormalizedNewsItem]:
        template_articles = _MOCK_ARTICLES.get(ticker.upper(), [])
        if not template_articles:
            return []

        rng = random.Random(self._seed + hash(ticker) % 10000)
        cutoff = datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc) - timedelta(hours=lookback_hours)

        articles: List[NormalizedNewsItem] = []
        for i, tmpl in enumerate(template_articles[:max_articles]):
            hours_ago = rng.randint(1, lookback_hours - 1)
            pub_at = datetime.combine(as_of, datetime.min.time(), tzinfo=timezone.utc) - timedelta(hours=hours_ago)

            if pub_at < cutoff:
                continue

            articles.append(
                NormalizedNewsItem(
                    ticker=ticker.upper(),
                    published_at=pub_at,
                    source=tmpl["source"],
                    title=tmpl["title"],
                    url=tmpl["url"],
                    summary=tmpl.get("summary"),
                    raw={
                        "sentiment_hint": tmpl.get("sentiment_hint", "neutral"),
                        "catalyst_hint": tmpl.get("catalyst_hint", "other"),
                    },
                )
            )

        articles.sort(key=lambda a: a.published_at, reverse=True)

        # Add an earnings_imminent article for relevant tickers
        if ticker.upper() in _MOCK_EARNINGS_IMMINENT_TICKERS:
            earn_article = NormalizedNewsItem(
                ticker=ticker.upper(),
                published_at=datetime.combine(as_of, datetime.min.time(), tzinfo=timezone.utc) - timedelta(hours=2),
                source="Investor Relations",
                title=f"{ticker} earnings report scheduled in coming weeks",
                url=f"https://ir.{ticker.lower()}.com/earnings-date",
                summary=f"{ticker} has confirmed its upcoming earnings release date.",
                raw={"catalyst_hint": "earnings", "sentiment_hint": "unclear"},
            )
            articles.insert(0, earn_article)

        return articles[:max_articles]
