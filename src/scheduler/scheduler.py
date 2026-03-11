"""
APScheduler-based daily scheduler for the engine.

Runs the engine on a configurable CRON schedule (default: 18:00 UTC Mon–Fri).
Idempotency is enforced by the engine itself via run hash deduplication.
"""

from __future__ import annotations

from typing import Optional

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config.settings import Settings, get_settings
from ..engine import run_engine

log = structlog.get_logger(__name__)


def _scheduled_run(settings: Settings) -> None:
    log.info("scheduler.triggered")
    try:
        result = run_engine(settings=settings)
        log.info(
            "scheduler.run_complete",
            run_id=result.run_id,
            candidates=len(result.candidates),
        )
    except Exception as exc:
        log.error("scheduler.run_failed", error=str(exc), exc_info=True)


def start_scheduler(settings: Optional[Settings] = None) -> BackgroundScheduler:
    """
    Start the background scheduler and return the scheduler instance.

    Only activates if settings.scheduler.enabled is True.
    """
    settings = settings or get_settings()

    scheduler = BackgroundScheduler(timezone="UTC")

    if not settings.scheduler.enabled:
        log.info("scheduler.disabled")
        return scheduler

    cron_expr = settings.scheduler.cron  # e.g. "0 18 * * 1-5"
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
        id="daily_engine_run",
        replace_existing=True,
        misfire_grace_time=300,  # 5-minute grace if the server was briefly down
    )

    scheduler.start()
    log.info("scheduler.started", cron=cron_expr)
    return scheduler
