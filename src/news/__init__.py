"""
News pipeline for the Volatility Mispricing Engine.

Pipeline stages:
  1. Fetch articles via BaseNewsProvider (FMP, mock, etc.)
  2. Cache results by (ticker, date, lookback_hours)
  3. Extract NewsFeatures via LLM (structured JSON, no math)
  4. Compute deterministic score multipliers from NewsFeatures
"""

from .schemas import (
    Catalyst,
    MisPricingHypothesis,
    NewsFeatures,
    NewsRunStats,
    NormalizedNewsItem,
    RiskFlag,
    Sentiment,
)
from .provider import BaseNewsProvider, get_provider, MCP_SERVER_REGISTRY
from .cache import NewsCache
from .feature_extractor import extract_news_features
from .adjustments import compute_news_score_adjustment, adjustment_summary

# Import concrete providers to trigger their registration
from . import mcp_client  # noqa: F401

__all__ = [
    "NormalizedNewsItem",
    "NewsFeatures",
    "Catalyst",
    "RiskFlag",
    "Sentiment",
    "MisPricingHypothesis",
    "NewsRunStats",
    "BaseNewsProvider",
    "get_provider",
    "MCP_SERVER_REGISTRY",
    "NewsCache",
    "extract_news_features",
    "compute_news_score_adjustment",
    "adjustment_summary",
]
