"""
Abstract news provider interface.

The provider abstraction decouples the news pipeline from any specific
MCP server or REST API.  Concrete providers implement BaseNewsProvider
and are registered in the PROVIDER_REGISTRY.

MCP server metadata is declared here per the MCP Registry (preview)
specification: https://registry.modelcontextprotocol.io
Each entry specifies the canonical server name, version, and which
MCP tool names it exposes for news fetching.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date
from typing import Dict, List, Type

from .schemas import NormalizedNewsItem

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MCP Server metadata registry
# ---------------------------------------------------------------------------

MCP_SERVER_REGISTRY: Dict[str, dict] = {
    "fmp": {
        "mcp_server_name": "mcp-server-financialmodelingprep",
        "version": "0.1.0",
        "source_url": "https://registry.modelcontextprotocol.io/servers/mcp-server-financialmodelingprep",
        "tools": ["get_stock_news", "get_company_news", "get_general_news"],
        "rest_fallback": "https://financialmodelingprep.com/api/v3/stock_news",
        "env_key": "FMP_API_KEY",
        "description": "Financial Modeling Prep — institutional-grade news + fundamentals",
    },
    "financial_datasets": {
        "mcp_server_name": "financial-datasets-mcp",
        "version": "0.1.0",
        "source_url": "https://registry.modelcontextprotocol.io/servers/financial-datasets-mcp",
        "tools": ["get_market_news", "search_news"],
        "rest_fallback": "https://api.financialdatasets.ai/news",
        "env_key": "FINANCIAL_DATASETS_API_KEY",
        "description": "Financial Datasets — market data + news via MCP",
    },
    "finnhub": {
        "mcp_server_name": "finance-news-mcp",
        "version": "0.1.0",
        "source_url": "https://registry.modelcontextprotocol.io/servers/finance-news-mcp",
        "tools": ["search_financial_news", "get_company_news"],
        "rest_fallback": "https://finnhub.io/api/v1/company-news",
        "env_key": "FINNHUB_API_KEY",
        "description": "Finnhub-backed finance news MCP — real-time, query-driven",
    },
    "yahoo_finance": {
        "mcp_server_name": "yahoo-finance-news",
        "version": "1.0.0",
        "source_url": "local:mcp_servers/yahoo_finance_news",
        "tools": ["get_ticker_news", "get_market_news", "search_news"],
        "rest_fallback": None,
        "env_key": None,
        "description": "Yahoo Finance news via yfinance — no API key required",
    },
    "yahoo_finance_mcp": {
        "mcp_server_name": "yahoo-finance-news",
        "version": "1.0.0",
        "source_url": "local:mcp_servers/yahoo_finance_news",
        "tools": ["get_ticker_news", "get_market_news", "search_news"],
        "rest_fallback": None,
        "env_key": None,
        "description": "Yahoo Finance news via MCP stdio subprocess",
    },
    "mock": {
        "mcp_server_name": "mock-news-provider",
        "version": "0.0.1",
        "source_url": "local",
        "tools": [],
        "rest_fallback": None,
        "env_key": None,
        "description": "Deterministic mock for tests and offline development",
    },
}


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class BaseNewsProvider(ABC):
    """
    Abstract news provider.  All concrete providers must implement this.

    Providers return NormalizedNewsItem objects.  They must:
      - Call sanitizer.sanitize_article_dict on all raw API responses
      - Return articles sorted by published_at descending (newest first)
      - Never raise — return [] on errors, log the failure
    """

    provider_key: str = "base"

    @abstractmethod
    def fetch_news(
        self,
        ticker: str,
        as_of: date,
        lookback_hours: int = 72,
        max_articles: int = 20,
    ) -> List[NormalizedNewsItem]:
        """
        Fetch recent news for a ticker.

        Args:
            ticker: The stock ticker symbol.
            as_of: Reference date (UTC).  Fetch articles published
                   between (as_of - lookback_hours) and as_of.
            lookback_hours: Look-back window in hours.
            max_articles: Maximum number of articles to return.

        Returns:
            List of NormalizedNewsItem, sorted newest-first.
        """
        ...

    def is_available(self) -> bool:
        """Return True if the provider is configured and reachable."""
        return True

    def provider_info(self) -> dict:
        """Return MCP server metadata for this provider."""
        return MCP_SERVER_REGISTRY.get(self.provider_key, {})


# ---------------------------------------------------------------------------
# Provider factory / registry
# ---------------------------------------------------------------------------

_PROVIDER_CLASSES: Dict[str, Type[BaseNewsProvider]] = {}


def register_provider(key: str):
    """Decorator to register a provider class under a key."""
    def decorator(cls: Type[BaseNewsProvider]):
        _PROVIDER_CLASSES[key] = cls
        cls.provider_key = key
        return cls
    return decorator


def get_provider(key: str, **kwargs) -> BaseNewsProvider:
    """
    Instantiate and return a news provider by key.

    Args:
        key: Provider key (fmp | financial_datasets | finnhub | mock).
        **kwargs: Passed to the provider constructor.

    Raises:
        ValueError: If the provider key is not registered.
    """
    if key not in _PROVIDER_CLASSES:
        raise ValueError(
            f"Unknown news provider {key!r}. "
            f"Available: {list(_PROVIDER_CLASSES.keys())}"
        )
    return _PROVIDER_CLASSES[key](**kwargs)
