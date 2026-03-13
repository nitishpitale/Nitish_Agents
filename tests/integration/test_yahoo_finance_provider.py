"""
Integration tests for YahooFinanceNewsProvider in the pipeline.

yfinance is monkeypatched via the fetcher module — no real HTTP calls.
Tests verify the full path: provider → fetcher → normalize → NormalizedNewsItem.
Also validates the engine pipeline with yahoo_finance as the news provider.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import mcp_servers.yahoo_finance_news.fetcher as fetcher_mod
from src.config.settings import NewsConfig, Settings
from src.engine import run_engine
from src.ingest.mock_client import MockEToroClient
from src.news.schemas import NormalizedNewsItem
from src.news.yahoo_finance import YahooFinanceNewsProvider

REF_DATE = datetime(2026, 3, 11, 18, 0, 0, tzinfo=timezone.utc)

_NOW_TS = int(time.time())
_HOUR_AGO = _NOW_TS - 3600

MOCK_YF_NEWS = [
    {
        "uuid": "p1",
        "title": "AAPL Beats Estimates on Services Revenue",
        "publisher": "Reuters",
        "link": "https://finance.yahoo.com/news/aapl-services-p1",
        "providerPublishTime": _HOUR_AGO,
        "type": "STORY",
        "relatedTickers": ["AAPL"],
        "summary": "Apple's services segment posted record revenue.",
    },
    {
        "uuid": "p2",
        "title": "AAPL Faces Regulatory Scrutiny in EU",
        "publisher": "Bloomberg",
        "link": "https://finance.yahoo.com/news/aapl-eu-p2",
        "providerPublishTime": _HOUR_AGO - 2000,
        "type": "STORY",
        "relatedTickers": ["AAPL"],
    },
    {
        "uuid": "p3",
        "title": "Analyst Raises AAPL Price Target to $220",
        "publisher": "Barron's",
        "link": "https://finance.yahoo.com/news/aapl-pt-p3",
        "providerPublishTime": _HOUR_AGO - 4000,
        "type": "STORY",
        "relatedTickers": ["AAPL"],
    },
]


@pytest.fixture
def yf_provider():
    return YahooFinanceNewsProvider()


@pytest.fixture
def patched_yf():
    """Patch yfinance calls at the fetcher level."""
    with patch.object(fetcher_mod, "_yf_get_news", return_value=MOCK_YF_NEWS):
        yield


# ---------------------------------------------------------------------------
# YahooFinanceNewsProvider
# ---------------------------------------------------------------------------


class TestYahooFinanceNewsProvider:
    def test_fetch_news_returns_normalized_items(self, yf_provider, patched_yf):
        articles = yf_provider.fetch_news("AAPL", REF_DATE.date())
        assert len(articles) > 0
        for a in articles:
            assert isinstance(a, NormalizedNewsItem)

    def test_articles_have_valid_urls(self, yf_provider, patched_yf):
        articles = yf_provider.fetch_news("AAPL", REF_DATE.date())
        for a in articles:
            assert a.url.startswith("https://")

    def test_articles_sorted_newest_first(self, yf_provider, patched_yf):
        articles = yf_provider.fetch_news("AAPL", REF_DATE.date())
        for i in range(len(articles) - 1):
            assert articles[i].published_at >= articles[i + 1].published_at

    def test_max_articles_respected(self, yf_provider, patched_yf):
        articles = yf_provider.fetch_news("AAPL", REF_DATE.date(), max_articles=1)
        assert len(articles) <= 1

    def test_error_returns_empty_list(self, yf_provider):
        with patch.object(fetcher_mod, "_yf_get_news", side_effect=Exception("timeout")):
            articles = yf_provider.fetch_news("AAPL", REF_DATE.date())
        assert articles == []

    def test_source_field_populated(self, yf_provider, patched_yf):
        articles = yf_provider.fetch_news("AAPL", REF_DATE.date())
        for a in articles:
            assert a.source  # non-empty source

    def test_title_is_sanitized(self, yf_provider):
        html_items = [{**MOCK_YF_NEWS[0], "title": "<b>Apple</b> Results <script>x</script>"}]
        with patch.object(fetcher_mod, "_yf_get_news", return_value=html_items):
            articles = yf_provider.fetch_news("AAPL", REF_DATE.date())
        for a in articles:
            assert "<b>" not in a.title
            assert "<script>" not in a.title

    def test_deterministic_results(self, yf_provider, patched_yf):
        a1 = yf_provider.fetch_news("AAPL", REF_DATE.date())
        a2 = yf_provider.fetch_news("AAPL", REF_DATE.date())
        assert len(a1) == len(a2)
        for i, (item1, item2) in enumerate(zip(a1, a2)):
            assert item1.url == item2.url
            assert item1.title == item2.title

    def test_is_available(self, yf_provider):
        assert yf_provider.is_available() is True

    def test_provider_key(self, yf_provider):
        assert yf_provider.provider_key == "yahoo_finance"

    def test_provider_info_in_registry(self, yf_provider):
        info = yf_provider.provider_info()
        assert info["mcp_server_name"] == "yahoo-finance-news"
        assert "get_ticker_news" in info["tools"]


# ---------------------------------------------------------------------------
# Full engine pipeline with yahoo_finance provider
# ---------------------------------------------------------------------------


class TestEngineWithYahooFinanceProvider:
    @pytest.fixture
    def yahoo_finance_settings(self):
        s = Settings.from_yaml_and_env()
        s = s.model_copy(
            update={
                "news": NewsConfig(
                    enabled=True,
                    provider="yahoo_finance",
                    top_k_for_news=50,
                    lookback_hours=72,
                    max_articles_per_ticker=10,
                    cache_dir="./data/test_news_cache_yf",
                    alpha_sentiment=0.15,
                    alpha_catalyst=0.10,
                    beta_risk=0.20,
                    event_risk_mode="avoid",
                    llm_provider="mock",
                )
            }
        )
        return s

    @pytest.fixture
    def run_result(self, yahoo_finance_settings, patched_yf):
        client = MockEToroClient(reference_date=REF_DATE.date(), seed=42)
        return run_engine(
            settings=yahoo_finance_settings,
            tickers=["AAPL", "MSFT"],
            run_date=REF_DATE,
            force_rerun=True,
            client=client,
        )

    def test_run_completes(self, run_result):
        assert run_result is not None

    def test_produces_candidates(self, run_result):
        assert len(run_result.candidates) > 0

    def test_news_block_in_candidates(self, run_result):
        for c in run_result.candidates:
            d = c.to_dict()
            assert "news" in d
            assert "article_count" in d["news"]

    def test_news_articles_fetched(self, run_result):
        assert run_result.stats["news_articles_fetched"] > 0

    def test_news_provider_logged(self, run_result):
        assert run_result.stats["news_provider"] == "yahoo_finance"

    def test_score_multipliers_applied(self, run_result):
        from src.news.adjustments import MAX_MULTIPLIER, MIN_MULTIPLIER
        for c in run_result.candidates:
            m = c.news_score_multiplier
            assert MIN_MULTIPLIER <= m <= MAX_MULTIPLIER

    def test_deterministic_rerun(self, yahoo_finance_settings, patched_yf):
        client1 = MockEToroClient(reference_date=REF_DATE.date(), seed=42)
        client2 = MockEToroClient(reference_date=REF_DATE.date(), seed=42)
        r1 = run_engine(yahoo_finance_settings, ["AAPL"], REF_DATE, True, client1)
        r2 = run_engine(yahoo_finance_settings, ["AAPL"], REF_DATE, True, client2)
        assert len(r1.candidates) == len(r2.candidates)
        for c1, c2 in zip(r1.candidates, r2.candidates):
            assert c1.contract == c2.contract
            assert c1.news_score_multiplier == c2.news_score_multiplier

    def test_rationale_no_arithmetic(self, run_result):
        from src.llm.rationale import check_no_arithmetic
        for c in run_result.candidates:
            assert not check_no_arithmetic(c.reason_summary)

    def test_news_adjusted_score_present(self, run_result, yahoo_finance_settings):
        if not yahoo_finance_settings.news.enabled:
            pytest.skip()
        for c in run_result.candidates:
            assert c.news_adjusted_score is not None


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------


class TestProviderRegistry:
    def test_yahoo_finance_provider_registered(self):
        from src.news.provider import get_provider
        provider = get_provider("yahoo_finance")
        assert provider.provider_key == "yahoo_finance"

    def test_yahoo_finance_mcp_provider_registered(self):
        from src.news.provider import get_provider
        provider = get_provider("yahoo_finance_mcp")
        assert provider.provider_key == "yahoo_finance_mcp"

    def test_yahoo_finance_in_registry_metadata(self):
        from src.news.provider import MCP_SERVER_REGISTRY
        assert "yahoo_finance" in MCP_SERVER_REGISTRY
        info = MCP_SERVER_REGISTRY["yahoo_finance"]
        assert info["mcp_server_name"] == "yahoo-finance-news"

    def test_yahoo_finance_in_sanitizer_allowlist(self):
        from src.news.sanitizer import MCP_SERVER_ALLOWLIST, validate_mcp_server
        assert "yahoo-finance-news" in MCP_SERVER_ALLOWLIST
        assert validate_mcp_server("yahoo-finance-news") is True
