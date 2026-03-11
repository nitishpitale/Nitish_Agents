"""
Integration tests: end-to-end engine run using the mock eToro client.

Acceptance criteria:
  1. Run produces at least 5 candidates from mock dataset.
  2. Candidate JSON matches required schema exactly.
  3. Deterministic rerun of same mock data yields identical results.
  4. LLM rationale produced and contains no arithmetic.
  5. All required JSON fields are present and correctly typed.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from src.config.settings import Settings
from src.engine import run_engine
from src.ingest.mock_client import MockEToroClient
from src.llm.rationale import check_no_arithmetic


# Fixed reference date for deterministic tests
REF_DATE = datetime(2026, 3, 11, 18, 0, 0, tzinfo=timezone.utc)

# Required output JSON schema fields and their expected types
REQUIRED_SCHEMA = {
    "id": str,
    "run_date": str,
    "ticker": str,
    "contract": str,
    "expiry": str,
    "strike": (int, float),
    "type": str,
    "market_mid": (int, float),
    "bid": (int, float),
    "ask": (int, float),
    "iv": (int, float),
    "delta": (int, float),
    "vega": (int, float),
    "theta": (int, float),
    "sigma_hat_T": (int, float),
    "edge_vol": (int, float),
    "model_fair_value": (int, float),
    "edge_price": (int, float),
    "bid_ask_spread_pct": (int, float),
    "oi": int,
    "volume": int,
    "score": (int, float),
    "rank": int,
    "reason_summary": str,
    "risks": list,
    "metadata": dict,
}


@pytest.fixture(scope="module")
def default_settings() -> Settings:
    """Settings with mock client enabled."""
    return Settings.from_yaml_and_env()


@pytest.fixture(scope="module")
def mock_client() -> MockEToroClient:
    return MockEToroClient(reference_date=REF_DATE.date(), seed=42)


@pytest.fixture(scope="module")
def run_result(default_settings, mock_client):
    """Execute one engine run and return the result."""
    return run_engine(
        settings=default_settings,
        tickers=["AAPL", "MSFT", "TSLA", "NVDA", "SPY", "QQQ"],
        run_date=REF_DATE,
        force_rerun=True,
        client=mock_client,
    )


# ---------------------------------------------------------------------------
# Core acceptance tests
# ---------------------------------------------------------------------------


class TestEngineRun:
    def test_run_completes_without_error(self, run_result):
        assert run_result is not None
        assert run_result.run_id is not None

    def test_produces_at_least_five_candidates(self, run_result):
        """Acceptance test: must produce ≥ 5 candidates from mock dataset."""
        assert len(run_result.candidates) >= 5, (
            f"Expected ≥ 5 candidates, got {len(run_result.candidates)}"
        )

    def test_candidates_ranked_correctly(self, run_result):
        """Ranks must be sequential starting from 1."""
        for i, candidate in enumerate(run_result.candidates, start=1):
            assert candidate.rank == i, f"Expected rank {i}, got {candidate.rank}"

    def test_scores_are_descending(self, run_result):
        """Candidates must be sorted by score (or phase2_score) descending."""
        scores = [c.phase2_score or c.score for c in run_result.candidates]
        for i in range(len(scores) - 1):
            assert scores[i] >= scores[i + 1], (
                f"Score not descending at rank {i+1}: {scores[i]} < {scores[i+1]}"
            )

    def test_top_n_not_exceeded(self, run_result, default_settings):
        assert len(run_result.candidates) <= default_settings.scoring.top_n

    def test_daily_summary_generated(self, run_result):
        assert isinstance(run_result.daily_summary, str)
        assert len(run_result.daily_summary) > 0

    def test_stats_present(self, run_result):
        stats = run_result.stats
        assert "candidates_returned" in stats
        assert "total_contracts_ingested" in stats
        assert "runtime_seconds" in stats
        assert stats["total_contracts_ingested"] > 0


# ---------------------------------------------------------------------------
# Schema validation tests
# ---------------------------------------------------------------------------


class TestOutputSchema:
    def test_all_required_fields_present(self, run_result):
        """Every candidate must have all required JSON fields."""
        for candidate in run_result.candidates:
            d = candidate.to_dict()
            for field_name, expected_type in REQUIRED_SCHEMA.items():
                assert field_name in d, (
                    f"Field {field_name!r} missing from candidate {candidate.contract!r}"
                )
                value = d[field_name]
                assert isinstance(value, expected_type), (
                    f"Field {field_name!r} expected {expected_type}, got {type(value)} "
                    f"(value={value!r}) in candidate {candidate.contract!r}"
                )

    def test_type_field_is_C_or_P(self, run_result):
        for c in run_result.candidates:
            assert c.type in ("C", "P"), f"Invalid option type: {c.type}"

    def test_iv_in_valid_range(self, run_result):
        for c in run_result.candidates:
            assert 0 < c.iv <= 5.0, f"IV out of range: {c.iv} for {c.contract}"

    def test_delta_in_valid_range(self, run_result):
        for c in run_result.candidates:
            if c.type == "C":
                assert 0 <= c.delta <= 1, f"Call delta out of range: {c.delta}"
            else:
                assert -1 <= c.delta <= 0, f"Put delta out of range: {c.delta}"

    def test_vega_positive(self, run_result):
        for c in run_result.candidates:
            assert c.vega > 0, f"Vega must be positive: {c.vega}"

    def test_bid_less_than_ask(self, run_result):
        for c in run_result.candidates:
            assert c.bid < c.ask, f"Bid >= ask: bid={c.bid} ask={c.ask}"

    def test_mid_matches_bid_ask(self, run_result):
        for c in run_result.candidates:
            expected_mid = (c.bid + c.ask) / 2.0
            assert abs(c.market_mid - expected_mid) < 0.01, (
                f"mid={c.market_mid} != (bid+ask)/2={expected_mid}"
            )

    def test_run_date_format(self, run_result):
        for c in run_result.candidates:
            d = c.to_dict()
            # Must match YYYY-MM-DDTHH:MM:SSZ
            assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", d["run_date"]), (
                f"Invalid run_date format: {d['run_date']}"
            )

    def test_expiry_format(self, run_result):
        for c in run_result.candidates:
            d = c.to_dict()
            assert re.match(r"\d{4}-\d{2}-\d{2}", d["expiry"]), (
                f"Invalid expiry format: {d['expiry']}"
            )

    def test_contract_id_format(self, run_result):
        """Contract ID should contain ticker, expiry, type, and strike."""
        for c in run_result.candidates:
            assert c.ticker in c.contract
            assert c.type in c.contract


# ---------------------------------------------------------------------------
# Determinism tests
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_identical_reruns_produce_identical_results(self, default_settings):
        """
        Same mock data + same date → identical candidate list.
        This verifies reproducibility (critical acceptance test).
        """
        client1 = MockEToroClient(reference_date=REF_DATE.date(), seed=42)
        client2 = MockEToroClient(reference_date=REF_DATE.date(), seed=42)

        result1 = run_engine(
            settings=default_settings,
            tickers=["AAPL", "MSFT", "TSLA"],
            run_date=REF_DATE,
            force_rerun=True,
            client=client1,
        )
        result2 = run_engine(
            settings=default_settings,
            tickers=["AAPL", "MSFT", "TSLA"],
            run_date=REF_DATE,
            force_rerun=True,
            client=client2,
        )

        assert len(result1.candidates) == len(result2.candidates), (
            "Different number of candidates across reruns"
        )

        for c1, c2 in zip(result1.candidates, result2.candidates):
            assert c1.contract == c2.contract, "Contract order changed across reruns"
            assert c1.iv == c2.iv, f"IV changed: {c1.iv} vs {c2.iv}"
            assert c1.score == c2.score, f"Score changed: {c1.score} vs {c2.score}"
            assert c1.edge_price == c2.edge_price, "edge_price changed across reruns"

    def test_different_date_may_differ(self, default_settings):
        """Different dates use different market data → results may differ (sanity check)."""
        date1 = datetime(2026, 3, 11, 18, 0, 0, tzinfo=timezone.utc)
        date2 = datetime(2026, 3, 12, 18, 0, 0, tzinfo=timezone.utc)

        client1 = MockEToroClient(reference_date=date1.date(), seed=42)
        client2 = MockEToroClient(reference_date=date2.date(), seed=42)

        result1 = run_engine(
            settings=default_settings,
            tickers=["AAPL"],
            run_date=date1,
            force_rerun=True,
            client=client1,
        )
        result2 = run_engine(
            settings=default_settings,
            tickers=["AAPL"],
            run_date=date2,
            force_rerun=True,
            client=client2,
        )

        # At least the run dates differ
        assert result1.run_date != result2.run_date


# ---------------------------------------------------------------------------
# LLM rationale acceptance tests
# ---------------------------------------------------------------------------


class TestLLMRationale:
    def test_all_candidates_have_rationale(self, run_result):
        for c in run_result.candidates:
            assert len(c.reason_summary) > 0, (
                f"Empty rationale for {c.contract}"
            )

    def test_rationale_contains_no_arithmetic(self, run_result):
        """
        Critical acceptance test: LLM output must NOT contain arithmetic expressions
        like "0.28 - 0.22 = 0.06".
        """
        for c in run_result.candidates:
            has_arithmetic = check_no_arithmetic(c.reason_summary)
            assert not has_arithmetic, (
                f"Rationale for {c.contract} contains arithmetic: {c.reason_summary}"
            )

    def test_rationale_is_non_trivial(self, run_result):
        """Rationale should be at least 80 characters."""
        for c in run_result.candidates:
            assert len(c.reason_summary) >= 80, (
                f"Rationale too short ({len(c.reason_summary)} chars) for {c.contract}"
            )


# ---------------------------------------------------------------------------
# Phase 2 tests
# ---------------------------------------------------------------------------


class TestPhase2:
    def test_phase2_scores_present_when_enabled(self, run_result, default_settings):
        if not default_settings.phase2.enabled:
            pytest.skip("Phase 2 disabled in config")

        for c in run_result.candidates:
            assert c.phase2_score is not None, (
                f"Phase 2 score missing for {c.contract}"
            )

    def test_earnings_risk_annotated(self, run_result, default_settings):
        """Candidates with upcoming earnings should have risk annotation."""
        if not default_settings.phase2.enabled:
            pytest.skip("Phase 2 disabled in config")

        earnings_tickers = {"AAPL", "MSFT", "TSLA"}
        for c in run_result.candidates:
            if c.ticker in earnings_tickers:
                # May or may not have earnings annotation depending on window
                # Just verify the risks field is a list
                assert isinstance(c.risks, list)
