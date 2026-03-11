"""
FastAPI application factory and REST endpoints.

Endpoints:
  POST /run               Trigger an engine run
  GET  /results           List runs / return candidates for a date
  GET  /results/{id}      Detailed candidate record
  GET  /health            Health check + last run summary
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config.settings import Settings, get_settings
from ..engine import RunResult, run_engine

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    date: Optional[str] = Field(
        None, description="ISO date YYYY-MM-DD to run for (defaults to today UTC)"
    )
    tickers: Optional[List[str]] = Field(
        None, description="Override universe of tickers to scan"
    )
    force_rerun: bool = Field(False, description="Skip idempotency check")


class RunResponse(BaseModel):
    run_id: str
    run_date: str
    status: str
    stats: Dict[str, Any]
    candidates_count: int
    daily_summary: str
    candidates: List[Dict[str, Any]]


class HealthResponse(BaseModel):
    status: str
    last_run_id: Optional[str]
    last_run_date: Optional[str]
    last_run_candidates: Optional[int]
    uptime_seconds: float
    version: str


# ---------------------------------------------------------------------------
# In-process state (last run cache)
# ---------------------------------------------------------------------------

_last_run: Optional[RunResult] = None
_app_start_time = time.monotonic()


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="Volatility Mispricing Engine",
        description=(
            "Scans option markets, identifies undervalued options via volatility mispricing "
            "(Phase 1) and directional overlays (Phase 2)."
        ),
        version=settings.app_version,
    )

    # ------------------------------------------------------------------
    # POST /run
    # ------------------------------------------------------------------

    @app.post("/run", response_model=RunResponse, summary="Trigger an engine run")
    async def trigger_run(body: RunRequest, background_tasks: BackgroundTasks):
        global _last_run

        run_date: Optional[datetime] = None
        if body.date:
            try:
                run_date = datetime.fromisoformat(body.date).replace(tzinfo=timezone.utc)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid date format: {body.date!r}")

        try:
            result = run_engine(
                settings=settings,
                tickers=body.tickers,
                run_date=run_date,
                force_rerun=body.force_rerun,
            )
            _last_run = result
        except Exception as exc:
            log.error("api.run_error", error=str(exc), exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc))

        return RunResponse(
            run_id=result.run_id,
            run_date=result.run_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
            status="completed",
            stats=result.stats,
            candidates_count=len(result.candidates),
            daily_summary=result.daily_summary,
            candidates=[c.to_dict() for c in result.candidates],
        )

    # ------------------------------------------------------------------
    # GET /results
    # ------------------------------------------------------------------

    @app.get("/results", summary="Return candidates for a run date")
    async def get_results(
        date: Optional[str] = Query(None, description="YYYY-MM-DD"),
        limit: int = Query(20, ge=1, le=100),
        ticker: Optional[str] = Query(None, description="Filter by ticker"),
    ):
        results_dir = Path(settings.persistence.results_dir)

        if date:
            # Find run files matching the given date
            matching = list(results_dir.glob(f"run_{date}_*.json"))
            if not matching:
                raise HTTPException(
                    status_code=404, detail=f"No results found for date {date!r}"
                )
            # Use the most recent file if multiple exist
            matching.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            with open(matching[0]) as f:
                data = json.load(f)
            candidates = data.get("candidates", [])
        elif _last_run is not None:
            candidates = [c.to_dict() for c in _last_run.candidates]
        else:
            # Try loading the most recent file
            if not results_dir.exists():
                return JSONResponse({"candidates": [], "message": "No runs yet"})
            all_files = sorted(results_dir.glob("run_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not all_files:
                return JSONResponse({"candidates": [], "message": "No runs yet"})
            with open(all_files[0]) as f:
                data = json.load(f)
            candidates = data.get("candidates", [])

        # Optional ticker filter
        if ticker:
            candidates = [c for c in candidates if c.get("ticker", "").upper() == ticker.upper()]

        return JSONResponse({"candidates": candidates[:limit], "total": len(candidates)})

    # ------------------------------------------------------------------
    # GET /results/{id}
    # ------------------------------------------------------------------

    @app.get("/results/{candidate_id}", summary="Detailed candidate record by ID")
    async def get_result_by_id(candidate_id: str):
        # Search in-memory first
        if _last_run:
            for c in _last_run.candidates:
                if c.id == candidate_id:
                    return JSONResponse(c.to_dict())

        # Search persisted files
        results_dir = Path(settings.persistence.results_dir)
        if results_dir.exists():
            for fpath in sorted(results_dir.glob("run_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                with open(fpath) as f:
                    data = json.load(f)
                for c in data.get("candidates", []):
                    if c.get("id") == candidate_id:
                        return JSONResponse(c)

        raise HTTPException(status_code=404, detail=f"Candidate {candidate_id!r} not found")

    # ------------------------------------------------------------------
    # GET /health
    # ------------------------------------------------------------------

    @app.get("/health", response_model=HealthResponse, summary="Health check")
    async def health():
        uptime = time.monotonic() - _app_start_time
        if _last_run:
            return HealthResponse(
                status="ok",
                last_run_id=_last_run.run_id,
                last_run_date=_last_run.run_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
                last_run_candidates=len(_last_run.candidates),
                uptime_seconds=round(uptime, 1),
                version=settings.app_version,
            )
        return HealthResponse(
            status="ok",
            last_run_id=None,
            last_run_date=None,
            last_run_candidates=None,
            uptime_seconds=round(uptime, 1),
            version=settings.app_version,
        )

    return app
