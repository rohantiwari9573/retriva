"""Structured logging configuration.

Uses structlog so logs are consistently structured (JSON in production, readable
console output in development). structlog.contextvars is merged into every log
line (see merge_contextvars below), and app/workers/celery_app.py's task
wrapper binds document_id/task_id through it for the Celery worker - but no
equivalent binding exists yet for the FastAPI HTTP request path (no
request-id middleware), so request/user/org correlation there still comes
from each call site's explicit kwargs, not ambient context. A request-id
middleware was considered for Phase 7 and deliberately deferred: it would
need to be scoped around a StreamingResponse's generator body correctly (the
generator runs after the endpoint handler returns, by which point a naive
middleware's contextvars.reset() would already have fired), which is real
scope beyond a security/reliability hardening pass - see docs/security.md.
"""

import logging
import sys

import structlog

from app.core.config import settings


def configure_logging() -> None:
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.LOG_FORMAT == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared_processors, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.LOG_LEVEL]
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    return structlog.get_logger(name)
