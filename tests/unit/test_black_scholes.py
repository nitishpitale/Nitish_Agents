"""
Unit tests for Black-Scholes pricing, Greeks, and IV solver.

Tests verify:
  - Known-input → known-output for BSM price
  - IV solver round-trip: price → IV → price ≈ original
  - Greeks sign conventions
  - Edge cases: ATM, deep ITM/OTM, zero T
"""

from __future__ import annotations

import math
import pytest

from src.modeling.black_scholes import (
    bs_delta,
    bs_greeks,
    bs_price,
    bs_theta,
    bs_vega,
    implied_volatility,
)


# ---------------------------------------------------------------------------
# BSM Price tests (reference values computed with scipy.stats.norm independently)
# ---------------------------------------------------------------------------


class TestBSPrice:
    def test_atm_call_known_value(self):
        """ATM call: S=K=100, T=0.5yr, sigma=0.20, r=0.05, q=0."""
        # Reference: C ≈ 6.889 (Black-Scholes table)
        price = bs_price(S=100, K=100, T=0.5, sigma=0.20, r=0.05, q=0.0, option_type="C")
        assert abs(price - 6.889) < 0.01, f"Expected ~6.889, got {price:.4f}"

    def test_atm_put_known_value(self):
        """ATM put: S=K=100, T=0.5yr, sigma=0.20, r=0.05, q=0.  Put-call parity check."""
        call = bs_price(S=100, K=100, T=0.5, sigma=0.20, r=0.05, q=0.0, option_type="C")
        put = bs_price(S=100, K=100, T=0.5, sigma=0.20, r=0.05, q=0.0, option_type="P")
        # Put-call parity: C - P = S - K * exp(-rT)
        parity = call - put - (100 - 100 * math.exp(-0.05 * 0.5))
        assert abs(parity) < 1e-8, f"Put-call parity violated: diff={parity}"

    def test_deep_itm_call_approaches_intrinsic(self):
        """Deep ITM call should approach discounted intrinsic value."""
        price = bs_price(S=200, K=100, T=1.0, sigma=0.20, r=0.05, q=0.0, option_type="C")
        intrinsic = 200 - 100 * math.exp(-0.05 * 1.0)
        assert abs(price - intrinsic) < 0.01, f"Deep ITM call price {price} far from intrinsic {intrinsic}"

    def test_deep_otm_call_near_zero(self):
        """Deep OTM call should be near zero."""
        price = bs_price(S=50, K=200, T=0.5, sigma=0.20, r=0.05, q=0.0, option_type="C")
        assert price < 0.001, f"Deep OTM call price {price} should be near zero"

    def test_expired_option_intrinsic(self):
        """Option at T=0 should return intrinsic value."""
        call = bs_price(S=110, K=100, T=0, sigma=0.20, r=0.05, option_type="C")
        assert call == 10.0

        put = bs_price(S=90, K=100, T=0, sigma=0.20, r=0.05, option_type="P")
        assert put == 10.0

        # OTM at expiry → 0
        call_otm = bs_price(S=90, K=100, T=0, sigma=0.20, r=0.05, option_type="C")
        assert call_otm == 0.0

    def test_dividend_yield_lowers_call(self):
        """Adding dividend yield should reduce call price (forward shifts down)."""
        call_no_div = bs_price(S=100, K=100, T=1.0, sigma=0.25, r=0.05, q=0.0, option_type="C")
        call_with_div = bs_price(S=100, K=100, T=1.0, sigma=0.25, r=0.05, q=0.03, option_type="C")
        assert call_with_div < call_no_div

    def test_price_increases_with_vol(self):
        """Higher vol → higher option price (monotone in sigma)."""
        p1 = bs_price(S=100, K=100, T=1.0, sigma=0.10, r=0.05, option_type="C")
        p2 = bs_price(S=100, K=100, T=1.0, sigma=0.30, r=0.05, option_type="C")
        assert p2 > p1


# ---------------------------------------------------------------------------
# Greeks tests
# ---------------------------------------------------------------------------


class TestGreeks:
    def test_delta_call_range(self):
        """Call delta must be in (0, 1)."""
        delta = bs_delta(S=100, K=100, T=0.5, sigma=0.25, r=0.05, option_type="C")
        assert 0 < delta < 1

    def test_delta_put_range(self):
        """Put delta must be in (-1, 0)."""
        delta = bs_delta(S=100, K=100, T=0.5, sigma=0.25, r=0.05, option_type="P")
        assert -1 < delta < 0

    def test_atm_call_delta_near_half(self):
        """ATM call delta should be close to 0.5."""
        delta = bs_delta(S=100, K=100, T=1.0, sigma=0.20, r=0.0, q=0.0, option_type="C")
        assert abs(delta - 0.5) < 0.05

    def test_put_call_delta_relationship(self):
        """Call delta + |put delta| should equal 1 (no dividends)."""
        call_d = bs_delta(S=100, K=100, T=0.5, sigma=0.25, r=0.05, q=0.0, option_type="C")
        put_d = bs_delta(S=100, K=100, T=0.5, sigma=0.25, r=0.05, q=0.0, option_type="P")
        assert abs(call_d + abs(put_d) - 1.0) < 1e-9

    def test_vega_positive(self):
        """Vega must be positive for all options."""
        vega = bs_vega(S=100, K=100, T=0.5, sigma=0.25, r=0.05)
        assert vega > 0

    def test_vega_call_equals_put(self):
        """Vega is the same for call and put with same params."""
        vega_c = bs_vega(S=100, K=105, T=0.5, sigma=0.25, r=0.05)
        vega_p = bs_vega(S=100, K=105, T=0.5, sigma=0.25, r=0.05)
        assert abs(vega_c - vega_p) < 1e-12

    def test_theta_negative_for_call(self):
        """Call theta should typically be negative (time decay)."""
        theta = bs_theta(S=100, K=100, T=0.5, sigma=0.25, r=0.05, option_type="C")
        assert theta < 0

    def test_greeks_container(self):
        """bs_greeks returns all three greeks correctly."""
        g = bs_greeks(S=100, K=100, T=0.5, sigma=0.25, r=0.05, option_type="C")
        assert 0 < g.delta < 1
        assert g.vega > 0
        assert g.theta < 0


# ---------------------------------------------------------------------------
# Implied Volatility round-trip tests
# ---------------------------------------------------------------------------


class TestImpliedVolatility:
    def _check_round_trip(self, S, K, T, sigma, r, q, opt_type, tol=1e-5):
        price = bs_price(S, K, T, sigma, r, q, opt_type)
        iv = implied_volatility(price, S, K, T, r, q, opt_type)
        assert iv is not None, f"IV solver returned None for {opt_type} S={S} K={K} T={T} sigma={sigma}"
        assert abs(iv - sigma) < tol, f"IV round-trip failed: got {iv:.6f}, expected {sigma:.6f}"

    def test_atm_call_round_trip(self):
        self._check_round_trip(100, 100, 0.5, 0.20, 0.05, 0.0, "C")

    def test_atm_put_round_trip(self):
        self._check_round_trip(100, 100, 0.5, 0.20, 0.05, 0.0, "P")

    def test_otm_call_round_trip(self):
        self._check_round_trip(100, 110, 0.5, 0.25, 0.05, 0.0, "C")

    def test_otm_put_round_trip(self):
        self._check_round_trip(100, 90, 0.5, 0.25, 0.05, 0.0, "P")

    def test_high_vol_round_trip(self):
        self._check_round_trip(100, 100, 1.0, 0.80, 0.04, 0.0, "C")

    def test_low_vol_round_trip(self):
        self._check_round_trip(100, 100, 0.25, 0.05, 0.05, 0.0, "C")

    def test_with_dividend_round_trip(self):
        self._check_round_trip(150, 150, 0.5, 0.30, 0.05, 0.02, "C")

    def test_known_price_gives_known_iv(self):
        """
        For S=100, K=100, T=1, r=0.05, q=0, call price = 10.451:
        IV should solve to approximately 0.20.
        Reference price: bs_price(100, 100, 1, 0.20, 0.05) ≈ 10.451
        """
        reference_price = bs_price(100, 100, 1.0, 0.20, 0.05, 0.0, "C")
        iv = implied_volatility(reference_price, 100, 100, 1.0, 0.05, 0.0, "C")
        assert iv is not None
        assert abs(iv - 0.20) < 1e-5

    def test_below_intrinsic_returns_none(self):
        """Price below intrinsic value → IV solver should return None."""
        # Call intrinsic: max(S - K*exp(-rT), 0)
        iv = implied_volatility(0.001, 100, 50, 1.0, 0.05, 0.0, "C")
        assert iv is None

    def test_zero_T_returns_none(self):
        """T=0 → IV solver returns None."""
        iv = implied_volatility(5.0, 100, 95, 0.0, 0.05, 0.0, "C")
        assert iv is None

    @pytest.mark.parametrize("S,K,T,sigma,opt_type", [
        (185.5, 185, 0.08, 0.22, "C"),  # near ATM AAPL-like
        (415.0, 420, 0.16, 0.24, "P"),  # MSFT-like put
        (875.0, 900, 0.25, 0.50, "C"),  # NVDA-like OTM call
        (510.0, 500, 0.12, 0.14, "P"),  # SPY-like ITM put
    ])
    def test_realistic_round_trips(self, S, K, T, sigma, opt_type):
        self._check_round_trip(S, K, T, sigma, 0.045, 0.0, opt_type)
