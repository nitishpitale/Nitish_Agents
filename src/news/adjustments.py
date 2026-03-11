"""
Deterministic score adjustments from NewsFeatures.

All logic in this module is pure deterministic code.
The LLM feeds structured JSON (NewsFeatures) but NEVER touches
the numeric adjustment formula — that lives exclusively here.

Formula (config-driven):
    multiplier = 1.0
    multiplier *= (1 + alpha_sentiment * sentiment_sign * sentiment_confidence)
    for each catalyst:
        multiplier *= (1 + alpha_catalyst * cat_sign * cat_confidence * CATALYST_WEIGHT)
    for each risk_flag:
        sev_weight = SEVERITY_WEIGHTS[severity]
        if earnings_imminent and event_risk_mode == "seek":
            multiplier *= (1 + beta_risk * sev_weight * 0.5)
        else:
            multiplier *= (1 - beta_risk * sev_weight)
    multiplier = clamp(multiplier, MIN_MULTIPLIER, MAX_MULTIPLIER)

sentiment_sign:
    positive → +1 for calls, -1 for puts
    negative → -1 for calls, +1 for puts
    mixed/neutral → 0

All alphas, betas, and severity weights are configurable.
"""

from __future__ import annotations


from .schemas import NewsFeatures, Sentiment

# ---------------------------------------------------------------------------
# Constants — default values; all are overridable via config
# ---------------------------------------------------------------------------

SEVERITY_WEIGHTS: dict[str, float] = {
    "low": 0.25,
    "med": 0.60,
    "high": 1.00,
}

# Catalyst weight (0.5 = half the effect of overall sentiment)
CATALYST_DIRECTIONAL_WEIGHT = 0.5

# Bounds on final multiplier to prevent runaway scores
MIN_MULTIPLIER = 0.10
MAX_MULTIPLIER = 3.00


# ---------------------------------------------------------------------------
# Deterministic adjustment computation
# ---------------------------------------------------------------------------


def compute_news_score_adjustment(
    features: NewsFeatures,
    option_type: str,  # "C" or "P"
    alpha_sentiment: float = 0.15,
    alpha_catalyst: float = 0.10,
    beta_risk: float = 0.20,
    event_risk_mode: str = "avoid",  # "avoid" | "seek"
) -> float:
    """
    Compute a multiplicative score adjustment from NewsFeatures.

    Args:
        features: Structured news features (LLM output, already validated).
        option_type: "C" (call) or "P" (put).
        alpha_sentiment: Scaling factor for overall sentiment effect.
        alpha_catalyst: Scaling factor for per-catalyst directional effect.
        beta_risk: Scaling factor for risk flag penalty/boost.
        event_risk_mode: "avoid" → penalise earnings risk;
                         "seek"  → boost earnings risk (vol buyers).

    Returns:
        A float multiplier. 1.0 = no adjustment.  Clamped to [MIN, MAX].
    """
    multiplier = 1.0
    is_call = option_type == "C"

    # ------------------------------------------------------------------
    # 1. Overall sentiment adjustment
    # ------------------------------------------------------------------
    sentiment_sign = _sentiment_sign(features.sentiment, is_call)
    multiplier *= (
        1.0 + alpha_sentiment * sentiment_sign * features.sentiment.confidence
    )

    # ------------------------------------------------------------------
    # 2. Per-catalyst directional adjustment
    # ------------------------------------------------------------------
    for catalyst in features.catalysts:
        cat_sign = _direction_sign(catalyst.direction, is_call)
        if cat_sign != 0:
            multiplier *= (
                1.0
                + alpha_catalyst
                * cat_sign
                * catalyst.confidence
                * CATALYST_DIRECTIONAL_WEIGHT
            )

    # ------------------------------------------------------------------
    # 3. Risk flag adjustments
    # ------------------------------------------------------------------
    for risk_flag in features.risk_flags:
        sev = SEVERITY_WEIGHTS[risk_flag.severity]

        if risk_flag.type == "earnings_imminent":
            if event_risk_mode == "seek":
                # Vol buyers want event risk — apply a modest boost
                multiplier *= 1.0 + beta_risk * sev * 0.5
            else:
                # Default: penalise event risk
                multiplier *= 1.0 - beta_risk * sev
        else:
            # All other risk types (litigation, sec_inquiry, macro_shock, etc.)
            # always penalise regardless of option direction
            multiplier *= 1.0 - beta_risk * sev

    # ------------------------------------------------------------------
    # 4. Clamp to safe range
    # ------------------------------------------------------------------
    return max(MIN_MULTIPLIER, min(multiplier, MAX_MULTIPLIER))


def _sentiment_sign(sentiment: Sentiment, is_call: bool) -> float:
    """
    Map sentiment label to a directional sign.
    Positive news → bullish → boosts calls, hurts puts.
    Negative news → bearish → hurts calls, boosts puts.
    """
    raw_sign = {"positive": 1.0, "negative": -1.0, "mixed": 0.0, "neutral": 0.0}[
        sentiment.label
    ]
    return raw_sign if is_call else -raw_sign


def _direction_sign(direction: str, is_call: bool) -> float:
    """Map catalyst direction to a per-catalyst sign."""
    raw_sign = {"positive": 1.0, "negative": -1.0, "mixed": 0.0, "unclear": 0.0}[
        direction
    ]
    return raw_sign if is_call else -raw_sign


# ---------------------------------------------------------------------------
# Summary helpers (for logging / metadata)
# ---------------------------------------------------------------------------


def adjustment_summary(
    features: NewsFeatures,
    multiplier: float,
    option_type: str,
) -> dict:
    """Return a concise dict describing the adjustment — for metadata/logging."""
    return {
        "news_score_multiplier": round(multiplier, 4),
        "sentiment": features.sentiment.label,
        "sentiment_confidence": round(features.sentiment.confidence, 3),
        "risk_flags": [rf.type for rf in features.risk_flags],
        "max_risk_severity": features.max_risk_severity_weight(),
        "catalyst_count": len(features.catalysts),
        "has_earnings_risk": features.has_earnings_risk(),
        "option_type": option_type,
    }
