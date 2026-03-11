"""
Unit tests for LLM rationale generation.

Acceptance test: LLM output must reference numbers but NOT contain arithmetic
expressions (e.g. "0.28 - 0.22 = 0.06" must not appear).
"""

from __future__ import annotations

import re

from src.llm.rationale import (
    LLMProvider,
    check_no_arithmetic,
    generate_rationale,
    generate_daily_summary,
)


SAMPLE_CANDIDATE = {
    "id": "test-id-001",
    "run_date": "2026-03-11T18:00:00Z",
    "ticker": "AAPL",
    "contract": "AAPL 2026-04-17 C 185.00",
    "expiry": "2026-04-17",
    "strike": 185.0,
    "type": "C",
    "market_mid": 2.15,
    "bid": 2.00,
    "ask": 2.30,
    "iv": 0.22,
    "delta": 0.52,
    "vega": 0.11,
    "theta": -0.04,
    "sigma_hat_T": 0.28,
    "edge_vol": 0.06,
    "model_fair_value": 2.78,
    "edge_price": 0.63,
    "bid_ask_spread_pct": 0.07,
    "oi": 3200,
    "volume": 540,
    "score": 0.41,
    "rank": 1,
    "reason_summary": "",
    "risks": [],
    "metadata": {},
}


class TestMockRationale:
    def test_generates_non_empty_string(self):
        result = generate_rationale(SAMPLE_CANDIDATE, provider=LLMProvider.MOCK)
        assert isinstance(result, str)
        assert len(result) > 50

    def test_references_ticker(self):
        result = generate_rationale(SAMPLE_CANDIDATE, provider=LLMProvider.MOCK)
        assert "AAPL" in result

    def test_no_arithmetic_in_output(self):
        """
        Acceptance test: mock rationale must not contain arithmetic expressions.
        Pattern: <number> <op> <number> = <number>
        """
        result = generate_rationale(SAMPLE_CANDIDATE, provider=LLMProvider.MOCK)
        has_arithmetic = check_no_arithmetic(result)
        assert not has_arithmetic, f"Rationale contains arithmetic: {result}"

    def test_references_numeric_fields(self):
        """Rationale should reference at least some of the key numeric fields."""
        result = generate_rationale(SAMPLE_CANDIDATE, provider=LLMProvider.MOCK)
        # Should reference IV or sigma_hat_T in some form
        has_numbers = bool(re.search(r"\d+\.?\d*%?", result))
        assert has_numbers, "Rationale should reference numeric values"

    def test_fallback_when_no_api_key(self):
        """Without an API key, OpenAI provider should fall back to mock output."""
        result = generate_rationale(
            SAMPLE_CANDIDATE,
            provider=LLMProvider.OPENAI,
            api_key="",  # empty key → mock fallback
        )
        assert isinstance(result, str)
        assert len(result) > 0


class TestDailySummary:
    def test_generates_non_empty_summary(self):
        candidates = [SAMPLE_CANDIDATE] * 5
        result = generate_daily_summary(candidates, "2026-03-11", provider=LLMProvider.MOCK)
        assert isinstance(result, str)
        assert len(result) > 100

    def test_summary_contains_watch_list(self):
        candidates = [SAMPLE_CANDIDATE] * 5
        result = generate_daily_summary(candidates, "2026-03-11", provider=LLMProvider.MOCK)
        assert "WATCH LIST" in result or "Watch" in result.title()

    def test_no_arithmetic_in_summary(self):
        candidates = [SAMPLE_CANDIDATE] * 5
        result = generate_daily_summary(candidates, "2026-03-11", provider=LLMProvider.MOCK)
        has_arithmetic = check_no_arithmetic(result)
        assert not has_arithmetic, f"Summary contains arithmetic: {result}"


class TestCheckNoArithmetic:
    def test_detects_arithmetic(self):
        text = "IV=0.28 - 0.22 = 0.06 means the option is cheap."
        assert check_no_arithmetic(text) is True

    def test_plain_numbers_ok(self):
        text = "IV=22% is below the forecast of 28%, indicating underpricing."
        assert check_no_arithmetic(text) is False

    def test_percentage_references_ok(self):
        text = "The edge_vol of 6% is material given vega of 0.11."
        assert check_no_arithmetic(text) is False

    def test_division_detected(self):
        text = "Score = 0.63 / 0.10 = 6.3 which is high."
        assert check_no_arithmetic(text) is True
