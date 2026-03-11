"""
Application entry point.

Starts the FastAPI server and (optionally) the background scheduler.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog
import uvicorn

from src.api.app import create_app
from src.config.settings import get_settings
from src.scheduler.scheduler import start_scheduler

# ---------------------------------------------------------------------------
# Structured logging setup
# ---------------------------------------------------------------------------


def _configure_logging(log_level: str = "INFO") -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer() if os.getenv("ENV") != "production" else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper(), logging.INFO),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    settings = get_settings()
    _configure_logging(settings.log_level)

    log = structlog.get_logger("main")
    log.info(
        "startup",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
    )

    # Start scheduler (no-op if disabled)
    scheduler = start_scheduler(settings)

    # Create and run FastAPI app
    app = create_app(settings)

    try:
        uvicorn.run(
            app,
            host=settings.api.host,
            port=settings.api.port,
            workers=settings.api.workers,
            log_level=settings.log_level.lower(),
        )
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
