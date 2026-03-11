"""
Integration tests for the REST API endpoints.
Uses httpx TestClient (ASGI transport) — no network.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config.settings import Settings


REF_DATE = datetime(2026, 3, 11, 18, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.from_yaml_and_env()


@pytest.fixture(scope="module")
def client(settings) -> TestClient:
    app = create_app(settings)
    return TestClient(app)


class TestHealthEndpoint:
    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "uptime_seconds" in data
        assert "version" in data

    def test_health_initially_no_last_run(self, client):
        resp = client.get("/health")
        data = resp.json()
        # Before any run, last_run fields may be None
        assert data["last_run_id"] is None or isinstance(data["last_run_id"], str)


class TestRunEndpoint:
    def test_run_returns_200(self, client):
        resp = client.post(
            "/run",
            json={
                "date": "2026-03-11",
                "tickers": ["AAPL", "MSFT"],
                "force_rerun": True,
            },
        )
        assert resp.status_code == 200

    def test_run_returns_candidates(self, client):
        resp = client.post(
            "/run",
            json={"tickers": ["AAPL", "TSLA"], "force_rerun": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "candidates" in data
        assert isinstance(data["candidates"], list)
        assert len(data["candidates"]) > 0

    def test_run_response_schema(self, client):
        resp = client.post(
            "/run",
            json={"tickers": ["SPY"], "force_rerun": True},
        )
        data = resp.json()
        assert "run_id" in data
        assert "run_date" in data
        assert "status" in data
        assert data["status"] == "completed"
        assert "candidates_count" in data
        assert "daily_summary" in data
        assert "stats" in data

    def test_run_with_invalid_date_returns_400(self, client):
        resp = client.post("/run", json={"date": "not-a-date"})
        assert resp.status_code == 400

    def test_run_candidate_schema(self, client):
        resp = client.post(
            "/run",
            json={"tickers": ["AAPL"], "force_rerun": True},
        )
        data = resp.json()
        candidates = data["candidates"]
        assert len(candidates) > 0

        required = [
            "id", "run_date", "ticker", "contract", "expiry",
            "strike", "type", "market_mid", "bid", "ask",
            "iv", "delta", "vega", "theta", "sigma_hat_T",
            "edge_vol", "model_fair_value", "edge_price",
            "bid_ask_spread_pct", "oi", "volume", "score", "rank",
            "reason_summary", "risks", "metadata",
        ]
        for field in required:
            assert field in candidates[0], f"Missing field: {field}"


class TestResultsEndpoint:
    def _ensure_run(self, client, tickers=None):
        """Trigger a run to ensure results exist."""
        client.post(
            "/run",
            json={
                "tickers": tickers or ["AAPL", "MSFT"],
                "force_rerun": True,
            },
        )

    def test_results_after_run(self, client):
        self._ensure_run(client)
        resp = client.get("/results")
        assert resp.status_code == 200
        data = resp.json()
        assert "candidates" in data

    def test_results_limit_respected(self, client):
        self._ensure_run(client)
        resp = client.get("/results?limit=3")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["candidates"]) <= 3

    def test_results_ticker_filter(self, client):
        self._ensure_run(client, tickers=["AAPL", "MSFT"])
        resp = client.get("/results?ticker=AAPL")
        assert resp.status_code == 200
        data = resp.json()
        for c in data["candidates"]:
            assert c["ticker"].upper() == "AAPL"

    def test_results_by_id(self, client):
        resp = client.post(
            "/run",
            json={"tickers": ["SPY"], "force_rerun": True},
        )
        candidates = resp.json()["candidates"]
        assert len(candidates) > 0
        candidate_id = candidates[0]["id"]

        resp2 = client.get(f"/results/{candidate_id}")
        assert resp2.status_code == 200
        data = resp2.json()
        assert data["id"] == candidate_id

    def test_results_unknown_id_returns_404(self, client):
        resp = client.get("/results/nonexistent-id-xyz")
        assert resp.status_code == 404

    def test_health_shows_last_run_after_run(self, client):
        self._ensure_run(client)
        resp = client.get("/health")
        data = resp.json()
        assert data["last_run_id"] is not None
        assert data["last_run_candidates"] is not None
        assert data["last_run_candidates"] >= 0
