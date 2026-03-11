"""
Unit tests for deterministic news score adjustments.

All tests verify that:
  - Same inputs → same output (determinism)
  - Direction signals correctly boost/penalise calls vs puts
  - Risk flags always penalise (unless earnings + seek mode)
  - Multiplier is clamped within [MIN_MULTIPLIER, MAX_MULTIPLIER]
  - LLM never touches numeric values here (pure code)
"""

from __future__ import annotations


from src.news.adjustments import (
    MIN_MULTIPLIER,
    MAX_MULTIPLIER,
    compute_news_score_adjustment,
    adjustment_summary,
)
from src.news.schemas import (
    Catalyst,
    NewsFeatures,
    RiskFlag,
    Sentiment,
)


def _make_features(
    ticker="AAPL",
    sentiment_label="neutral",
    sentiment_conf=0.0,
    catalysts=None,
    risk_flags=None,
) -> NewsFeatures:
    return NewsFeatures(
        ticker=ticker,
        sentiment=Sentiment(label=sentiment_label, confidence=sentiment_conf),
        catalysts=catalysts or [],
        risk_flags=risk_flags or [],
    )


class TestSentimentAdjustment:
    def test_neutral_no_change(self):
        f = _make_features(sentiment_label="neutral", sentiment_conf=0.0)
        mult_c = compute_news_score_adjustment(f, "C")
        mult_p = compute_news_score_adjustment(f, "P")
        assert abs(mult_c - 1.0) < 1e-9
        assert abs(mult_p - 1.0) < 1e-9

    def test_positive_sentiment_boosts_calls(self):
        f = _make_features(sentiment_label="positive", sentiment_conf=0.8)
        mult = compute_news_score_adjustment(f, "C", alpha_sentiment=0.15)
        assert mult > 1.0

    def test_positive_sentiment_penalises_puts(self):
        f = _make_features(sentiment_label="positive", sentiment_conf=0.8)
        mult = compute_news_score_adjustment(f, "P", alpha_sentiment=0.15)
        assert mult < 1.0

    def test_negative_sentiment_penalises_calls(self):
        f = _make_features(sentiment_label="negative", sentiment_conf=0.8)
        mult = compute_news_score_adjustment(f, "C", alpha_sentiment=0.15)
        assert mult < 1.0

    def test_negative_sentiment_boosts_puts(self):
        f = _make_features(sentiment_label="negative", sentiment_conf=0.8)
        mult = compute_news_score_adjustment(f, "P", alpha_sentiment=0.15)
        assert mult > 1.0

    def test_mixed_sentiment_no_change(self):
        f = _make_features(sentiment_label="mixed", sentiment_conf=0.5)
        mult_c = compute_news_score_adjustment(f, "C", alpha_sentiment=0.15)
        mult_p = compute_news_score_adjustment(f, "P", alpha_sentiment=0.15)
        # mixed sign = 0 → no sentiment contribution
        assert abs(mult_c - 1.0) < 1e-9
        assert abs(mult_p - 1.0) < 1e-9

    def test_higher_confidence_stronger_effect(self):
        f_low = _make_features(sentiment_label="positive", sentiment_conf=0.2)
        f_high = _make_features(sentiment_label="positive", sentiment_conf=0.9)
        mult_low = compute_news_score_adjustment(f_low, "C")
        mult_high = compute_news_score_adjustment(f_high, "C")
        assert mult_high > mult_low

    def test_exact_formula(self):
        """
        positive sentiment, conf=0.5, alpha=0.15, no other signals:
        multiplier = 1 + 0.15 * 1 * 0.5 = 1.075
        """
        f = _make_features(sentiment_label="positive", sentiment_conf=0.5)
        mult = compute_news_score_adjustment(f, "C", alpha_sentiment=0.15,
                                             alpha_catalyst=0.0, beta_risk=0.0)
        assert abs(mult - 1.075) < 1e-9


class TestCatalystAdjustment:
    def test_positive_catalyst_boosts_call(self):
        f = _make_features(
            catalysts=[Catalyst(type="earnings", direction="positive", confidence=0.7)]
        )
        mult = compute_news_score_adjustment(f, "C", alpha_catalyst=0.10,
                                             alpha_sentiment=0.0, beta_risk=0.0)
        assert mult > 1.0

    def test_negative_catalyst_penalises_call(self):
        f = _make_features(
            catalysts=[Catalyst(type="legal", direction="negative", confidence=0.7)]
        )
        mult = compute_news_score_adjustment(f, "C", alpha_catalyst=0.10,
                                             alpha_sentiment=0.0, beta_risk=0.0)
        assert mult < 1.0

    def test_unclear_catalyst_no_change(self):
        f = _make_features(
            catalysts=[Catalyst(type="macro", direction="unclear", confidence=0.8)]
        )
        mult = compute_news_score_adjustment(f, "C", alpha_catalyst=0.10,
                                             alpha_sentiment=0.0, beta_risk=0.0)
        assert abs(mult - 1.0) < 1e-9

    def test_mixed_catalyst_no_change(self):
        f = _make_features(
            catalysts=[Catalyst(type="analyst", direction="mixed", confidence=0.6)]
        )
        mult = compute_news_score_adjustment(f, "C", alpha_catalyst=0.10,
                                             alpha_sentiment=0.0, beta_risk=0.0)
        assert abs(mult - 1.0) < 1e-9


class TestRiskAdjustment:
    def test_low_risk_small_penalty(self):
        f = _make_features(
            risk_flags=[RiskFlag(type="litigation", severity="low")]
        )
        mult = compute_news_score_adjustment(f, "C", alpha_sentiment=0.0,
                                             alpha_catalyst=0.0, beta_risk=0.20)
        # 1 - 0.20 * 0.25 = 0.95
        assert abs(mult - 0.95) < 1e-9

    def test_high_risk_large_penalty(self):
        f = _make_features(
            risk_flags=[RiskFlag(type="sec_inquiry", severity="high")]
        )
        mult = compute_news_score_adjustment(f, "C", alpha_sentiment=0.0,
                                             alpha_catalyst=0.0, beta_risk=0.20)
        # 1 - 0.20 * 1.0 = 0.80
        assert abs(mult - 0.80) < 1e-9

    def test_multiple_risk_flags_compound(self):
        f = _make_features(
            risk_flags=[
                RiskFlag(type="litigation", severity="med"),
                RiskFlag(type="macro_shock", severity="med"),
            ]
        )
        mult = compute_news_score_adjustment(f, "C", alpha_sentiment=0.0,
                                             alpha_catalyst=0.0, beta_risk=0.20)
        # (1 - 0.20*0.60) * (1 - 0.20*0.60) = 0.88 * 0.88 = 0.7744
        expected = (1 - 0.20 * 0.60) ** 2
        assert abs(mult - expected) < 1e-9

    def test_earnings_imminent_avoid_mode_penalises(self):
        f = _make_features(
            risk_flags=[RiskFlag(type="earnings_imminent", severity="high")]
        )
        mult = compute_news_score_adjustment(f, "C", alpha_sentiment=0.0,
                                             alpha_catalyst=0.0, beta_risk=0.20,
                                             event_risk_mode="avoid")
        # 1 - 0.20 * 1.0 = 0.80
        assert mult < 1.0

    def test_earnings_imminent_seek_mode_boosts(self):
        f = _make_features(
            risk_flags=[RiskFlag(type="earnings_imminent", severity="high")]
        )
        mult = compute_news_score_adjustment(f, "C", alpha_sentiment=0.0,
                                             alpha_catalyst=0.0, beta_risk=0.20,
                                             event_risk_mode="seek")
        # 1 + 0.20 * 1.0 * 0.5 = 1.10
        assert mult > 1.0


class TestClampBehaviour:
    def test_multiplier_never_below_min(self):
        """Extreme negative signals should not go below MIN_MULTIPLIER."""
        f = _make_features(
            sentiment_label="negative",
            sentiment_conf=1.0,
            risk_flags=[
                RiskFlag(type="sec_inquiry", severity="high"),
                RiskFlag(type="litigation", severity="high"),
                RiskFlag(type="guidance_cut", severity="high"),
                RiskFlag(type="macro_shock", severity="high"),
            ],
        )
        mult = compute_news_score_adjustment(f, "C", alpha_sentiment=0.5,
                                             beta_risk=0.9)
        assert mult >= MIN_MULTIPLIER

    def test_multiplier_never_above_max(self):
        """Extreme positive signals should not exceed MAX_MULTIPLIER."""
        f = _make_features(
            sentiment_label="positive",
            sentiment_conf=1.0,
            catalysts=[
                Catalyst(type="earnings", direction="positive", confidence=1.0),
                Catalyst(type="analyst", direction="positive", confidence=1.0),
                Catalyst(type="guidance", direction="positive", confidence=1.0),
            ],
        )
        mult = compute_news_score_adjustment(f, "C", alpha_sentiment=5.0,
                                             alpha_catalyst=5.0)
        assert mult <= MAX_MULTIPLIER


class TestDeterminism:
    def test_same_inputs_same_output(self):
        f = _make_features(
            sentiment_label="positive",
            sentiment_conf=0.7,
            catalysts=[Catalyst(type="earnings", direction="positive", confidence=0.8)],
            risk_flags=[RiskFlag(type="earnings_imminent", severity="med")],
        )
        m1 = compute_news_score_adjustment(f, "C")
        m2 = compute_news_score_adjustment(f, "C")
        assert m1 == m2

    def test_call_put_multipliers_are_mirrors_for_symmetric_signal(self):
        """For a pure positive sentiment signal, call mult > 1 and put mult < 1 symmetrically."""
        f = _make_features(sentiment_label="positive", sentiment_conf=0.6)
        mult_c = compute_news_score_adjustment(f, "C", alpha_sentiment=0.20,
                                               alpha_catalyst=0.0, beta_risk=0.0)
        mult_p = compute_news_score_adjustment(f, "P", alpha_sentiment=0.20,
                                               alpha_catalyst=0.0, beta_risk=0.0)
        # For positive sentiment: call = 1 + 0.20*0.6 = 1.12, put = 1 - 0.20*0.6 = 0.88
        assert abs(mult_c - 1.12) < 1e-9
        assert abs(mult_p - 0.88) < 1e-9
        # They should be symmetric around 1.0
        assert abs((mult_c - 1.0) + (mult_p - 1.0)) < 1e-9


class TestAdjustmentSummary:
    def test_summary_contains_required_keys(self):
        f = _make_features(
            sentiment_label="positive",
            sentiment_conf=0.7,
            risk_flags=[RiskFlag(type="earnings_imminent", severity="med")],
        )
        mult = compute_news_score_adjustment(f, "C")
        summary = adjustment_summary(f, mult, "C")

        assert "news_score_multiplier" in summary
        assert "sentiment" in summary
        assert "sentiment_confidence" in summary
        assert "risk_flags" in summary
        assert "has_earnings_risk" in summary
        assert summary["has_earnings_risk"] is True
        assert summary["sentiment"] == "positive"
