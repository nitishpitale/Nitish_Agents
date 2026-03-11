"""
Mock eToro client that returns deterministic synthetic data for testing
and development. No network calls — all data is generated from fixtures.

The mock data is designed to produce at least 10 scoreable candidates
across multiple tickers to satisfy integration test requirements.
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from .models import HistoricalPrices, OptionContract, SpotData


# ---------------------------------------------------------------------------
# Deterministic spot prices and vol assumptions for the mock universe
# ---------------------------------------------------------------------------

_MOCK_SPOTS: dict[str, float] = {
    "AAPL": 185.50,
    "MSFT": 415.00,
    "AMZN": 195.00,
    "GOOGL": 175.00,
    "TSLA": 245.00,
    "NVDA": 875.00,
    "META": 555.00,
    "SPY": 510.00,
    "QQQ": 435.00,
    "IWM": 205.00,
}

# Annualised vol regime per ticker (used to build synthetic price history)
_MOCK_VOLS: dict[str, float] = {
    "AAPL": 0.22,
    "MSFT": 0.24,
    "AMZN": 0.28,
    "GOOGL": 0.26,
    "TSLA": 0.55,
    "NVDA": 0.50,
    "META": 0.38,
    "SPY": 0.14,
    "QQQ": 0.17,
    "IWM": 0.18,
}

# Dividend yields
_MOCK_DIVS: dict[str, float] = {
    "AAPL": 0.005,
    "MSFT": 0.007,
    "AMZN": 0.0,
    "GOOGL": 0.0,
    "TSLA": 0.0,
    "NVDA": 0.001,
    "META": 0.004,
    "SPY": 0.013,
    "QQQ": 0.006,
    "IWM": 0.012,
}

# Next earnings dates (used for Phase 2 risk annotation)
_MOCK_EARNINGS: dict[str, Optional[date]] = {
    "AAPL": date(2026, 5, 1),
    "MSFT": date(2026, 4, 24),
    "AMZN": date(2026, 5, 2),
    "GOOGL": date(2026, 4, 29),
    "TSLA": date(2026, 4, 22),
    "NVDA": date(2026, 5, 20),
    "META": date(2026, 4, 30),
    "SPY": None,
    "QQQ": None,
    "IWM": None,
}


def _mock_prices_seed(ticker: str, n_days: int = 130, seed: int = 42) -> List[float]:
    """
    Generate deterministic synthetic price history using geometric Brownian motion.
    Uses a fixed random seed so every call returns identical data.
    """
    spot = _MOCK_SPOTS.get(ticker, 100.0)
    vol = _MOCK_VOLS.get(ticker, 0.25)
    div = _MOCK_DIVS.get(ticker, 0.0)
    r = 0.045
    dt = 1.0 / 252.0

    rng = random.Random(seed + hash(ticker) % 10000)

    prices = [spot]
    # Walk backwards to simulate history: we need prices ENDING at current spot
    # So generate from some past price and normalise to end at spot.
    mu = r - div - 0.5 * vol**2

    # Generate log returns
    log_returns = []
    for _ in range(n_days):
        z = rng.gauss(0, 1)
        log_returns.append(mu * dt + vol * math.sqrt(dt) * z)

    # Reconstruct prices ending at spot
    # Start = spot / exp(sum(log_returns))
    total_log_ret = sum(log_returns)
    start_price = spot / math.exp(total_log_ret)

    prices = [start_price]
    for lr in log_returns:
        prices.append(prices[-1] * math.exp(lr))

    return prices


def _market_iv_offset(ticker: str, strike_moneyness: float) -> float:
    """
    Apply a vol surface smile / skew to market-implied IVs.
    Moneyness = K/S.  Skew: OTM puts have higher IV (put skew).
    This creates realistic mispricing opportunities in the mock data.
    """
    base_vol = _MOCK_VOLS.get(ticker, 0.25)
    # Skew: add 2% per 5% OTM move downward, subtract 1% upward
    if strike_moneyness < 1.0:
        skew = (1.0 - strike_moneyness) * 0.25
    else:
        skew = -(strike_moneyness - 1.0) * 0.10

    # Add a random (but deterministic) vol offset to create mispricing
    rng = random.Random(int(abs(strike_moneyness * 1000)) + hash(ticker) % 999)
    noise = rng.gauss(0, 0.02)

    return max(base_vol + skew + noise, 0.05)


class MockEToroClient:
    """
    Deterministic mock replacement for EToroClient.
    Returns synthetic but self-consistent market data.
    """

    def __init__(self, reference_date: Optional[date] = None, seed: int = 42) -> None:
        self._ref_date = reference_date or date(2026, 3, 11)
        self._seed = seed
        self._universe = list(_MOCK_SPOTS.keys())

    # ------------------------------------------------------------------
    # Spot prices
    # ------------------------------------------------------------------

    def get_spot(self, ticker: str) -> Optional[SpotData]:
        if ticker not in _MOCK_SPOTS:
            return None
        spot = _MOCK_SPOTS[ticker]
        spread = spot * 0.0002  # 2 bps spread on underlying
        return SpotData(
            ticker=ticker,
            price=spot,
            bid=round(spot - spread / 2, 4),
            ask=round(spot + spread / 2, 4),
            dividend_yield=_MOCK_DIVS.get(ticker, 0.0),
            timestamp=datetime(
                self._ref_date.year,
                self._ref_date.month,
                self._ref_date.day,
                16, 0, 0,
                tzinfo=timezone.utc,
            ),
        )

    # ------------------------------------------------------------------
    # Historical prices
    # ------------------------------------------------------------------

    def get_historical_prices(
        self, ticker: str, lookback_days: int = 120
    ) -> Optional[HistoricalPrices]:
        if ticker not in _MOCK_SPOTS:
            return None

        prices = _mock_prices_seed(ticker, n_days=lookback_days + 5, seed=self._seed)

        dates: List[date] = []
        cur = self._ref_date - timedelta(days=len(prices) - 1)
        for _ in prices:
            dates.append(cur)
            cur += timedelta(days=1)

        return HistoricalPrices(ticker=ticker, closes=prices, dates=dates)

    # ------------------------------------------------------------------
    # Option chains
    # ------------------------------------------------------------------

    def get_option_chain(self, ticker: str) -> List[OptionContract]:
        """
        Generate a synthetic option chain for the given ticker.
        Creates contracts across 3 expiry cycles and multiple strikes.
        """
        if ticker not in _MOCK_SPOTS:
            return []

        spot = _MOCK_SPOTS[ticker]
        div = _MOCK_DIVS.get(ticker, 0.0)
        r = 0.045

        # 3 expiry cycles: ~30d, ~60d, ~90d from reference date
        expiry_offsets = [28, 56, 91]
        contracts: List[OptionContract] = []

        for offset in expiry_offsets:
            expiry = self._ref_date + timedelta(days=offset)
            T = offset / 365.0

            # Strikes: -20% to +20% from spot in ~5% increments
            moneyness_levels = [0.80, 0.85, 0.90, 0.925, 0.95, 0.975, 1.0,
                                 1.025, 1.05, 1.075, 1.10, 1.15, 1.20]

            for moneyness in moneyness_levels:
                K = round(spot * moneyness, 1)
                market_iv = _market_iv_offset(ticker, moneyness)

                # Compute BSM mid price at market IV
                from ..modeling.black_scholes import bs_price
                for opt_type in ("C", "P"):
                    # Skip deep OTM contracts (price < 0.05)
                    mid_price = bs_price(spot, K, T, market_iv, r, div, opt_type)  # type: ignore[arg-type]
                    if mid_price < 0.05:
                        continue

                    # Synthetic bid/ask with realistic spread
                    # Spread widens for OTM, low volume
                    otm_factor = abs(math.log(spot / K))
                    spread_pct = 0.03 + otm_factor * 0.15
                    spread_pct = min(spread_pct, 0.25)  # cap at 25%

                    half_spread = mid_price * spread_pct / 2.0
                    bid = max(round(mid_price - half_spread, 2), 0.01)
                    ask = round(mid_price + half_spread, 2)

                    # Volume and OI: ATM options are more liquid
                    base_volume = 1000 if abs(moneyness - 1.0) < 0.05 else 300
                    rng = random.Random(
                        hash(f"{ticker}{expiry}{K}{opt_type}") % 99999 + self._seed
                    )
                    volume = int(base_volume * rng.uniform(0.3, 2.5))
                    oi = int(volume * rng.uniform(3, 12))

                    contracts.append(
                        OptionContract(
                            ticker=ticker,
                            expiry=expiry,
                            strike=K,
                            option_type=opt_type,  # type: ignore[arg-type]
                            bid=bid,
                            ask=ask,
                            volume=volume,
                            open_interest=oi,
                            spot=spot,
                            dividend_yield=div,
                            quote_time=datetime(
                                self._ref_date.year,
                                self._ref_date.month,
                                self._ref_date.day,
                                16, 0, 0,
                                tzinfo=timezone.utc,
                            ),
                        )
                    )

        return contracts

    # ------------------------------------------------------------------
    # Earnings (Phase 2)
    # ------------------------------------------------------------------

    def get_next_earnings_date(self, ticker: str) -> Optional[date]:
        return _MOCK_EARNINGS.get(ticker)

    # ------------------------------------------------------------------
    # Momentum (Phase 2) — 20d return percentile vs peers
    # ------------------------------------------------------------------

    def get_momentum_percentile(self, ticker: str) -> Optional[float]:
        """Return 20-day return percentile vs universe (0.0 to 1.0)."""
        prices = _mock_prices_seed(ticker, n_days=25, seed=self._seed)
        if len(prices) < 21:
            return None
        ret = (prices[-1] - prices[-21]) / prices[-21]

        # Compute returns for all tickers and rank
        all_rets = {}
        for t in self._universe:
            p = _mock_prices_seed(t, n_days=25, seed=self._seed)
            if len(p) >= 21:
                all_rets[t] = (p[-1] - p[-21]) / p[-21]

        if not all_rets:
            return 0.5

        sorted_rets = sorted(all_rets.values())
        rank = sorted_rets.index(all_rets.get(ticker, ret))
        return rank / max(len(sorted_rets) - 1, 1)
