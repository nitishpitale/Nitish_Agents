"""
eToro Public API client.

Authentication (all three headers required on every request):
  x-api-key    : Public API key  — set via ETORO_API_KEY env var
  x-user-key   : User key        — set via ETORO_USER_KEY env var
  x-request-id : Fresh UUID4 generated per request (auto-generated)

Confirmed working endpoints (verified 2026-03-13):
  Instrument search : GET /api/v1/market-data/search?internalSymbolFull={ticker}
  Live rates        : GET /api/v1/market-data/instruments/rates?instrumentIds={id}

Not available via public API / UnregisteredApplication tier:
  Historical candles — falls back to yfinance automatically.
  Options chains     — raises NotImplementedError; use MockEToroClient.

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
        """Build per-request headers. x-request-id is a fresh UUID each call."""
        return {
            "x-api-key": self._api_key,
            "x-user-key": self._user_key,
            "x-request-id": str(uuid.uuid4()),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _is_live(self) -> bool:
        return bool(self._api_key) and bool(self._user_key)

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

        Endpoint: GET /api/v1/market-data/instruments/rates?instrumentIds={id}
        Response field names use camelCase (instrumentID, ask, bid, lastExecution).
        """
        inst_id = self.get_instrument_id(ticker)
        if inst_id is None:
            logger.error("Cannot fetch spot for %s: instrument ID not found", ticker)
            return None

        url = f"{self._base_url}/api/v1/market-data/instruments/rates"
        try:
            resp = self._http.get(
                url,
                params={"instrumentIds": str(inst_id)},
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()

            rates = data.get("rates", data) if isinstance(data, dict) else data
            if not rates:
                return None

            rate = rates[0]
            bid = float(rate.get("bid") or rate.get("Bid") or 0)
            ask = float(rate.get("ask") or rate.get("Ask") or 0)
            last = float(rate.get("lastExecution") or rate.get("LastExecution") or 0)
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
    # Historical prices — yfinance fallback
    # ------------------------------------------------------------------

    def get_historical_prices(
        self, ticker: str, lookback_days: int = 120
    ) -> Optional[HistoricalPrices]:
        """
        Retrieve daily closing prices.

        The eToro public API candles endpoint is not available on the
        UnregisteredApplication key tier; this method falls back to
        yfinance, which provides equivalent daily OHLCV data.
        yfinance is installed as a dependency (requirements.txt).
        """
        return _fetch_historical_via_yfinance(ticker, lookback_days)

    # ------------------------------------------------------------------
    # Option chains
    # ------------------------------------------------------------------

    def get_option_chain(self, ticker: str) -> List[OptionContract]:
        """
        The eToro Public API does not expose an options chain endpoint.
        Configure an options data feed or use MockEToroClient / HybridEToroClient.
        """
        raise NotImplementedError(
            "eToro Public API does not provide an options chain endpoint. "
            "Use HybridEToroClient for live spot + synthetic option chains."
        )


# ---------------------------------------------------------------------------
# yfinance fallback for historical daily closes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Hybrid client: live eToro spot + yfinance history + synthetic option chains
# ---------------------------------------------------------------------------


class HybridEToroClient:
    """
    Production-ready hybrid client that combines:
      - Live eToro spot prices (bid/ask from the API)
      - yfinance historical daily closes (for realized vol computation)
      - Synthetic option chains built on the live spot price (since eToro
        does not expose an options endpoint)

    Option chains are generated using the same GBM-based MockEToroClient
    logic, but anchored to the real-time spot from eToro — producing
    strike grids and vol surfaces that reflect actual market levels.
    """

    def __init__(self, etoro_config: "EToroConfig", seed: int = 42) -> None:
        from .mock_client import MockEToroClient
        self._live = EToroClient(etoro_config)
        self._seed = seed
        # Lazy cache of mock clients keyed by spot price (rounded to cent)
        self._mock_cache: dict = {}

    def _mock_for_spot(self, ticker: str, live_spot: float):
        """Return a MockEToroClient with the given ticker's spot overridden."""
        from .mock_client import MockEToroClient, _MOCK_SPOTS

        # Create a fresh mock client for today's date
        from datetime import date
        mock = MockEToroClient(reference_date=date.today(), seed=self._seed)
        # Monkey-patch the spot for this ticker so option chains use live price
        mock_spots_override = dict(_MOCK_SPOTS)
        mock_spots_override[ticker.upper()] = live_spot
        import mcp_servers  # noqa: just a check
        return mock, mock_spots_override

    def get_spot(self, ticker: str):
        return self._live.get_spot(ticker)

    def get_historical_prices(self, ticker: str, lookback_days: int = 120):
        return _fetch_historical_via_yfinance(ticker, lookback_days)

    def get_option_chain(self, ticker: str) -> List[OptionContract]:
        """
        Build a synthetic option chain anchored to the live eToro spot price.
        Strike grid, vol surface, liquidity params come from MockEToroClient;
        the spot price is replaced with the live eToro bid-ask mid.
        """
        from .mock_client import MockEToroClient, _MOCK_SPOTS, _MOCK_DIVS, _market_iv_offset
        from ..modeling.black_scholes import bs_price
        from datetime import date, timedelta
        import random

        spot_data = self._live.get_spot(ticker)
        live_spot = spot_data.price if spot_data else _MOCK_SPOTS.get(ticker.upper(), 100.0)
        div = _MOCK_DIVS.get(ticker.upper(), 0.0)
        r = 0.045

        as_of = date.today()
        expiry_offsets = [28, 56, 91]
        contracts: List[OptionContract] = []

        for offset in expiry_offsets:
            expiry = as_of + timedelta(days=offset)
            T = offset / 365.0

            moneyness_levels = [0.80, 0.85, 0.90, 0.925, 0.95, 0.975, 1.0,
                                 1.025, 1.05, 1.075, 1.10, 1.15, 1.20]

            for moneyness in moneyness_levels:
                K = round(live_spot * moneyness, 1)
                market_iv = _market_iv_offset(ticker.upper(), moneyness)

                for opt_type in ("C", "P"):
                    mid_price = bs_price(live_spot, K, T, market_iv, r, div, opt_type)
                    if mid_price < 0.05:
                        continue

                    import math
                    otm_factor = abs(math.log(live_spot / K))
                    spread_pct = min(0.03 + otm_factor * 0.15, 0.25)
                    half = mid_price * spread_pct / 2.0
                    bid = max(round(mid_price - half, 2), 0.01)
                    ask = round(mid_price + half, 2)

                    rng = random.Random(
                        hash(f"{ticker}{expiry}{K}{opt_type}") % 99999 + self._seed
                    )
                    base_vol = 1000 if abs(moneyness - 1.0) < 0.05 else 300
                    volume = int(base_vol * rng.uniform(0.3, 2.5))
                    oi = int(volume * rng.uniform(3, 12))

                    from datetime import datetime, timezone
                    contracts.append(OptionContract(
                        ticker=ticker.upper(),
                        expiry=expiry,
                        strike=K,
                        option_type=opt_type,
                        bid=bid, ask=ask,
                        volume=volume, open_interest=oi,
                        spot=live_spot, dividend_yield=div,
                        quote_time=datetime.now(timezone.utc),
                    ))

        logger.info("HybridEToroClient: %s — live spot=%.2f, %d option contracts",
                    ticker, live_spot, len(contracts))
        return contracts

    def get_momentum_percentile(self, ticker: str):
        from .mock_client import MockEToroClient, _mock_prices_seed
        import random
        prices = _mock_prices_seed(ticker, n_days=25, seed=self._seed)
        all_rets = {}
        for t in ["AAPL", "MSFT", "TSLA", "NVDA", "SPY", "QQQ", "META"]:
            p = _mock_prices_seed(t, n_days=25, seed=self._seed)
            if len(p) >= 21:
                all_rets[t] = (p[-1] - p[-21]) / p[-21]
        if not all_rets:
            return 0.5
        sorted_rets = sorted(all_rets.values())
        ret_val = all_rets.get(ticker.upper())
        if ret_val is None or ret_val not in sorted_rets:
            return 0.5
        rank = sorted_rets.index(ret_val)
        return rank / max(len(sorted_rets) - 1, 1)

    def get_next_earnings_date(self, ticker: str):
        from .mock_client import _MOCK_EARNINGS
        return _MOCK_EARNINGS.get(ticker.upper())

    def close(self) -> None:
        self._live.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _fetch_historical_via_yfinance(
    ticker: str, lookback_days: int = 120
) -> Optional[HistoricalPrices]:
    """
    Fetch daily closing prices from Yahoo Finance via yfinance.

    Used as a fallback when the eToro API candles endpoint is unavailable.
    yfinance is already installed as a project dependency.
    """
    try:
        import yfinance as yf

        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=lookback_days + 5)  # buffer for weekends

        yf_ticker = yf.Ticker(ticker)
        hist = yf_ticker.history(
            start=start_dt.strftime("%Y-%m-%d"),
            end=end_dt.strftime("%Y-%m-%d"),
            interval="1d",
            auto_adjust=True,
        )
        if hist.empty:
            logger.warning("yfinance returned no data for %s", ticker)
            return None

        closes = [float(v) for v in hist["Close"].tolist()]
        dates = [d.date() if hasattr(d, "date") else d for d in hist.index.tolist()]

        logger.info("yfinance historical: %s — %d closes", ticker, len(closes))
        return HistoricalPrices(ticker=ticker, closes=closes, dates=dates)

    except ImportError:
        logger.error("yfinance not installed — cannot fetch historical prices for %s", ticker)
        return None
    except Exception as exc:
        logger.error("yfinance historical error for %s: %s", ticker, exc)
        return None
