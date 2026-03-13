"""
Yahoo Finance news fetcher.

Single source of truth for all yfinance data access.
Used by both the MCP server (server.py) and the direct provider
(src/news/yahoo_finance.py) so there is zero duplication.

All external data passes through the sanitizer before being returned.
yfinance is never called in unit tests — callers mock `_yf_get_news`
and `_yf_search_news` at the module level.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

# Lazy import so the module can be imported without yfinance installed
# (useful in environments where only the sanitizer / schema is needed).
try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False

from src.news.sanitizer import sanitize_text, sanitize_title, sanitize_url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Article types to keep (skip VIDEO, LIVECOVERAGE, etc. unless configured)
KEPT_TYPES: frozenset[str] = frozenset({"STORY", "ARTICLE", "RESEARCHREPORT"})

# Major market proxies for get_market_news
_MARKET_TICKERS = ("SPY", "QQQ", "DIA", "^VIX", "^TNX")

# max number of items yfinance will return per call
_YF_MAX_COUNT = 100


# ---------------------------------------------------------------------------
# Thin yfinance wrappers (overridden in tests via monkeypatch)
# ---------------------------------------------------------------------------


def _yf_get_news(ticker: str, count: int) -> list[dict]:
    """
    Call yfinance to fetch news for a ticker.
    Isolated into its own function so unit tests can monkeypatch it
    without spawning real network calls.
    """
    if not _YF_AVAILABLE:
        raise RuntimeError("yfinance is not installed")
    t = yf.Ticker(ticker)
    return t.get_news(count=min(count, _YF_MAX_COUNT)) or []


def _yf_search_news(query: str, count: int) -> list[dict]:
    """
    Search Yahoo Finance for news matching a free-text query.
    yfinance >= 0.2.50 exposes `yf.Search(query).news`.
    Falls back to empty list on older versions.
    """
    if not _YF_AVAILABLE:
        raise RuntimeError("yfinance is not installed")
    try:
        results = yf.Search(query, news_count=min(count, _YF_MAX_COUNT)).news
        return results or []
    except AttributeError:
        logger.warning("yf.Search not available in this yfinance version")
        return []


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _parse_publish_time(raw: dict) -> datetime:
    """
    Parse the publish timestamp from a yfinance news item.

    yfinance items use either:
      providerPublishTime: int  (Unix epoch seconds)
      displayTime:         str  (ISO-like string)
    """
    ts = raw.get("providerPublishTime") or raw.get("time")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _extract_url(raw: dict) -> Optional[str]:
    """
    Extract and sanitize the canonical article URL.
    yfinance items may use 'link', 'url', or nested 'canonicalUrl'.
    """
    for key in ("link", "url"):
        url = raw.get(key, "")
        if url:
            return sanitize_url(url)

    # Some items nest the URL
    canonical = raw.get("canonicalUrl") or {}
    if isinstance(canonical, dict):
        url = canonical.get("url", "")
        if url:
            return sanitize_url(url)

    return None


def _extract_source(raw: dict) -> str:
    """Extract the publisher / source name from a yfinance item."""
    source = (
        raw.get("publisher")
        or raw.get("source")
        or (raw.get("providerData") or {}).get("publisher", "")
        or "Yahoo Finance"
    )
    return sanitize_text(str(source), max_len=100)


def _extract_summary(raw: dict) -> Optional[str]:
    """Extract an optional article summary / snippet."""
    for key in ("summary", "description", "snippet"):
        text = raw.get(key, "")
        if text:
            return sanitize_text(str(text), max_len=1000)
    return None


def _normalize_item(raw: dict, ticker: str) -> Optional[dict]:
    """
    Convert one raw yfinance news dict into a NormalizedNewsItem-compatible dict.

    Returns None if the item lacks a usable URL or title.
    All string fields are passed through the sanitizer.
    """
    url = _extract_url(raw)
    if not url:
        return None

    title = sanitize_title(raw.get("title", ""))
    if not title:
        return None

    # Skip non-article content types (videos, live coverage)
    item_type = (raw.get("type") or "STORY").upper()
    if item_type not in KEPT_TYPES:
        logger.debug("Skipping item type %s: %s", item_type, title[:60])
        return None

    pub_dt = _parse_publish_time(raw)

    return {
        "ticker": ticker.upper(),
        "published_at": pub_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": _extract_source(raw),
        "title": title,
        "url": url,
        "summary": _extract_summary(raw),
        "raw": {
            "uuid": raw.get("uuid", ""),
            "type": item_type,
            "related_tickers": raw.get("relatedTickers", []),
        },
    }


# ---------------------------------------------------------------------------
# Public fetch functions
# ---------------------------------------------------------------------------


def fetch_ticker_news(
    ticker: str,
    lookback_hours: int = 72,
    max_articles: int = 20,
) -> list[dict]:
    """
    Fetch and normalize recent news for a stock ticker from Yahoo Finance.

    Args:
        ticker: Stock symbol (e.g. "AAPL").
        lookback_hours: Discard articles older than this many hours.
        max_articles: Maximum number of articles to return.

    Returns:
        List of normalized article dicts (NormalizedNewsItem-compatible),
        sorted newest-first.  Empty list on error.
    """
    ticker = ticker.upper().strip()
    if not ticker or len(ticker) > 10:
        raise ValueError(f"Invalid ticker: {ticker!r}")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    try:
        raw_items = _yf_get_news(ticker, count=max_articles * 2)
    except Exception as exc:
        logger.error("yfinance fetch_ticker_news error for %s: %s", ticker, exc)
        return []

    articles = []
    for raw in raw_items:
        item = _normalize_item(raw, ticker)
        if item is None:
            continue
        pub_dt = datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
        if pub_dt < cutoff:
            continue
        articles.append(item)
        if len(articles) >= max_articles:
            break

    articles.sort(key=lambda a: a["published_at"], reverse=True)
    logger.info("fetch_ticker_news: %s → %d articles", ticker, len(articles))
    return articles


def fetch_market_news(max_articles: int = 20) -> list[dict]:
    """
    Fetch general market news by aggregating news from major index proxies.

    Uses SPY, QQQ, DIA, ^VIX, ^TNX as market proxies and deduplicates by URL.

    Args:
        max_articles: Total articles to return across all proxies.

    Returns:
        Deduplicated list of normalized article dicts, sorted newest-first.
    """
    seen_urls: set[str] = set()
    articles: list[dict] = []
    per_ticker = max(3, max_articles // len(_MARKET_TICKERS) + 1)

    for proxy in _MARKET_TICKERS:
        if len(articles) >= max_articles:
            break
        try:
            raw_items = _yf_get_news(proxy, count=per_ticker * 2)
        except Exception as exc:
            logger.warning("market_news: %s error: %s", proxy, exc)
            continue

        for raw in raw_items:
            item = _normalize_item(raw, "MARKET")
            if item is None or item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            articles.append(item)
            if len(articles) >= max_articles:
                break

    articles.sort(key=lambda a: a["published_at"], reverse=True)
    logger.info("fetch_market_news: %d articles", len(articles))
    return articles


def fetch_search_news(query: str, max_articles: int = 10) -> list[dict]:
    """
    Search Yahoo Finance news by free-text query.

    Args:
        query: Search query string (e.g. "Fed rate cut 2026").
        max_articles: Maximum number of articles to return.

    Returns:
        List of normalized article dicts, sorted newest-first.
    """
    query = query.strip()
    if not query or len(query) > 300:
        raise ValueError("Query must be 1-300 characters")

    try:
        raw_items = _yf_search_news(query, count=max_articles * 2)
    except Exception as exc:
        logger.error("yfinance search_news error for %r: %s", query, exc)
        return []

    articles = []
    seen_urls: set[str] = set()
    for raw in raw_items:
        item = _normalize_item(raw, ticker="SEARCH")
        if item is None or item["url"] in seen_urls:
            continue
        seen_urls.add(item["url"])
        articles.append(item)
        if len(articles) >= max_articles:
            break

    articles.sort(key=lambda a: a["published_at"], reverse=True)
    logger.info("fetch_search_news: %r → %d articles", query, len(articles))
    return articles
