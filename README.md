# Volatility Mispricing Engine

A production-ready agent that scans option markets daily, identifies undervalued options by detecting volatility mispricing (**Phase 1**), and enhances signals with directional overlays (**Phase 2**).

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Volatility Mispricing Engine                   │
│                                                                        │
│  ┌─────────────┐    ┌──────────────┐    ┌──────────────────────────┐  │
│  │   Ingest    │    │  Modeling    │    │       Scoring            │  │
│  │             │    │              │    │                          │  │
│  │ EToroClient │───▶│ Black-Scholes│───▶│  Filters (spread/OI/DTE)│  │
│  │ MockClient  │    │ IV Solver    │    │  compute_score()         │  │
│  │             │    │ Realized Vol │    │  Phase 2 Overlay         │  │
│  └─────────────┘    └──────────────┘    └──────────┬───────────────┘  │
│                                                     │                  │
│  ┌─────────────────────────────────────────────────▼───────────────┐  │
│  │                     Engine Orchestrator                          │  │
│  │  run_engine() → ingest → filter → score → LLM rationale → store │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                │                                        │
│  ┌─────────────────────────────▼────────────────────────────────────┐  │
│  │                         REST API (FastAPI)                        │  │
│  │  POST /run  │  GET /results  │  GET /results/{id}  │  GET /health│  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                │                                        │
│  ┌─────────────────────────────▼────────────────────────────────────┐  │
│  │                     Scheduler (APScheduler)                       │  │
│  │            Daily CRON: 18:00 UTC Mon–Fri (configurable)          │  │
│  └─────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘

  LLM Layer (rationale only — no math):
  ┌───────────────────────────────────────────────────────┐
  │  generate_rationale(candidate_dict) → 2-4 sentences  │
  │  generate_daily_summary(candidates) → brief report   │
  └───────────────────────────────────────────────────────┘
```

**Key design constraint:** All pricing, IV extraction, Greeks, realized vol forecasting, and scoring are deterministic Python — the LLM only generates human-readable rationale text.

---

## Module Structure

```
src/
├── config/
│   └── settings.py          # Pydantic settings; loads config.yaml + env vars
├── ingest/
│   ├── models.py            # OptionContract, SpotData, HistoricalPrices
│   ├── etoro_client.py      # Live eToro API client
│   └── mock_client.py       # Deterministic mock for tests / dev
├── modeling/
│   ├── black_scholes.py     # BSM price, Greeks (delta/vega/theta), IV solver
│   └── realized_vol.py      # 20d/60d historical vol, EWMA, blended sigma_hat
├── scoring/
│   ├── filters.py           # Bid-ask spread, OI, DTE filters
│   └── scorer.py            # compute_score(), score_candidates(), Phase 2 overlay
├── llm/
│   ├── rationale.py         # generate_rationale(), generate_daily_summary()
│   └── prompt_templates.md  # Exact prompt templates (documented)
├── api/
│   └── app.py               # FastAPI application factory + endpoints
├── scheduler/
│   └── scheduler.py         # APScheduler CRON background scheduler
└── engine.py                # Main orchestrator: run_engine()

tests/
├── unit/
│   ├── test_black_scholes.py  # IV round-trips, pricing, Greeks
│   ├── test_realized_vol.py   # Historical/EWMA/blend vol
│   ├── test_scoring.py        # Filters + scoring formula
│   └── test_llm.py            # Rationale tests incl. no-arithmetic checker
└── integration/
    ├── test_end_to_end.py     # Full engine run on mock data, schema validation
    └── test_api.py            # REST API endpoint tests
```

---

## Quick Start

### Prerequisites
- Python 3.12+
- Docker + Docker Compose (optional)

### Local Development

```bash
# Clone and install
git clone <repo>
cd volatility-mispricing-engine
pip install -r requirements.txt

# Run with mock data (no credentials needed)
python3 main.py
```

The API will be available at `http://localhost:8000`.

### Using Docker

```bash
# Build and start (mock mode, no credentials required)
docker-compose up --build

# Verify health
curl http://localhost:8000/health

# Trigger a run
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"tickers": ["AAPL", "MSFT", "TSLA"], "force_rerun": true}'

# Get results
curl "http://localhost:8000/results"
```

### Production with live eToro + OpenAI

Set environment variables (or Cursor Secrets):

```bash
export ETORO_API_KEY="your-etoro-api-key"
export ETORO_BASE_URL="https://public-api.etoro.com"
export ETORO_USE_MOCK=false
export OPENAI_API_KEY="sk-..."
export LLM_PROVIDER=openai
```

Then update `config.yaml`:
```yaml
etoro:
  use_mock: false
llm:
  provider: openai
```

---

## API Reference

### `POST /run`
Trigger a full engine run.

**Request body:**
```json
{
  "date": "2026-03-11",          // optional, defaults to today UTC
  "tickers": ["AAPL", "MSFT"],   // optional, overrides config universe
  "force_rerun": false           // skip idempotency check
}
```

**Response:**
```json
{
  "run_id": "abc123",
  "run_date": "2026-03-11T18:00:00Z",
  "status": "completed",
  "stats": { "candidates_returned": 20, "runtime_seconds": 1.2 },
  "candidates_count": 20,
  "daily_summary": "SUMMARY:\n...\nWATCH LIST:\n1. ...",
  "candidates": [...]
}
```

### `GET /results?date=YYYY-MM-DD&ticker=AAPL&limit=20`
Return candidates for a run date.

### `GET /results/{id}`
Detailed candidate record by UUID.

### `GET /health`
```json
{
  "status": "ok",
  "last_run_id": "abc123",
  "last_run_date": "2026-03-11T18:00:00Z",
  "last_run_candidates": 20,
  "uptime_seconds": 3600.0,
  "version": "1.0.0"
}
```

---

## Output JSON Schema

Each candidate in `/results` matches this schema exactly:

```json
{
  "id": "<uuid>",
  "run_date": "2026-03-11T18:00:00Z",
  "ticker": "AAPL",
  "contract": "AAPL 2026-04-08 C 185.00",
  "expiry": "2026-04-08",
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
  "bid_ask_spread_pct": 0.069,
  "oi": 3200,
  "volume": 540,
  "score": 0.41,
  "rank": 1,
  "reason_summary": "AAPL call: IV=22% is below the forecast of 28%...",
  "risks": ["earnings in 21 days"],
  "metadata": { "days_to_expiry": 28 }
}
```

---

## Configuration

All settings in `config.yaml` can be overridden by environment variables.

### Key Config Options

| Setting | Default | Description |
|---|---|---|
| `etoro.use_mock` | `true` | Use mock client (set false for production) |
| `filters.max_bid_ask_spread_pct` | `0.10` | Max 10% bid-ask spread |
| `filters.min_open_interest` | `200` | Minimum OI |
| `filters.min_days_to_expiry` | `3` | Skip expiring-soon contracts |
| `scoring.top_n` | `20` | Max candidates returned |
| `scoring.slippage_factor` | `0.001` | Slippage proxy = factor × mid |
| `realized_vol.blend_weights` | `[0.25, 0.25, 0.50]` | 20d/60d/EWMA blend |
| `realized_vol.ewma_lambda` | `0.94` | EWMA decay factor |
| `phase2.enabled` | `true` | Enable directional overlay |
| `phase2.earnings_penalty_days` | `14` | Penalise if earnings within N days |
| `llm.provider` | `mock` | `openai` or `mock` |
| `scheduler.enabled` | `false` | Enable daily CRON scheduler |
| `scheduler.cron` | `0 18 * * 1-5` | Schedule (UTC) |

---

## Scoring Formula (exact)

```python
slippage = slippage_factor * mid   # default: 0.001 * mid
denom = bid_ask_spread_pct + abs(theta) + slippage
if denom <= 0:
    denom = 1e-6
score = (edge_price * vega) / denom
```

Where:
- `edge_price = model_fair_value - market_mid`
- `model_fair_value = BSM_price(spot, K, T, sigma_hat_T, r, q)`
- `sigma_hat_T = blended_realized_vol(20d, 60d, EWMA)`
- `vega`, `theta` = Black-Scholes Greeks at market IV

### Phase 2 Score (when enabled)

```python
phase2_score = (
    base_score * w.base_score          # default 0.70
    + momentum_adj * w.momentum        # default 0.15
    + earnings_adj * w.earnings_penalty  # default -0.10
    + sentiment_adj * w.sentiment      # default 0.05
)
```

---

## Running Tests

```bash
# All tests
python3 -m pytest tests/ -v

# Unit tests only (fast)
python3 -m pytest tests/unit/ -v

# Integration tests only
python3 -m pytest tests/integration/ -v

# With coverage
python3 -m pytest tests/ --cov=src --cov-report=term-missing
```

### Acceptance Tests (must pass)

| Test | File |
|---|---|
| IV solver round-trip (price→IV→price) | `tests/unit/test_black_scholes.py` |
| Scoring formula exact | `tests/unit/test_scoring.py` |
| ≥ 5 candidates from mock data | `tests/integration/test_end_to_end.py` |
| Schema matches exactly | `tests/integration/test_end_to_end.py` |
| Deterministic reruns | `tests/integration/test_end_to_end.py` |
| LLM output contains no arithmetic | `tests/unit/test_llm.py`, `tests/integration/test_end_to_end.py` |

---

## Monitoring & Logging

The engine uses structured JSON logging (via `structlog`). Every run emits:

```json
{"event": "engine.complete", "run_id": "abc123", "tickers_scanned": 10,
 "total_contracts_ingested": 390, "candidates_returned": 20,
 "runtime_seconds": 1.23, "timestamp": "2026-03-11T18:00:01Z"}
```

Key log events:
- `engine.start` — run begins
- `ingest.option_chain` — contracts loaded per ticker
- `engine.complete` — run done with stats
- `engine.duplicate_run_skipped` — idempotency skip
- `scheduler.triggered` — CRON fired

---

## Phase 2 — Directional Overlay

When `phase2.enabled: true`, the score is enhanced with:

1. **Momentum**: 20-day return percentile vs peers. High momentum boosts calls, penalises puts.
2. **Earnings**: Upcoming earnings within `earnings_penalty_days` days are penalised to reduce event risk.
3. **Sentiment** (optional, `news_sentiment_enabled: true`): External sentiment feed in [-1, +1] range.

All Phase 2 weights are configurable in `config.yaml` under `phase2.weights`.

---

## LLM Constraints

The LLM is used **only** for:
1. Writing 2–4 sentence rationale per candidate (referencing pre-computed numbers)
2. Daily briefing summary + watch list

The LLM must **never** compute IV, Greeks, model prices, edges, or scores.
See [`src/llm/prompt_templates.md`](src/llm/prompt_templates.md) for exact templates.

Acceptance test: output is checked with regex `\b\d+\.?\d*\s*[-+*/]\s*\d+\.?\d*\s*=\s*\d+\.?\d*\b` — if an arithmetic expression is detected in the rationale, the test fails.

---

## Assumptions & Approximations

- **Time to expiry**: `T = DTE / 365` (calendar days; 252-day convention is not used for option pricing)
- **Risk-free rate**: Single configurable rate applied across all tenors (no term structure)
- **Dividend yield**: Continuous dividend yield per underlying (sourced from eToro or set to 0)
- **IV solver**: Newton-Raphson primary, bisection fallback; tolerances `1e-7`
- **Realized vol**: EWMA uses RiskMetrics λ=0.94 by default; blend weights configurable
- **Annualisation**: `vol_annual = vol_daily × sqrt(252)` throughout
- **No term structure or vol surface**: Simple blended vol forecast (not a full surface model)

---

## Design Decisions

1. **Python over TypeScript**: Numerical libraries (scipy, numpy available), mature financial ecosystem, simpler async story for this use case.
2. **FastAPI**: Automatic OpenAPI docs, Pydantic validation, async-ready.
3. **Deterministic mock**: MockEToroClient uses fixed `random.Random(seed)` — same seed = same data always.
4. **Separation of concerns**: LLM layer is completely isolated — `llm/rationale.py` only receives pre-computed dicts.
5. **Idempotency**: SHA-256 hash of `(sorted_tickers, date)` prevents duplicate runs.
6. **No scipy dependency for IV**: Pure Python Newton-Raphson + bisection for portability and transparency.
