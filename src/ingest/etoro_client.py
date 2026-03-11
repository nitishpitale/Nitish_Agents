"""
eToro MCP API client for market data ingestion.

Fetches:
  - Current spot prices and bid/ask via /market-data/rates
  - Historical OHLCV candles via /market-data/instruments/{id}/candles
  - Instrument metadata via /market-data/instruments/search

Note: eToro's public API does not expose a dedicated options chain endpoint.
Option chain data is synthesised from available endpoints or sourced via
a complementary options data feed configured in config.yaml.
The mock client (MockEToroClient) provides synthetic option chain data for
integration testing and development without live credentials.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import httpx

from ..config.settings import EToroConfig
from .models import HistoricalPrices, OptionContract, SpotData

logger = logging.getLogger(__name__)


class EToroClient:
    """
    Live eToro API client.

    Uses the eToro Public API endpoints to retrieve:
      - Instrument IDs (cached)
      - Current market rates (bid/ask/spot)
      - Historical daily closes for realized vol computation
      - Option chain data (from options feed if configured)
    """

    def __init__(self, config: EToroConfig) -> None:
        self._config = config
        self._base_url = config.base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._http = httpx.Client(
            timeout=config.timeout_seconds,
            headers=self._headers,
        )
        # Simple in-process cache for instrument IDs (immutable per eToro docs)
        self._instrument_id_cache: Dict[str, int] = {}

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "EToroClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Instrument resolution
    # ------------------------------------------------------------------

    def get_instrument_id(self, ticker: str) -> Optional[int]:
        """
        Resolve a ticker symbol to an eToro instrumentId.
        Results are cached — IDs are immutable per eToro documentation.
        """
        if ticker in self._instrument_id_cache:
            return self._instrument_id_cache[ticker]

        url = f"{self._base_url}/api/v1/market-data/instruments/search"
        try:
            resp = self._http.get(url, params={"query": ticker, "limit": 10})
            resp.raise_for_status()
            data = resp.json()
            instruments = data.get("instruments", data) if isinstance(data, dict) else data
            for inst in instruments:
                symbol = inst.get("internalSymbolFull") or inst.get("symbol") or ""
                if symbol.upper() == ticker.upper():
                    inst_id = int(inst["instrumentId"])
                    self._instrument_id_cache[ticker] = inst_id
                    return inst_id
        except Exception as exc:
            logger.warning("Failed to resolve instrument ID for %s: %s", ticker, exc)
        return None

    # ------------------------------------------------------------------
    # Spot prices
    # ------------------------------------------------------------------

    def get_spot(self, ticker: str) -> Optional[SpotData]:
        """
        Retrieve current spot price, bid, and ask for a ticker.
        Maps to GET /api/v1/market-data/rates?instrumentIds=...
        """
        inst_id = self.get_instrument_id(ticker)
        if inst_id is None:
            logger.error("Cannot get spot for %s — instrument ID not found", ticker)
            return None

        url = f"{self._base_url}/api/v1/market-data/rates"
        try:
            resp = self._http.get(url, params={"instrumentIds": str(inst_id)})
            resp.raise_for_status()
            rates = resp.json()
            rate = rates[0] if isinstance(rates, list) else rates
            bid = float(rate.get("Bid") or rate.get("bid") or 0)
            ask = float(rate.get("Ask") or rate.get("ask") or 0)
            mid = (bid + ask) / 2.0 if bid and ask else float(rate.get("LastExecution", 0))
            return SpotData(
                ticker=ticker,
                price=mid,
                bid=bid,
                ask=ask,
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.error("Failed to get spot for %s: %s", ticker, exc)
            return None

    # ------------------------------------------------------------------
    # Historical prices
    # ------------------------------------------------------------------

    def get_historical_prices(
        self, ticker: str, lookback_days: int = 120
    ) -> Optional[HistoricalPrices]:
        """
        Retrieve daily closing prices for the last `lookback_days` calendar days.
        Maps to GET /api/v1/market-data/instruments/{id}/candles
        """
        inst_id = self.get_instrument_id(ticker)
        if inst_id is None:
            return None

        url = f"{self._base_url}/api/v1/market-data/instruments/{inst_id}/candles"
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=lookback_days)

        try:
            resp = self._http.get(
                url,
                params={
                    "resolution": "ONE_DAY",
                    "fromDate": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "toDate": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            candles = data.get("candles", data) if isinstance(data, dict) else data

            closes: List[float] = []
            dates: List[date] = []
            for c in sorted(candles, key=lambda x: x.get("date", x.get("timestamp", ""))):
                closes.append(float(c.get("close", c.get("Close", 0))))
                raw_date = c.get("date", c.get("timestamp", ""))[:10]
                dates.append(date.fromisoformat(raw_date))

            return HistoricalPrices(ticker=ticker, closes=closes, dates=dates)
        except Exception as exc:
            logger.error("Failed to get historical prices for %s: %s", ticker, exc)
            return None

    # ------------------------------------------------------------------
    # Option chains
    # ------------------------------------------------------------------

    def get_option_chain(self, ticker: str) -> List[OptionContract]:
        """
        Retrieve option chain for a ticker.

        The eToro Public API does not expose a dedicated options endpoint.
        In production, this would integrate with an options data provider
        (e.g., a supplementary options feed). For now this raises NotImplementedError
        so callers fall back to the mock client in non-production environments.
        """
        raise NotImplementedError(
            "eToro Public API does not provide an options chain endpoint. "
            "Configure an options data feed or use MockEToroClient for development."
        )
