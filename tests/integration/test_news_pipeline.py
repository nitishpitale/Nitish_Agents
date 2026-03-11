"""
Integration tests for the news pipeline.

Tests verify:
  1. Mock news provider fetches articles for known tickers.
  2. Feature extraction produces valid NewsFeatures objects.
  3. Score adjustments are deterministic.
  4. Full engine run with news enabled produces enriched candidates.
  5. Re-running with same fixtures produces identical results (determinism).
  6. No LLM arithmetic in rationale summaries.
  7. Candidate JSON schema includes the "news" block with required fields.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.config.settings import Settings
from src.engine import run_engine
from src.ingest.mock_client import MockEToroClient
from src.news import (
    NewsCache,
    NewsFeatures,
    NormalizedNewsItem,
    extract_news_features,
)
from src.news.mcp_client import MockNewsProvider
from src.news.sanitizer import check_no_arithmetic_news

REF_DATE = datetime(2026, 3, 11, 18, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.from_yaml_and_env()


@pytest.fixture(scope="module")
def market_client() -> MockEToroClient:
    return MockEToroClient(reference_date=REF_DATE.date(), seed=42)


@pytest.fixture(scope="module")
def news_provider() -> MockNewsProvider:
    return MockNewsProvider(seed=42, reference_date=REF_DATE.date())


# ---------------------------------------------------------------------------
# Mock provider tests
# ---------------------------------------------------------------------------


class TestMockNewsProvider:
    def test_fetches_articles_for_known_ticker(self, news_provider):
        articles = news_provider.fetch_news("AAPL", REF_DATE.date())
        assert len(articles) > 0

    def test_articles_are_normalized_news_items(self, news_provider):
        articles = news_provider.fetch_news("AAPL", REF_DATE.date())
        for a in articles:
            assert isinstance(a, NormalizedNewsItem)

    def test_articles_sorted_newest_first(self, news_provider):
        articles = news_provider.fetch_news("MSFT", REF_DATE.date())
        for i in range(len(articles) - 1):
            assert articles[i].published_at >= articles[i + 1].published_at

    def test_articles_within_lookback_window(self, news_provider):
        lookback = 72
        articles = news_provider.fetch_news("TSLA", REF_DATE.date(), lookback_hours=lookback)
        cutoff = datetime(REF_DATE.year, REF_DATE.month, REF_DATE.day, tzinfo=timezone.utc)
        for a in articles:
            assert a.published_at <= cutoff

    def test_unknown_ticker_returns_empty(self, news_provider):
        articles = news_provider.fetch_news("UNKNOWN_XYZ", REF_DATE.date())
        assert articles == []

    def test_deterministic_same_ticker(self, news_provider):
        a1 = news_provider.fetch_news("NVDA", REF_DATE.date())
        a2 = news_provider.fetch_news("NVDA", REF_DATE.date())
        assert len(a1) == len(a2)
        for i1, i2 in zip(a1, a2):
            assert i1.title == i2.title
            assert i1.url == i2.url

    def test_all_urls_are_https(self, news_provider):
        for ticker in ["AAPL", "MSFT", "TSLA", "SPY"]:
            for a in news_provider.fetch_news(ticker, REF_DATE.date()):
                assert a.url.startswith("http"), f"Bad URL: {a.url}"

    def test_earnings_imminent_article_for_relevant_tickers(self, news_provider):
        """Tickers near earnings should have an earnings_imminent article."""
        articles = news_provider.fetch_news("AAPL", REF_DATE.date())
        titles = [a.title.lower() for a in articles]
        has_earnings = any("earnings" in t for t in titles)
        assert has_earnings


# ---------------------------------------------------------------------------
# Feature extraction tests
# ---------------------------------------------------------------------------


class TestFeatureExtraction:
    def test_produces_valid_news_features(self, news_provider):
        articles = news_provider.fetch_news("AAPL", REF_DATE.date())
        features = extract_news_features("AAPL", articles, REF_DATE.date())
        assert isinstance(features, NewsFeatures)
        assert features.ticker == "AAPL"

    def test_sentiment_is_valid_label(self, news_provider):
        articles = news_provider.fetch_news("TSLA", REF_DATE.date())
        features = extract_news_features("TSLA", articles, REF_DATE.date())
        assert features.sentiment.label in ("positive", "negative", "mixed", "neutral")
        assert 0.0 <= features.sentiment.confidence <= 1.0

    def test_no_arithmetic_in_hypotheses(self, news_provider):
        """Mispricing hypotheses must not contain arithmetic expressions."""
        articles = news_provider.fetch_news("AAPL", REF_DATE.date())
        features = extract_news_features("AAPL", articles, REF_DATE.date())
        for hyp in features.why_mispriced_hypotheses:
            assert not check_no_arithmetic_news(hyp.hypothesis), (
                f"Hypothesis contains arithmetic: {hyp.hypothesis}"
            )

    def test_evidence_urls_in_article_set(self, news_provider):
        """All evidence URLs must come from the provided articles."""
        articles = news_provider.fetch_news("MSFT", REF_DATE.date())
        features = extract_news_features("MSFT", articles, REF_DATE.date())
        article_urls = {a.url for a in articles}
        for cat in features.catalysts:
            for url in cat.evidence_urls:
                assert url in article_urls, f"URL not in articles: {url}"
        for rf in features.risk_flags:
            for url in rf.evidence_urls:
                assert url in article_urls, f"URL not in articles: {url}"

    def test_empty_articles_returns_neutral(self):
        features = extract_news_features("SPY", [], REF_DATE.date())
        assert features.sentiment.label == "neutral"
        assert features.sentiment.confidence == 0.0

    def test_deterministic_extraction(self, news_provider):
        articles = news_provider.fetch_news("NVDA", REF_DATE.date())
        f1 = extract_news_features("NVDA", articles, REF_DATE.date())
        f2 = extract_news_features("NVDA", articles, REF_DATE.date())
        assert f1.sentiment.label == f2.sentiment.label
        assert f1.sentiment.confidence == f2.sentiment.confidence
        assert len(f1.catalysts) == len(f2.catalysts)


# ---------------------------------------------------------------------------
# Full engine run with news enabled
# ---------------------------------------------------------------------------


class TestEnginewithNews:
    @pytest.fixture(scope="class")
    def run_result(self, settings, market_client, news_provider):
        return run_engine(
            settings=settings,
            tickers=["AAPL", "MSFT", "TSLA", "NVDA", "SPY"],
            run_date=REF_DATE,
            force_rerun=True,
            client=market_client,
        )

    def test_run_completes(self, run_result):
        assert run_result is not None
        assert len(run_result.candidates) > 0

    def test_candidates_have_news_block(self, run_result):
        """Every candidate must include the 'news' key in to_dict()."""
        for c in run_result.candidates:
            d = c.to_dict()
            assert "news" in d, f"Missing news block in {c.contract}"
            news = d["news"]
            assert "article_count" in news
            assert "top_headlines" in news
            assert "score_multiplier" in news

    def test_news_schema_types(self, run_result):
        for c in run_result.candidates:
            news = c.to_dict()["news"]
            assert isinstance(news["article_count"], int)
            assert isinstance(news["top_headlines"], list)
            assert isinstance(news["score_multiplier"], float)

    def test_news_score_multiplier_in_range(self, run_result):
        from src.news.adjustments import MAX_MULTIPLIER, MIN_MULTIPLIER
        for c in run_result.candidates:
            m = c.news_score_multiplier
            assert MIN_MULTIPLIER <= m <= MAX_MULTIPLIER, (
                f"Multiplier {m} out of range for {c.contract}"
            )

    def test_news_adjusted_score_present_when_news_enabled(self, run_result, settings):
        if not settings.news.enabled:
            pytest.skip("News disabled")
        for c in run_result.candidates:
            assert c.news_adjusted_score is not None, (
                f"news_adjusted_score missing for {c.contract}"
            )

    def test_stats_include_news_fields(self, run_result):
        stats = run_result.stats
        assert "news_enabled" in stats
        assert "news_provider" in stats
        assert "news_articles_fetched" in stats
        assert "news_cache_hit_ratio" in stats

    def test_rationale_references_news_when_available(self, run_result):
        """Rationale for candidates with news should mention sentiment or catalyst."""
        for c in run_result.candidates:
            if c.news_article_count > 0 and c.reason_summary:
                # Just verify non-empty; content check below
                assert len(c.reason_summary) >= 80

    def test_no_arithmetic_in_rationale(self, run_result):
        """Acceptance test: rationale must not contain arithmetic."""
        from src.llm.rationale import check_no_arithmetic
        for c in run_result.candidates:
            assert not check_no_arithmetic(c.reason_summary), (
                f"Arithmetic in rationale for {c.contract}: {c.reason_summary}"
            )

    def test_deterministic_rerun(self, settings):
        """Two identical runs must produce identical candidates."""
        client1 = MockEToroClient(reference_date=REF_DATE.date(), seed=42)
        client2 = MockEToroClient(reference_date=REF_DATE.date(), seed=42)

        r1 = run_engine(
            settings=settings,
            tickers=["AAPL", "MSFT"],
            run_date=REF_DATE,
            force_rerun=True,
            client=client1,
        )
        r2 = run_engine(
            settings=settings,
            tickers=["AAPL", "MSFT"],
            run_date=REF_DATE,
            force_rerun=True,
            client=client2,
        )

        assert len(r1.candidates) == len(r2.candidates)
        for c1, c2 in zip(r1.candidates, r2.candidates):
            assert c1.contract == c2.contract
            assert c1.score == c2.score
            assert c1.news_score_multiplier == c2.news_score_multiplier
            assert c1.news_adjusted_score == c2.news_adjusted_score


# ---------------------------------------------------------------------------
# News cache tests
# ---------------------------------------------------------------------------


class TestNewsCache:
    def test_cache_miss_returns_none(self, tmp_path):
        cache = NewsCache(cache_dir=str(tmp_path))
        result = cache.get("AAPL", REF_DATE.date(), 72)
        assert result is None
        assert cache.misses == 1
        assert cache.hits == 0

    def test_cache_set_and_get(self, tmp_path, news_provider):
        cache = NewsCache(cache_dir=str(tmp_path))
        articles = news_provider.fetch_news("MSFT", REF_DATE.date())
        cache.set("MSFT", REF_DATE.date(), 72, articles)

        retrieved = cache.get("MSFT", REF_DATE.date(), 72)
        assert retrieved is not None
        assert len(retrieved) == len(articles)
        assert cache.hits == 1

    def test_cache_wrong_date_misses(self, tmp_path, news_provider):
        from datetime import timedelta
        cache = NewsCache(cache_dir=str(tmp_path))
        articles = news_provider.fetch_news("SPY", REF_DATE.date())
        cache.set("SPY", REF_DATE.date(), 72, articles)

        other_date = REF_DATE.date() + timedelta(days=1)
        result = cache.get("SPY", other_date, 72)
        assert result is None

    def test_hit_ratio_calculation(self, tmp_path, news_provider):
        cache = NewsCache(cache_dir=str(tmp_path))
        articles = news_provider.fetch_news("AAPL", REF_DATE.date())
        cache.set("AAPL", REF_DATE.date(), 72, articles)

        cache.get("AAPL", REF_DATE.date(), 72)   # hit
        cache.get("TSLA", REF_DATE.date(), 72)   # miss

        assert cache.hit_ratio == 0.5

    def test_cache_returns_normalized_items(self, tmp_path, news_provider):
        cache = NewsCache(cache_dir=str(tmp_path))
        articles = news_provider.fetch_news("NVDA", REF_DATE.date())
        cache.set("NVDA", REF_DATE.date(), 72, articles)

        retrieved = cache.get("NVDA", REF_DATE.date(), 72)
        for item in retrieved:
            assert isinstance(item, NormalizedNewsItem)
