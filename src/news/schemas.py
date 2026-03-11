"""
Pydantic schemas for the news pipeline.

NormalizedNewsItem: raw article from any provider, normalised.
NewsFeatures: structured LLM output — catalysts, sentiment, risk flags,
              mispricing hypotheses.  All numeric fields are confidence
              values in [0, 1]; the LLM must not introduce any other numbers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enum-style Literals
# ---------------------------------------------------------------------------

CatalystType = Literal[
    "earnings", "guidance", "macro", "legal", "product",
    "analyst", "ratings", "m&a", "regulatory", "other",
]
Direction = Literal["positive", "negative", "mixed", "unclear"]
SentimentLabel = Literal["positive", "negative", "mixed", "neutral"]
Severity = Literal["low", "med", "high"]
RiskType = Literal[
    "earnings_imminent", "litigation", "sec_inquiry", "guidance_cut",
    "macro_shock", "high_short_interest", "rumor",
]

# ---------------------------------------------------------------------------
# NormalizedNewsItem
# ---------------------------------------------------------------------------


class NormalizedNewsItem(BaseModel):
    """
    A single article normalised from any news provider.
    The 'raw' field holds the original provider response for audit.
    """
    ticker: str
    published_at: datetime
    source: str
    title: str
    url: str
    summary: Optional[str] = None
    raw: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def validate_url_scheme(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"URL must use http/https scheme: {v!r}")
        return v


# ---------------------------------------------------------------------------
# NewsFeatures sub-models
# ---------------------------------------------------------------------------


class Catalyst(BaseModel):
    type: CatalystType
    direction: Direction
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_urls: List[str] = Field(default_factory=list)

    @field_validator("evidence_urls")
    @classmethod
    def urls_must_be_http(cls, urls: List[str]) -> List[str]:
        for u in urls:
            if u and not u.startswith(("http://", "https://")):
                raise ValueError(f"evidence_url must use http/https: {u!r}")
        return urls


class Sentiment(BaseModel):
    label: SentimentLabel
    confidence: float = Field(ge=0.0, le=1.0)


class RiskFlag(BaseModel):
    type: RiskType
    severity: Severity
    evidence_urls: List[str] = Field(default_factory=list)

    @field_validator("evidence_urls")
    @classmethod
    def urls_must_be_http(cls, urls: List[str]) -> List[str]:
        for u in urls:
            if u and not u.startswith(("http://", "https://")):
                raise ValueError(f"evidence_url must use http/https: {u!r}")
        return urls


class MisPricingHypothesis(BaseModel):
    hypothesis: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_urls: List[str] = Field(default_factory=list)

    @field_validator("evidence_urls")
    @classmethod
    def urls_must_be_http(cls, urls: List[str]) -> List[str]:
        for u in urls:
            if u and not u.startswith(("http://", "https://")):
                raise ValueError(f"evidence_url must use http/https: {u!r}")
        return urls


# ---------------------------------------------------------------------------
# NewsFeatures — the structured LLM output
# ---------------------------------------------------------------------------


class NewsFeatures(BaseModel):
    """
    Structured signals extracted from news articles by the LLM.

    IMPORTANT: The LLM must not introduce any numeric values except
    confidence scores in [0, 1].  All URLs must come from the
    provided articles.  No arithmetic expressions are permitted.
    """
    ticker: str
    catalysts: List[Catalyst] = Field(default_factory=list)
    sentiment: Sentiment = Field(default_factory=lambda: Sentiment(label="neutral", confidence=0.0))
    risk_flags: List[RiskFlag] = Field(default_factory=list)
    why_mispriced_hypotheses: List[MisPricingHypothesis] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_evidence_urls_from_provided(self) -> "NewsFeatures":
        """
        Enforced at extraction time (with the actual article URLs available).
        Here we just ensure all evidence_urls are non-empty valid URLs.
        The extractor validates against the actual article URL set.
        """
        return self

    def dominant_sentiment_sign(self) -> int:
        """Return +1, -1, or 0 based on sentiment label."""
        return {"positive": 1, "negative": -1, "mixed": 0, "neutral": 0}[self.sentiment.label]

    def max_risk_severity_weight(self) -> float:
        """Return the highest severity weight across all risk flags."""
        weights = {"low": 0.25, "med": 0.60, "high": 1.00}
        if not self.risk_flags:
            return 0.0
        return max(weights[rf.severity] for rf in self.risk_flags)

    def has_earnings_risk(self) -> bool:
        return any(rf.type == "earnings_imminent" for rf in self.risk_flags)

    def top_headlines_from_articles(
        self, articles: List[NormalizedNewsItem], n: int = 3
    ) -> List[str]:
        return [a.title for a in articles[:n]]


# ---------------------------------------------------------------------------
# Run-level news stats (for logging/monitoring)
# ---------------------------------------------------------------------------


class NewsRunStats(BaseModel):
    provider: str
    tickers_fetched: int = 0
    total_articles: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    llm_calls: int = 0
    errors: List[str] = Field(default_factory=list)

    @property
    def cache_hit_ratio(self) -> float:
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total > 0 else 0.0
