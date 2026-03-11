"""
Unit tests for news schemas — validation, URL safety, field constraints.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.news.schemas import (
    Catalyst,
    NewsFeatures,
    NormalizedNewsItem,
    RiskFlag,
    Sentiment,
)


class TestNormalizedNewsItem:
    def _make(self, **kwargs) -> NormalizedNewsItem:
        defaults = dict(
            ticker="AAPL",
            published_at=datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc),
            source="Reuters",
            title="Apple beats expectations",
            url="https://reuters.com/aapl-earnings",
        )
        defaults.update(kwargs)
        return NormalizedNewsItem(**defaults)

    def test_valid_item(self):
        item = self._make()
        assert item.ticker == "AAPL"
        assert item.url.startswith("https://")

    def test_rejects_non_http_url(self):
        with pytest.raises(ValidationError, match="http/https"):
            self._make(url="javascript:alert(1)")

    def test_rejects_file_url(self):
        with pytest.raises(ValidationError):
            self._make(url="file:///etc/passwd")

    def test_optional_summary(self):
        item = self._make(summary=None)
        assert item.summary is None

    def test_raw_field_accepts_dict(self):
        item = self._make(raw={"foo": "bar"})
        assert item.raw["foo"] == "bar"


class TestCatalyst:
    def test_valid_catalyst(self):
        c = Catalyst(type="earnings", direction="positive", confidence=0.85,
                     evidence_urls=["https://reuters.com/aapl"])
        assert c.confidence == 0.85

    def test_confidence_out_of_range_high(self):
        with pytest.raises(ValidationError):
            Catalyst(type="earnings", direction="positive", confidence=1.5)

    def test_confidence_out_of_range_low(self):
        with pytest.raises(ValidationError):
            Catalyst(type="earnings", direction="positive", confidence=-0.1)

    def test_invalid_catalyst_type(self):
        with pytest.raises(ValidationError):
            Catalyst(type="unknown_type", direction="positive", confidence=0.5)

    def test_invalid_direction(self):
        with pytest.raises(ValidationError):
            Catalyst(type="earnings", direction="bullish", confidence=0.5)

    def test_evidence_url_must_be_http(self):
        with pytest.raises(ValidationError):
            Catalyst(type="earnings", direction="positive", confidence=0.5,
                     evidence_urls=["ftp://bad.com/file"])

    def test_empty_evidence_urls_ok(self):
        c = Catalyst(type="macro", direction="mixed", confidence=0.3, evidence_urls=[])
        assert c.evidence_urls == []


class TestRiskFlag:
    def test_valid_risk_flag(self):
        rf = RiskFlag(type="earnings_imminent", severity="high",
                      evidence_urls=["https://ir.apple.com/earnings"])
        assert rf.severity == "high"

    def test_invalid_risk_type(self):
        with pytest.raises(ValidationError):
            RiskFlag(type="unknown_risk", severity="med")

    def test_invalid_severity(self):
        with pytest.raises(ValidationError):
            RiskFlag(type="litigation", severity="critical")

    def test_injected_url_rejected(self):
        with pytest.raises(ValidationError):
            RiskFlag(type="litigation", severity="med",
                     evidence_urls=["javascript:void(0)"])


class TestNewsFeatures:
    def test_minimal_valid_features(self):
        f = NewsFeatures(
            ticker="AAPL",
            sentiment=Sentiment(label="neutral", confidence=0.0),
        )
        assert f.ticker == "AAPL"
        assert f.catalysts == []
        assert f.risk_flags == []

    def test_dominant_sentiment_sign_positive(self):
        f = NewsFeatures(
            ticker="AAPL",
            sentiment=Sentiment(label="positive", confidence=0.8),
        )
        assert f.dominant_sentiment_sign() == 1

    def test_dominant_sentiment_sign_negative(self):
        f = NewsFeatures(
            ticker="TSLA",
            sentiment=Sentiment(label="negative", confidence=0.7),
        )
        assert f.dominant_sentiment_sign() == -1

    def test_dominant_sentiment_sign_neutral(self):
        f = NewsFeatures(
            ticker="SPY",
            sentiment=Sentiment(label="neutral", confidence=0.0),
        )
        assert f.dominant_sentiment_sign() == 0

    def test_mixed_sentiment_sign_zero(self):
        f = NewsFeatures(
            ticker="QQQ",
            sentiment=Sentiment(label="mixed", confidence=0.5),
        )
        assert f.dominant_sentiment_sign() == 0

    def test_has_earnings_risk_true(self):
        f = NewsFeatures(
            ticker="AAPL",
            sentiment=Sentiment(label="neutral", confidence=0.0),
            risk_flags=[RiskFlag(type="earnings_imminent", severity="med")],
        )
        assert f.has_earnings_risk() is True

    def test_has_earnings_risk_false(self):
        f = NewsFeatures(
            ticker="SPY",
            sentiment=Sentiment(label="neutral", confidence=0.0),
            risk_flags=[RiskFlag(type="litigation", severity="low")],
        )
        assert f.has_earnings_risk() is False

    def test_max_risk_severity_weight(self):
        f = NewsFeatures(
            ticker="AAPL",
            sentiment=Sentiment(label="neutral", confidence=0.0),
            risk_flags=[
                RiskFlag(type="litigation", severity="low"),
                RiskFlag(type="sec_inquiry", severity="high"),
            ],
        )
        assert f.max_risk_severity_weight() == 1.0

    def test_max_risk_severity_no_flags(self):
        f = NewsFeatures(
            ticker="SPY",
            sentiment=Sentiment(label="neutral", confidence=0.0),
        )
        assert f.max_risk_severity_weight() == 0.0

    def test_model_serialisation_round_trip(self):
        f = NewsFeatures(
            ticker="MSFT",
            sentiment=Sentiment(label="positive", confidence=0.75),
            catalysts=[
                Catalyst(type="guidance", direction="positive", confidence=0.6,
                         evidence_urls=["https://cnbc.com/msft"])
            ],
        )
        d = f.model_dump(mode="json")
        f2 = NewsFeatures.model_validate(d)
        assert f2.ticker == "MSFT"
        assert f2.sentiment.label == "positive"
        assert len(f2.catalysts) == 1
