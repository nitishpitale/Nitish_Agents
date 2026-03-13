"""
Unit tests for the Yahoo Finance News fetcher.

All yfinance network calls are replaced with monkeypatching of the
module-level `_yf_get_news` and `_yf_search_news` functions, so no
real HTTP requests are made.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import mcp_servers.yahoo_finance_news.fetcher as fetcher_mod
from mcp_servers.yahoo_finance_news.fetcher import (
    fetch_market_news,
    fetch_search_news,
    fetch_ticker_news,
)

# ---------------------------------------------------------------------------
# Sample raw yfinance news item (structure mirrors real API output)
# ---------------------------------------------------------------------------

_NOW_TS = int(time.time())
_HOUR_AGO_TS = _NOW_TS - 3600
_WEEK_AGO_TS = _NOW_TS - 7 * 24 * 3600

SAMPLE_RAW_ITEMS = [
    {
        "uuid": "abc-001",
        "title": "Apple Reports Record Q2 on iPhone Sales",
        "publisher": "Reuters",
        "link": "https://finance.yahoo.com/news/apple-q2-001",
        "providerPublishTime": _HOUR_AGO_TS,
        "type": "STORY",
        "relatedTickers": ["AAPL", "MSFT"],
        "summary": "Apple Inc. reported record quarterly earnings.",
    },
    {
        "uuid": "abc-002",
        "title": "Apple Faces EU Antitrust Probe",
        "publisher": "WSJ",
        "link": "https://finance.yahoo.com/news/apple-eu-002",
        "providerPublishTime": _HOUR_AGO_TS - 1800,
        "type": "STORY",
        "relatedTickers": ["AAPL"],
        "summary": "European regulators opened an inquiry into App Store.",
    },
    {
        "uuid": "abc-003",
        "title": "Apple CEO Interview Video",
        "publisher": "CNBC",
        "link": "https://finance.yahoo.com/video/apple-ceo-003",
        "providerPublishTime": _HOUR_AGO_TS - 900,
        "type": "VIDEO",
        "relatedTickers": ["AAPL"],
    },
    {
        "uuid": "abc-004",
        "title": "Old Apple Story from Last Week",
        "publisher": "Bloomberg",
        "link": "https://finance.yahoo.com/news/apple-old-004",
        "providerPublishTime": _WEEK_AGO_TS,
        "type": "STORY",
        "relatedTickers": ["AAPL"],
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_yf_news(items=None):
    """Context manager that patches _yf_get_news to return given items."""
    return patch.object(
        fetcher_mod, "_yf_get_news", return_value=items if items is not None else SAMPLE_RAW_ITEMS
    )


def _patch_yf_search(items=None):
    return patch.object(
        fetcher_mod, "_yf_search_news", return_value=items if items is not None else SAMPLE_RAW_ITEMS[:2]
    )


# ---------------------------------------------------------------------------
# fetch_ticker_news
# ---------------------------------------------------------------------------


class TestFetchTickerNews:
    def test_returns_articles_for_valid_ticker(self):
        with _patch_yf_news():
            articles = fetch_ticker_news("AAPL")
        assert len(articles) >= 1

    def test_filters_out_video_type(self):
        with _patch_yf_news():
            articles = fetch_ticker_news("AAPL")
        types = [a["raw"].get("type", "") for a in articles]
        assert "VIDEO" not in types

    def test_filters_out_articles_older_than_lookback(self):
        with _patch_yf_news():
            articles = fetch_ticker_news("AAPL", lookback_hours=48)
        # The WEEK_AGO article should be excluded
        titles = [a["title"] for a in articles]
        assert "Old Apple Story from Last Week" not in titles

    def test_respects_max_articles(self):
        many_items = SAMPLE_RAW_ITEMS * 10  # 40 items
        with patch.object(fetcher_mod, "_yf_get_news", return_value=many_items):
            articles = fetch_ticker_news("AAPL", max_articles=3)
        assert len(articles) <= 3

    def test_sorted_newest_first(self):
        with _patch_yf_news():
            articles = fetch_ticker_news("AAPL")
        for i in range(len(articles) - 1):
            assert articles[i]["published_at"] >= articles[i + 1]["published_at"]

    def test_ticker_uppercase_in_output(self):
        with _patch_yf_news():
            articles = fetch_ticker_news("aapl")
        assert all(a["ticker"] == "AAPL" for a in articles)

    def test_url_sanitized(self):
        with _patch_yf_news():
            articles = fetch_ticker_news("AAPL")
        for a in articles:
            assert a["url"].startswith("https://"), f"Bad URL: {a['url']}"

    def test_output_schema_keys(self):
        with _patch_yf_news():
            articles = fetch_ticker_news("AAPL")
        required = {"ticker", "published_at", "source", "title", "url"}
        for a in articles:
            assert required.issubset(a.keys()), f"Missing keys in: {a.keys()}"

    def test_invalid_ticker_raises(self):
        with pytest.raises(ValueError):
            fetch_ticker_news("A" * 20)

    def test_empty_ticker_raises(self):
        with pytest.raises(ValueError):
            fetch_ticker_news("")

    def test_yfinance_error_returns_empty(self):
        with patch.object(fetcher_mod, "_yf_get_news", side_effect=Exception("network error")):
            articles = fetch_ticker_news("AAPL")
        assert articles == []

    def test_missing_url_item_skipped(self):
        bad_items = [
            {"uuid": "x1", "title": "No URL article", "publisher": "X",
             "providerPublishTime": _HOUR_AGO_TS, "type": "STORY"},
        ] + SAMPLE_RAW_ITEMS[:1]
        with patch.object(fetcher_mod, "_yf_get_news", return_value=bad_items):
            articles = fetch_ticker_news("AAPL")
        assert all("url" in a and a["url"] for a in articles)

    def test_injected_url_rejected(self):
        bad_items = [{
            "uuid": "evil",
            "title": "Legit headline",
            "publisher": "Hacker",
            "link": "javascript:alert(document.cookie)",
            "providerPublishTime": _HOUR_AGO_TS,
            "type": "STORY",
        }]
        with patch.object(fetcher_mod, "_yf_get_news", return_value=bad_items):
            articles = fetch_ticker_news("AAPL")
        assert articles == []

    def test_html_stripped_from_title(self):
        html_items = [{
            **SAMPLE_RAW_ITEMS[0],
            "title": "<b>Apple</b> Reports <script>evil()</script>Record Q2",
        }]
        with patch.object(fetcher_mod, "_yf_get_news", return_value=html_items):
            articles = fetch_ticker_news("AAPL")
        for a in articles:
            assert "<b>" not in a["title"]
            assert "<script>" not in a["title"]

    def test_no_items_returns_empty(self):
        with patch.object(fetcher_mod, "_yf_get_news", return_value=[]):
            articles = fetch_ticker_news("AAPL")
        assert articles == []

    def test_unix_timestamp_parsed_correctly(self):
        with _patch_yf_news([SAMPLE_RAW_ITEMS[0]]):
            articles = fetch_ticker_news("AAPL")
        assert len(articles) == 1
        pub = datetime.fromisoformat(articles[0]["published_at"].replace("Z", "+00:00"))
        expected = datetime.fromtimestamp(_HOUR_AGO_TS, tz=timezone.utc)
        assert abs((pub - expected).total_seconds()) < 2


# ---------------------------------------------------------------------------
# fetch_market_news
# ---------------------------------------------------------------------------


class TestFetchMarketNews:
    def test_returns_articles(self):
        with _patch_yf_news():
            articles = fetch_market_news(max_articles=5)
        assert isinstance(articles, list)

    def test_deduplicates_across_proxies(self):
        # All proxies return the same articles — only unique URLs survive
        with _patch_yf_news():
            articles = fetch_market_news(max_articles=10)
        urls = [a["url"] for a in articles]
        assert len(urls) == len(set(urls)), "Duplicate URLs in market news"

    def test_max_articles_respected(self):
        many = SAMPLE_RAW_ITEMS * 5
        with patch.object(fetcher_mod, "_yf_get_news", return_value=many):
            articles = fetch_market_news(max_articles=3)
        assert len(articles) <= 3

    def test_sorted_newest_first(self):
        with _patch_yf_news():
            articles = fetch_market_news()
        for i in range(len(articles) - 1):
            assert articles[i]["published_at"] >= articles[i + 1]["published_at"]

    def test_errors_handled_gracefully(self):
        with patch.object(fetcher_mod, "_yf_get_news", side_effect=Exception("timeout")):
            articles = fetch_market_news()
        assert articles == []


# ---------------------------------------------------------------------------
# fetch_search_news
# ---------------------------------------------------------------------------


class TestFetchSearchNews:
    def test_returns_articles_for_query(self):
        with _patch_yf_search():
            articles = fetch_search_news("Fed rate cut")
        assert isinstance(articles, list)

    def test_empty_query_raises(self):
        with pytest.raises(ValueError):
            fetch_search_news("")

    def test_too_long_query_raises(self):
        with pytest.raises(ValueError):
            fetch_search_news("x" * 301)

    def test_max_articles_respected(self):
        with _patch_yf_search(SAMPLE_RAW_ITEMS * 5):
            articles = fetch_search_news("AAPL earnings", max_articles=1)
        assert len(articles) <= 1

    def test_deduplicates_by_url(self):
        duplicate_items = SAMPLE_RAW_ITEMS[:1] * 5
        with patch.object(fetcher_mod, "_yf_search_news", return_value=duplicate_items):
            articles = fetch_search_news("test query")
        urls = [a["url"] for a in articles]
        assert len(urls) == len(set(urls))

    def test_error_returns_empty(self):
        with patch.object(fetcher_mod, "_yf_search_news", side_effect=Exception("search failed")):
            articles = fetch_search_news("apple")
        assert articles == []


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


class TestNormalizeItem:
    """Test the _normalize_item helper directly."""

    def test_standard_item(self):
        from mcp_servers.yahoo_finance_news.fetcher import _normalize_item
        result = _normalize_item(SAMPLE_RAW_ITEMS[0], "AAPL")
        assert result is not None
        assert result["ticker"] == "AAPL"
        assert result["url"].startswith("https://")
        assert result["title"]

    def test_missing_url_returns_none(self):
        from mcp_servers.yahoo_finance_news.fetcher import _normalize_item
        item = {**SAMPLE_RAW_ITEMS[0]}
        del item["link"]
        result = _normalize_item(item, "AAPL")
        assert result is None

    def test_empty_title_returns_none(self):
        from mcp_servers.yahoo_finance_news.fetcher import _normalize_item
        item = {**SAMPLE_RAW_ITEMS[0], "title": ""}
        result = _normalize_item(item, "AAPL")
        assert result is None

    def test_video_type_filtered(self):
        from mcp_servers.yahoo_finance_news.fetcher import _normalize_item
        result = _normalize_item(SAMPLE_RAW_ITEMS[2], "AAPL")  # VIDEO type
        assert result is None

    def test_source_extracted(self):
        from mcp_servers.yahoo_finance_news.fetcher import _normalize_item
        result = _normalize_item(SAMPLE_RAW_ITEMS[0], "AAPL")
        assert result["source"] == "Reuters"

    def test_publish_time_from_unix_ts(self):
        from mcp_servers.yahoo_finance_news.fetcher import _normalize_item
        result = _normalize_item(SAMPLE_RAW_ITEMS[0], "AAPL")
        assert "T" in result["published_at"]
        assert result["published_at"].endswith("Z")
