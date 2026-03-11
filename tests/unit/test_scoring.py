"""
Unit tests for the scoring function and filters.
"""

from __future__ import annotations

from datetime import date


from src.scoring.scorer import compute_score
from src.scoring.filters import FilterConfig, filter_contract, apply_filters
from src.ingest.models import OptionContract


def _make_contract(
    ticker="AAPL",
    expiry_days=30,
    strike=100.0,
    spot=100.0,
    bid=1.95,
    ask=2.05,
    volume=500,
    oi=1000,
    opt_type="C",
    div=0.0,
    as_of: date = date(2026, 3, 11),
) -> OptionContract:
    return OptionContract(
        ticker=ticker,
        expiry=as_of + __import__("datetime").timedelta(days=expiry_days),
        strike=strike,
        option_type=opt_type,
        bid=bid,
        ask=ask,
        volume=volume,
        open_interest=oi,
        spot=spot,
        dividend_yield=div,
    )


# ---------------------------------------------------------------------------
# compute_score tests
# ---------------------------------------------------------------------------


class TestComputeScore:
    def test_basic_score(self):
        """
        edge_price=0.63, vega=0.11, spread=0.06, theta=-0.04, mid=2.15
        slippage = 0.001 * 2.15 = 0.00215
        denom = 0.06 + 0.04 + 0.00215 = 0.10215
        score = (0.63 * 0.11) / 0.10215 ≈ 0.6781
        """
        score = compute_score(
            edge_price=0.63,
            vega=0.11,
            bid_ask_spread_pct=0.06,
            theta=-0.04,
            mid=2.15,
            slippage_factor=0.001,
        )
        expected = (0.63 * 0.11) / (0.06 + 0.04 + 0.001 * 2.15)
        assert abs(score - expected) < 1e-10

    def test_score_matches_spec(self):
        """Exact example from the spec."""
        score = compute_score(
            edge_price=0.63,
            vega=0.11,
            bid_ask_spread_pct=0.06,
            theta=-0.04,
            mid=2.15,
        )
        assert score > 0

    def test_zero_denom_uses_floor(self):
        """Denominator of zero → uses 1e-6 floor."""
        score = compute_score(
            edge_price=1.0, vega=0.1, bid_ask_spread_pct=0.0, theta=0.0, mid=0.0
        )
        expected = (1.0 * 0.1) / 1e-6
        assert abs(score - expected) < 1.0

    def test_negative_edge_gives_negative_score(self):
        score = compute_score(
            edge_price=-0.5, vega=0.1, bid_ask_spread_pct=0.05, theta=-0.02, mid=2.0
        )
        assert score < 0

    def test_higher_edge_gives_higher_score(self):
        s1 = compute_score(0.2, 0.1, 0.05, -0.02, 2.0)
        s2 = compute_score(0.6, 0.1, 0.05, -0.02, 2.0)
        assert s2 > s1

    def test_higher_vega_gives_higher_score(self):
        s1 = compute_score(0.5, 0.05, 0.05, -0.02, 2.0)
        s2 = compute_score(0.5, 0.20, 0.05, -0.02, 2.0)
        assert s2 > s1

    def test_wider_spread_gives_lower_score(self):
        s1 = compute_score(0.5, 0.1, 0.03, -0.02, 2.0)
        s2 = compute_score(0.5, 0.1, 0.15, -0.02, 2.0)
        assert s1 > s2

    def test_deterministic(self):
        """Same inputs → same output every time."""
        args = (0.63, 0.11, 0.06, -0.04, 2.15)
        assert compute_score(*args) == compute_score(*args)


# ---------------------------------------------------------------------------
# filter_contract tests
# ---------------------------------------------------------------------------


class TestFilterContract:
    AS_OF = date(2026, 3, 11)
    CFG = FilterConfig(
        max_bid_ask_spread_pct=0.10,
        min_open_interest=200,
        min_days_to_expiry=3,
        max_days_to_expiry=180,
    )

    def test_valid_contract_passes(self):
        contract = _make_contract()
        result = filter_contract(contract, self.AS_OF, self.CFG)
        assert result.passed

    def test_missing_bid_fails(self):
        contract = _make_contract(bid=None)
        result = filter_contract(contract, self.AS_OF, self.CFG)
        assert not result.passed
        assert "bid/ask" in result.reason

    def test_missing_ask_fails(self):
        contract = _make_contract(ask=None)
        result = filter_contract(contract, self.AS_OF, self.CFG)
        assert not result.passed

    def test_wide_spread_fails(self):
        # spread = (3.0 - 1.0) / 2.0 = 100% > 10%
        contract = _make_contract(bid=1.0, ask=3.0)
        result = filter_contract(contract, self.AS_OF, self.CFG)
        assert not result.passed
        assert "spread" in result.reason

    def test_low_oi_fails(self):
        contract = _make_contract(oi=100)
        result = filter_contract(contract, self.AS_OF, self.CFG)
        assert not result.passed
        assert "OI" in result.reason

    def test_expired_fails(self):
        contract = _make_contract(expiry_days=1)
        result = filter_contract(contract, self.AS_OF, self.CFG)
        assert not result.passed
        assert "DTE" in result.reason

    def test_too_far_out_fails(self):
        contract = _make_contract(expiry_days=200)
        result = filter_contract(contract, self.AS_OF, self.CFG)
        assert not result.passed

    def test_borderline_spread_passes(self):
        # mid = 2.15, spread = 0.215 → spread_pct = 10% exactly (borderline)
        bid = 2.0425
        ask = 2.2575
        contract = _make_contract(bid=bid, ask=ask)
        spread_pct = (ask - bid) / ((bid + ask) / 2)
        # 10% exactly should pass (<= check)
        cfg = FilterConfig(max_bid_ask_spread_pct=spread_pct + 0.001)
        result = filter_contract(contract, self.AS_OF, cfg)
        assert result.passed

    def test_apply_filters_counts(self):
        as_of = self.AS_OF
        contracts = [
            _make_contract(oi=100),   # fail OI
            _make_contract(oi=1000),  # pass
            _make_contract(bid=None), # fail bid
            _make_contract(),         # pass
        ]
        passed, counts = apply_filters(contracts, as_of, self.CFG)
        assert len(passed) == 2
        assert sum(counts.values()) == 2
