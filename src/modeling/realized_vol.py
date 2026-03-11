"""
Realized volatility forecasting functions.

All functions are DETERMINISTIC — no LLM involvement.

Inputs are sequences of daily closing prices (or returns).
Outputs are annualised volatility estimates.

Annualisation: multiply daily vol by sqrt(252) — 252 trading days per year.
"""

from __future__ import annotations

import math
from typing import List, Sequence

TRADING_DAYS_PER_YEAR = 252


def _log_returns(prices: Sequence[float]) -> List[float]:
    """Compute log returns from a price series."""
    if len(prices) < 2:
        return []
    return [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]


def historical_vol(prices: Sequence[float], window: int) -> float | None:
    """
    Compute historical (close-to-close) realised volatility over the last
    `window` trading days, annualised.

    Args:
        prices: Sequence of daily closing prices, oldest first.
        window: Rolling window in trading days (e.g. 20 or 60).

    Returns:
        Annualised volatility, or None if there is insufficient data.
    """
    if len(prices) < window + 1:
        return None

    window_prices = list(prices[-(window + 1) :])
    returns = _log_returns(window_prices)

    n = len(returns)
    if n < 2:
        return None

    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)  # unbiased
    return math.sqrt(variance * TRADING_DAYS_PER_YEAR)


def ewma_vol(
    prices: Sequence[float],
    lam: float = 0.94,
    min_obs: int = 10,
) -> float | None:
    """
    Exponentially weighted moving average volatility (RiskMetrics style).

    Variance_{t} = λ * Variance_{t-1} + (1-λ) * r_{t}^2

    Args:
        prices: Sequence of daily closing prices, oldest first.
        lam: EWMA decay factor (default 0.94, RiskMetrics standard).
        min_obs: Minimum observations required.

    Returns:
        Annualised EWMA volatility, or None if insufficient data.
    """
    if len(prices) < min_obs + 1:
        return None

    returns = _log_returns(list(prices))
    if len(returns) < min_obs:
        return None

    # Initialise with the variance of the first 10 returns
    init_n = min(10, len(returns))
    init_mean = sum(returns[:init_n]) / init_n
    var = sum((r - init_mean) ** 2 for r in returns[:init_n]) / init_n

    for r in returns[init_n:]:
        var = lam * var + (1.0 - lam) * r**2

    return math.sqrt(var * TRADING_DAYS_PER_YEAR)


def blend_sigma_hat(
    prices: Sequence[float],
    T_years: float,
    window_20d: int = 20,
    window_60d: int = 60,
    ewma_lambda: float = 0.94,
    weights: tuple[float, float, float] = (0.25, 0.25, 0.50),
) -> float | None:
    """
    Blend 20-day, 60-day, and EWMA volatility estimates into a single
    forward-looking forecast sigma_hat_T for option expiry at T_years.

    Weights default to [0.25, 0.25, 0.50] (EWMA-heavy for recency).

    Args:
        prices: Daily closing prices, oldest first.
        T_years: Option tenor in years (used for tenor-adjustment placeholder).
        window_20d: Window for 20-day historical vol.
        window_60d: Window for 60-day historical vol.
        ewma_lambda: EWMA decay parameter.
        weights: (w_20d, w_60d, w_ewma) — must sum to 1.0.

    Returns:
        Blended annualised volatility forecast, or None if insufficient data.
    """
    w20, w60, wewma = weights
    if abs(w20 + w60 + wewma - 1.0) > 1e-6:
        raise ValueError(f"Blend weights must sum to 1.0, got {w20+w60+wewma:.4f}")

    vol20 = historical_vol(prices, window_20d)
    vol60 = historical_vol(prices, window_60d)
    ewma = ewma_vol(prices, lam=ewma_lambda)

    available = [(v, w) for v, w in [(vol20, w20), (vol60, w60), (ewma, wewma)] if v is not None]

    if not available:
        return None

    # Re-normalise weights for available components
    total_weight = sum(w for _, w in available)
    blended = sum(v * (w / total_weight) for v, w in available)

    return blended
