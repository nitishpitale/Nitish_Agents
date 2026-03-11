"""
Black-Scholes-Merton (BSM) pricing, Greeks, and implied volatility solver.

All functions are DETERMINISTIC — no LLM involvement.

Conventions:
  - T : time to expiry in years  (e.g. 30 days → 30/365)
  - sigma : annualised volatility (0.25 = 25 %)
  - r : continuously compounded risk-free rate
  - q : continuous dividend yield (0 if unknown)
  - option_type : "C" (call) or "P" (put)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

# Minimum time-to-expiry to avoid division by zero (≈ 1 minute)
_MIN_T = 1.0 / (365 * 24 * 60)
# Minimum sigma to avoid degenerate solutions
_MIN_SIGMA = 1e-8
# IV solver tolerances
_IV_TOLERANCE = 1e-7
_IV_MAX_ITER_NR = 100   # Newton-Raphson iterations
_IV_MAX_ITER_BS = 200   # Bisection fallback iterations
_IV_SIGMA_LOW = 1e-6
_IV_SIGMA_HIGH = 20.0   # 2000 % vol upper bound


OptionType = Literal["C", "P"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _d1_d2(
    S: float, K: float, T: float, sigma: float, r: float, q: float
) -> tuple[float, float]:
    """Compute d1 and d2 for BSM formula."""
    T = max(T, _MIN_T)
    sigma = max(sigma, _MIN_SIGMA)
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return d1, d2


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# ---------------------------------------------------------------------------
# Public pricing and Greeks
# ---------------------------------------------------------------------------


def bs_price(
    S: float,
    K: float,
    T: float,
    sigma: float,
    r: float,
    q: float = 0.0,
    option_type: OptionType = "C",
) -> float:
    """
    Black-Scholes-Merton option price.

    Args:
        S: Spot price of the underlying.
        K: Strike price.
        T: Time to expiry in years.
        sigma: Annualised volatility.
        r: Continuously compounded risk-free rate.
        q: Continuous dividend yield (default 0).
        option_type: "C" for call, "P" for put.

    Returns:
        Theoretical option price. Returns intrinsic value when T is tiny.
    """
    if T <= 0:
        # At / after expiry — return intrinsic value
        if option_type == "C":
            return max(S - K, 0.0)
        return max(K - S, 0.0)

    d1, d2 = _d1_d2(S, K, T, sigma, r, q)
    disc = math.exp(-r * T)
    disc_q = math.exp(-q * T)

    if option_type == "C":
        return S * disc_q * _norm_cdf(d1) - K * disc * _norm_cdf(d2)
    else:
        return K * disc * _norm_cdf(-d2) - S * disc_q * _norm_cdf(-d1)


def bs_delta(
    S: float,
    K: float,
    T: float,
    sigma: float,
    r: float,
    q: float = 0.0,
    option_type: OptionType = "C",
) -> float:
    """
    BSM delta: dV/dS.

    Returns:
        Delta in [-1, 1].
    """
    if T <= 0:
        if option_type == "C":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0

    d1, _ = _d1_d2(S, K, T, sigma, r, q)
    disc_q = math.exp(-q * T)

    if option_type == "C":
        return disc_q * _norm_cdf(d1)
    return disc_q * (_norm_cdf(d1) - 1.0)


def bs_vega(
    S: float,
    K: float,
    T: float,
    sigma: float,
    r: float,
    q: float = 0.0,
) -> float:
    """
    BSM vega: dV/d_sigma.

    Same for calls and puts.
    Returns vega per 1 unit of sigma (not per 1 % move — caller divides by 100 if desired).
    """
    if T <= 0:
        return 0.0
    d1, _ = _d1_d2(S, K, T, sigma, r, q)
    disc_q = math.exp(-q * T)
    return S * disc_q * _norm_pdf(d1) * math.sqrt(T)


def bs_theta(
    S: float,
    K: float,
    T: float,
    sigma: float,
    r: float,
    q: float = 0.0,
    option_type: OptionType = "C",
) -> float:
    """
    BSM theta: dV/dt (per calendar year).

    Theta is typically negative (time decay).
    Returns theta in price units per year; divide by 365 for daily.
    """
    if T <= 0:
        return 0.0

    d1, d2 = _d1_d2(S, K, T, sigma, r, q)
    disc = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    sqrt_T = math.sqrt(T)

    term1 = -S * disc_q * _norm_pdf(d1) * sigma / (2.0 * sqrt_T)

    if option_type == "C":
        term2 = -r * K * disc * _norm_cdf(d2)
        term3 = q * S * disc_q * _norm_cdf(d1)
    else:
        term2 = r * K * disc * _norm_cdf(-d2)
        term3 = -q * S * disc_q * _norm_cdf(-d1)

    return term1 + term2 + term3


@dataclass
class Greeks:
    """Container for option Greeks."""
    delta: float
    vega: float
    theta: float  # per year


def bs_greeks(
    S: float,
    K: float,
    T: float,
    sigma: float,
    r: float,
    q: float = 0.0,
    option_type: OptionType = "C",
) -> Greeks:
    """Compute all required Greeks in one call."""
    return Greeks(
        delta=bs_delta(S, K, T, sigma, r, q, option_type),
        vega=bs_vega(S, K, T, sigma, r, q),
        theta=bs_theta(S, K, T, sigma, r, q, option_type),
    )


# ---------------------------------------------------------------------------
# Implied Volatility Solver
# ---------------------------------------------------------------------------


def implied_volatility(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    q: float = 0.0,
    option_type: OptionType = "C",
) -> float | None:
    """
    Compute implied volatility via Newton-Raphson with bisection fallback.

    Args:
        market_price: Observed market option price (mid = (bid+ask)/2).
        S: Spot price.
        K: Strike price.
        T: Time to expiry in years.
        r: Risk-free rate.
        q: Dividend yield.
        option_type: "C" or "P".

    Returns:
        Implied volatility as a decimal (e.g. 0.25 = 25 %), or None if
        the solver cannot converge (e.g. price is below intrinsic value).
    """
    if T <= 0:
        return None

    # Validate price vs intrinsic value (lower bound)
    disc = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    if option_type == "C":
        intrinsic = max(S * disc_q - K * disc, 0.0)
        upper_bound = S * disc_q  # call price ≤ forward price
    else:
        intrinsic = max(K * disc - S * disc_q, 0.0)
        upper_bound = K * disc

    if market_price < intrinsic - _IV_TOLERANCE:
        return None
    if market_price > upper_bound + _IV_TOLERANCE:
        return None

    # Clamp market_price to valid range to avoid log(0) issues
    market_price = max(market_price, intrinsic + 1e-12)

    # --- Phase 1: Newton-Raphson starting from Brenner-Subrahmanyam approximation ---
    sigma = _brenner_subrahmanyam_init(market_price, S, K, T, r, q)

    for _ in range(_IV_MAX_ITER_NR):
        price = bs_price(S, K, T, sigma, r, q, option_type)
        vega = bs_vega(S, K, T, sigma, r, q)

        diff = price - market_price
        if abs(diff) < _IV_TOLERANCE:
            return sigma

        if abs(vega) < 1e-12:
            break  # vega near zero — fall through to bisection

        sigma = sigma - diff / vega
        if sigma < _IV_SIGMA_LOW or sigma > _IV_SIGMA_HIGH:
            break  # out of bounds — fall through to bisection

    # --- Phase 2: Bisection fallback ---
    lo, hi = _IV_SIGMA_LOW, _IV_SIGMA_HIGH

    price_lo = bs_price(S, K, T, lo, r, q, option_type)
    price_hi = bs_price(S, K, T, hi, r, q, option_type)

    if (price_lo - market_price) * (price_hi - market_price) > 0:
        return None  # market_price outside [price_lo, price_hi]

    for _ in range(_IV_MAX_ITER_BS):
        mid_sigma = 0.5 * (lo + hi)
        price_mid = bs_price(S, K, T, mid_sigma, r, q, option_type)
        diff = price_mid - market_price

        if abs(diff) < _IV_TOLERANCE:
            return mid_sigma

        if (price_lo - market_price) * diff <= 0:
            hi = mid_sigma
            price_hi = price_mid
        else:
            lo = mid_sigma
            price_lo = price_mid

    # Return best estimate even if not fully converged
    return 0.5 * (lo + hi)


def _brenner_subrahmanyam_init(
    market_price: float, S: float, K: float, T: float, r: float, q: float
) -> float:
    """
    Brenner-Subrahmanyam (1988) approximation for ATM options.
    Used as initial guess for Newton-Raphson.
    """
    forward = S * math.exp((r - q) * T)
    # Approximate: price ≈ (F + K) / 2 * sigma * sqrt(2*pi/T) * 0.5
    atm_approx = (forward + K) / 2.0
    if atm_approx <= 0 or T <= 0:
        return 0.20  # safe default
    sigma_guess = math.sqrt(2.0 * math.pi / T) * market_price / atm_approx
    return max(min(sigma_guess, _IV_SIGMA_HIGH), _IV_SIGMA_LOW)
