"""
APScheduler-based daily scheduler.

Default schedule: 7:30 AM PST every weekday (Mon–Fri).
  PST = UTC-8  → 15:30 UTC  (standard time)
  PDT = UTC-7  → 14:30 UTC  (daylight saving, Mar–Nov)

The scheduler always uses the UTC CRON expression internally.
Set SCHEDULER_CRON in the environment to override (UTC).

Post-run pipeline:
  1. run_engine() → top-N candidates
  2. format_daily_report() → Markdown + console text
  3. write_report_to_docs() → Google Docs (or local file fallback)
  4. Structured log with report location
"""

from __future__ import annotations

import logging
from typing import Optional

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config.settings import Settings, get_settings
from ..engine import run_engine
from ..reporting.formatter import format_daily_report
from ..reporting.google_docs import write_report_to_docs

log = structlog.get_logger(__name__)

# 7:30 AM PST = 15:30 UTC (standard time, Nov–Mar)
# 7:30 AM PDT = 14:30 UTC (daylight saving, Mar–Nov)
# We use 15:30 UTC which is safe across both zones within a 1-hour window.
# Override via SCHEDULER_CRON env var for exact PDT control.
_DEFAULT_CRON_UTC = "30 15 * * 1-5"   # Mon–Fri 15:30 UTC ≈ 7:30 AM PST


def _ensure_google_token_valid() -> bool:
    """
    Before each scheduled run, check and auto-refresh the Google token if possible.
    Returns True if a valid token is available.
    """
    import json
    import requests as req
    from pathlib import Path

    token_file = Path("data/.google_token.json")
    if not token_file.exists():
        return False

    try:
        data = json.loads(token_file.read_text())
        token = data.get("access_token", "")

        # Check if current token is still valid (with 5-min buffer)
        if token:
            info = req.get(
                "https://www.googleapis.com/oauth2/v1/tokeninfo",
                params={"access_token": token}, timeout=10,
            ).json()
            if "error" not in info and int(info.get("expires_in", 0)) > 300:
                return True

        # Try auto-refresh if we have the secret
        refresh_token = data.get("refresh_token", "")
        client_id = data.get("client_id", "")
        client_secret = data.get("client_secret", "")

        if refresh_token and client_id and client_secret:
            resp = req.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=15,
            ).json()
            if "access_token" in resp:
                data["access_token"] = resp["access_token"]
                token_file.write_text(json.dumps(data, indent=2))
                log.info("scheduler.token_auto_refreshed",
                         expires_in=resp.get("expires_in"))
                return True
            log.warning("scheduler.token_refresh_failed", error=resp.get("error"))

        log.warning(
            "scheduler.google_token_expired",
            hint=(
                "Token expired and cannot auto-refresh. "
                "Run: python3 scripts/refresh_google_token.py "
                "OR call POST /token with a fresh access token."
            ),
        )
        return False
    except Exception as exc:
        log.error("scheduler.token_check_error", error=str(exc))
        return False


def _scheduled_run(settings: Settings) -> None:
    """Execute a full engine run and dispatch the daily report."""
    import os
    log.info("scheduler.triggered", schedule="07:30 PST")

    # Ensure Google token is valid before running
    token_ok = _ensure_google_token_valid()
    if not token_ok:
        log.warning("scheduler.google_token_unavailable",
                    note="Report will be saved locally instead of Google Docs")

    try:
        result = run_engine(settings=settings)
    except Exception as exc:
        log.error("scheduler.engine_failed", error=str(exc), exc_info=True)
        return

    n = len(result.candidates)
    log.info("scheduler.engine_complete", run_id=result.run_id, candidates=n)

    if not result.candidates:
        log.warning("scheduler.no_candidates", run_id=result.run_id)
        return

    # ── Format report ──────────────────────────────────────────────────────
    try:
        reports = format_daily_report(
            candidates=result.candidates,
            run_date=result.run_date,
            top_n=5,
            daily_summary=result.daily_summary,
        )
    except Exception as exc:
        log.error("scheduler.format_failed", error=str(exc), exc_info=True)
        return

    # ── Print top-5 to console / logs ─────────────────────────────────────
    print("\n" + reports["console_text"] + "\n")

    # ── Write to Google Docs ───────────────────────────────────────────────
    doc_id = os.getenv("GDRIVE_DOC_ID", settings.reporting.gdrive_doc_id)
    folder_id = os.getenv("GDRIVE_FOLDER_ID", settings.reporting.gdrive_folder_id)

    try:
        result_doc = write_report_to_docs(
            markdown=reports["google_docs_markdown"],
            run_date=result.run_date,
            doc_id=doc_id or None,
            folder_id=folder_id or None,
        )
        log.info(
            "scheduler.report_dispatched",
            method=result_doc["method"],
            location=result_doc["location"],
            message=result_doc["message"],
        )
        print(result_doc["message"])
    except Exception as exc:
        log.error("scheduler.report_dispatch_failed", error=str(exc), exc_info=True)


def start_scheduler(settings: Optional[Settings] = None) -> BackgroundScheduler:
    """
    Start the background scheduler and return the instance.

    The scheduler fires every weekday at 7:30 AM PST (15:30 UTC by default).
    Disabled unless settings.scheduler.enabled = True or SCHEDULER_ENABLED=true.
    """
    import os
    settings = settings or get_settings()

    scheduler = BackgroundScheduler(timezone="UTC")

    enabled = settings.scheduler.enabled or os.getenv("SCHEDULER_ENABLED", "").lower() in (
        "1", "true", "yes"
    )

    if not enabled:
        log.info("scheduler.disabled", hint="Set SCHEDULER_ENABLED=true to activate")
        return scheduler

    # Allow runtime override of the CRON expression
    cron_expr = os.getenv("SCHEDULER_CRON", settings.scheduler.cron or _DEFAULT_CRON_UTC)
    parts = cron_expr.split()
    if len(parts) != 5:
        raise ValueError(f"Invalid CRON expression: {cron_expr!r}")

    minute, hour, day, month, day_of_week = parts
    trigger = CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone="UTC",
    )

    scheduler.add_job(
        _scheduled_run,
        trigger=trigger,
        args=[settings],
        id="daily_top5_report",
        replace_existing=True,
        misfire_grace_time=600,   # 10-minute grace window
        coalesce=True,            # only run once if multiple misfires pile up
    )

    scheduler.start()
    log.info(
        "scheduler.started",
        cron_utc=cron_expr,
        schedule_pst="07:30 PST (Mon–Fri)",
        gdrive_doc_id=settings.reporting.gdrive_doc_id or "not set",
    )
    return scheduler
