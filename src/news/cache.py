"""
File-based cache for news articles.

Cache key: (ticker, date, lookback_hours)
Cache is stored as JSON files in a configurable directory.
TTL is set to one calendar day — articles fetched on run_date
are reused for the entire day to keep runs idempotent and cheap.

Monitoring: hit/miss counts are tracked in NewsRunStats.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional

from .schemas import NormalizedNewsItem

logger = logging.getLogger(__name__)


class NewsCache:
    """
    Simple file-based JSON cache for NormalizedNewsItem lists.

    Cache file path:
        {cache_dir}/{ticker.upper()}_{date}_{lookback_hours}h.json
    """

    def __init__(self, cache_dir: str = "./data/news_cache") -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def _key_path(self, ticker: str, as_of: date, lookback_hours: int) -> Path:
        fname = f"{ticker.upper()}_{as_of.isoformat()}_{lookback_hours}h.json"
        return self._dir / fname

    def get(
        self, ticker: str, as_of: date, lookback_hours: int
    ) -> Optional[List[NormalizedNewsItem]]:
        """
        Return cached articles or None on cache miss.
        Validates that cache file was written on the same calendar date.
        """
        path = self._key_path(ticker, as_of, lookback_hours)
        if not path.exists():
            self.misses += 1
            logger.debug("news_cache.miss ticker=%s date=%s", ticker, as_of)
            return None

        try:
            with open(path) as f:
                payload = json.load(f)

            # Validate cache date matches
            cached_date = payload.get("cache_date")
            if cached_date != as_of.isoformat():
                self.misses += 1
                path.unlink(missing_ok=True)
                logger.debug("news_cache.stale ticker=%s", ticker)
                return None

            articles = [
                NormalizedNewsItem.model_validate(item)
                for item in payload.get("articles", [])
            ]
            self.hits += 1
            logger.debug("news_cache.hit ticker=%s articles=%d", ticker, len(articles))
            return articles

        except Exception as exc:
            logger.warning("news_cache.read_error ticker=%s: %s", ticker, exc)
            self.misses += 1
            return None

    def set(
        self,
        ticker: str,
        as_of: date,
        lookback_hours: int,
        articles: List[NormalizedNewsItem],
    ) -> None:
        """Write articles to cache. Silently ignores write errors."""
        path = self._key_path(ticker, as_of, lookback_hours)
        try:
            payload = {
                "cache_date": as_of.isoformat(),
                "ticker": ticker.upper(),
                "lookback_hours": lookback_hours,
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "articles": [a.model_dump(mode="json") for a in articles],
            }
            with open(path, "w") as f:
                json.dump(payload, f, indent=2, default=str)
            logger.debug("news_cache.write ticker=%s articles=%d", ticker, len(articles))
        except Exception as exc:
            logger.warning("news_cache.write_error ticker=%s: %s", ticker, exc)

    def reset_stats(self) -> None:
        self.hits = 0
        self.misses = 0

    @property
    def hit_ratio(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0
