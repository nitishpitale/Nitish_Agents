# Changelog

All notable changes are documented here. Dates are UTC.

---

## [1.0.0] - 2026-03-11

### Initial Release — Phase 1 + Phase 2 Complete

#### Added

**Core Architecture**
- `src/config/settings.py`: Pydantic-based settings loaded from `config.yaml` with full environment variable override support. All thresholds, weights, and credentials are configurable without code changes.

**Modeling Layer (Phase 1)**
- `src/modeling/black_scholes.py`: Black-Scholes-Merton option pricing (calls and puts with continuous dividend yield), all Greeks (delta, vega, theta), and a robust IV solver (Newton-Raphson with Brenner-Subrahmanyam initialisation + bisection fallback). Tolerances: 1e-7.
- `src/modeling/realized_vol.py`: 20-day and 60-day historical (close-to-close) realised vol, EWMA volatility (RiskMetrics, configurable λ), and a blended `sigma_hat_T` with configurable weights.

**Ingest Layer**
- `src/ingest/models.py`: Pure data containers — `OptionContract`, `SpotData`, `HistoricalPrices`. No pricing logic.
- `src/ingest/etoro_client.py`: Live eToro Public API client covering instrument ID resolution (cached), spot prices, and historical OHLCV candles. Note: eToro does not expose an options chain endpoint; documented in code.
- `src/ingest/mock_client.py`: Deterministic synthetic market data using fixed-seed GBM. Produces realistic option chains with vol smile/skew, liquidity variations, earnings dates, and momentum data — fully offline, no credentials needed.

**Scoring Layer (Phase 1)**
- `src/scoring/filters.py`: Liquidity and quality filters — bid-ask spread %, open interest, days to expiry, completeness checks.
- `src/scoring/scorer.py`: Exact scoring formula `score = (edge_price × vega) / (bid_ask_spread_pct + |theta| + slippage)`. Returns top N `CandidateResult` objects with full numeric detail. Each field matches the required output JSON schema.

**Phase 2 — Directional Overlay**
- `src/scoring/scorer.py::apply_phase2_overlay()`: Momentum percentile adjustment (calls vs puts), earnings proximity penalty, optional sentiment feed. All weights configurable. Phase 2 is a drop-in extension that re-ranks Phase 1 candidates.

**LLM Layer**
- `src/llm/rationale.py`: `generate_rationale()` and `generate_daily_summary()` — both accept pre-computed numeric dicts and return prose summaries. OpenAI and mock providers. `check_no_arithmetic()` acceptance test helper.
- `src/llm/prompt_templates.md`: Exact prompt templates (system + user) for rationale and daily summary, with substitution variable documentation and design constraints.

**Orchestration**
- `src/engine.py`: `run_engine()` ties all layers together. Implements idempotency via SHA-256 run hashes, result persistence, and structured logging.
- `src/scheduler/scheduler.py`: APScheduler CRON scheduler (18:00 UTC Mon-Fri default). Disabled by default.

**REST API**
- `src/api/app.py`: FastAPI app — `POST /run`, `GET /results`, `GET /results/{id}`, `GET /health`. Full OpenAPI documentation auto-generated.

**Tests**
- 74 unit tests covering: BSM price/Greeks (known-value assertions + round-trips), IV solver (parametrised across realistic strikes), realized vol functions, scoring formula (exact + boundary), filters, and LLM no-arithmetic checker.
- 37 integration tests covering: full end-to-end engine run, output schema validation, determinism (identical reruns = identical results), API endpoints, and LLM rationale quality.

**Infrastructure**
- `Dockerfile` + `docker-compose.yml`: Multi-stage production container. Non-root user. Health check.
- `config.yaml`: All defaults documented with comments.
- `fixtures/mock_option_chain.json`: Sample output fixture.

#### Design Decisions

- Newton-Raphson + bisection IV solver chosen over scipy for transparency and portability.
- `random.Random(seed)` (not `numpy.random`) used in mock data for strict determinism across platforms.
- LLM layer completely isolated: it only receives serialised `dict` — no access to internal domain objects.
- Phase 2 implemented as a post-processing step so Phase 1 results are always available independently.

#### Known Limitations

- eToro Public API does not expose options chains natively. Production deployment requires a supplementary options data provider.
- Vol forecasting does not incorporate a full term structure or vol surface — uses a simple blended scalar.
- No portfolio-level Greeks aggregation (per-contract only).

---

## [Unreleased]

### Planned

- Full vol surface model (SABR or SVI parameterisation)
- Portfolio-level Greeks aggregation (delta-hedging view)
- Webhook notifications for top candidates
- Historical backtesting mode
- Streaming WebSocket endpoint for live updates
