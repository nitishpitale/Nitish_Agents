"""
eToro Public API client.

Authentication (all three headers required on every request):
  x-api-key    : Public API key  — set via ETORO_API_KEY env var
  x-user-key   : User key        — set via ETORO_USER_KEY env var
  x-request-id : Fresh UUID4 generated per request

Endpoints used:
  Instrument search : GET /api/v1/market-data/search?internalSymbolFull={ticker}
  Current rates     : GET /api/v1/market-data/rates?instrumentIds={id}
  Historical candles: GET /api/v1/market-data/instruments/{id}/candles

Options chains:
  The eToro Public API does not expose an options chain endpoint.
  In production this method raises NotImplementedError; callers fall back
  to MockEToroClient or a supplementary options-data feed.

Ref: https://api-portal.etoro.com/getting-started/authentication
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import httpx

from ..config.settings import EToroConfig
from .models import HistoricalPrices, OptionContract, SpotData

logger = logging.getLogger(__name__)


class EToroClient:
    """
    Live eToro Public API client.

    All three required headers are attached automatically:
      x-api-key, x-user-key, x-request-id (new UUID per request).
    """

    def __init__(self, config: EToroConfig) -> None:
        self._config = config
        self._base_url = config.base_url.rstrip("/")
        self._api_key = config.api_key
        self._user_key = config.user_key
        self._http = httpx.Client(timeout=config.timeout_seconds)
        # Immutable instrument ID cache (IDs never change per eToro docs)
        self._instrument_id_cache: Dict[str, int] = {}

    def _headers(self) -> Dict[str, str]:
        """Build per-request headers with a fresh x-request-id UUID."""
        return {
            "x-api-key": self._api_key,
            "x-user-key": self._user_key,
            "x-request-id": str(uuid.uuid4()),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

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

        Endpoint: GET /api/v1/market-data/search?internalSymbolFull={ticker}
        Instrument IDs are immutable — cached in process memory.
        """
        ticker = ticker.upper()
        if ticker in self._instrument_id_cache:
            return self._instrument_id_cache[ticker]

        url = f"{self._base_url}/api/v1/market-data/search"
        try:
            resp = self._http.get(
                url,
                params={"internalSymbolFull": ticker},
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()

            # Response shape: {"items": [...], "totalItems": N}
            items = data.get("items", data) if isinstance(data, dict) else data
            if isinstance(items, list):
                for inst in items:
                    symbol = inst.get("internalSymbolFull") or inst.get("symbol", "")
                    if symbol.upper() == ticker:
                        inst_id = int(inst["instrumentId"])
                        self._instrument_id_cache[ticker] = inst_id
                        logger.debug("Resolved %s → instrumentId=%d", ticker, inst_id)
                        return inst_id

            logger.warning("Instrument not found for ticker: %s", ticker)
        except Exception as exc:
            logger.warning("get_instrument_id failed for %s: %s", ticker, exc)
        return None

    # ------------------------------------------------------------------
    # Spot prices
    # ------------------------------------------------------------------

    def get_spot(self, ticker: str) -> Optional[SpotData]:
        """
        Retrieve current bid/ask/mid for a ticker.

        Endpoint: GET /api/v1/market-data/rates?instrumentIds={id}
        """
        inst_id = self.get_instrument_id(ticker)
        if inst_id is None:
            logger.error("Cannot fetch spot for %s: instrument ID not found", ticker)
            return None

        url = f"{self._base_url}/api/v1/market-data/rates"
        try:
            resp = self._http.get(
                url,
                params={"instrumentIds": str(inst_id)},
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()

            # Response may be a list or a dict containing a list
            rates = data if isinstance(data, list) else data.get("rates", [data])
            if not rates:
                return None

            rate = rates[0]
            bid = float(rate.get("Bid") or rate.get("bid") or 0)
            ask = float(rate.get("Ask") or rate.get("ask") or 0)
            last = float(rate.get("LastExecution") or rate.get("lastExecution") or 0)
            mid = (bid + ask) / 2.0 if bid and ask else last

            return SpotData(
                ticker=ticker,
                price=mid,
                bid=bid or None,
                ask=ask or None,
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.error("get_spot failed for %s: %s", ticker, exc)
            return None

    # ------------------------------------------------------------------
    # Historical prices
    # ------------------------------------------------------------------

    def get_historical_prices(
        self, ticker: str, lookback_days: int = 120
    ) -> Optional[HistoricalPrices]:
        """
        Retrieve daily closing prices for the past `lookback_days` calendar days.

        Endpoint: GET /api/v1/market-data/instruments/{id}/candles
        Resolution: ONE_DAY
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
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()

            # Response shape varies; look for a candles list
            candles = (
                data.get("candles")
                or data.get("data")
                or (data if isinstance(data, list) else [])
            )

            closes: List[float] = []
            dates: List[date] = []
            for c in sorted(
                candles,
                key=lambda x: x.get("date") or x.get("timestamp") or x.get("time") or "",
            ):
                close_val = c.get("close") or c.get("Close") or c.get("c") or 0
                closes.append(float(close_val))
                raw_date = (c.get("date") or c.get("timestamp") or c.get("time") or "")[:10]
                try:
                    dates.append(date.fromisoformat(raw_date))
                except ValueError:
                    dates.append(datetime.now(timezone.utc).date())

            logger.info("Historical prices: %s — %d candles", ticker, len(closes))
            return HistoricalPrices(ticker=ticker, closes=closes, dates=dates)

        except Exception as exc:
            logger.error("get_historical_prices failed for %s: %s", ticker, exc)
            return None

    # ------------------------------------------------------------------
    # Option chains
    # ------------------------------------------------------------------

    def get_option_chain(self, ticker: str) -> List[OptionContract]:
        """
        The eToro Public API does not expose an options chain endpoint.
        Configure an options data feed or use MockEToroClient for development.
        """
        raise NotImplementedError(
            "eToro Public API does not provide an options chain endpoint. "
            "Set ETORO_USE_MOCK=true or configure an options data provider."
        )
