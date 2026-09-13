"""Structured logging configuration.

Uses structlog so logs are consistently structured (JSON in production, readable
console output in development). structlog.contextvars is merged into every log
line (see merge_contextvars below); app/workers/celery_app.py's task wrapper
binds document_id/task_id through it for the Celery worker, and (Phase 8)
app/core/request_id.py's middleware binds request_id for the FastAPI HTTP
path - see that module's docstring for why binding without ever clearing is
the correct, StreamingResponse-safe approach, which is what let Phase 7's
deferred request-ID middleware finally get built. trace_id/span_id are
stamped separately by _add_trace_context below, from OpenTelemetry's current
span rather than contextvars, since a span's lifetime is scoped by `with
tracer.start_as_current_span(...):` blocks, not by the request.
"""

import logging
import sys

import structlog
from opentelemetry import trace

from app.core.config import settings


def _add_trace_context(
    logger: object, method_name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Stamps trace_id/span_id from the currently active OpenTelemetry span
    onto every log line (Phase 8), so a log line and a trace for the same
    operation can be found from either direction - see docs/observability.md
    "Request ID vs. trace ID". A no-op when there is no active recording
    span (startup code, Celery beat, OTEL_ENABLED=false) - not every log
    line happens inside a traced operation, and that's fine, not a failure.
    Importing `opentelemetry.trace` here (rather than in app/core/telemetry.py)
    keeps this module self-contained - it's imported by nearly everything,
    including tests that never touch tracing at all - and reading the
    current span is a pure, dependency-free API call whether or not a real
    TracerProvider/exporter was ever configured."""
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def configure_logging() -> None:
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _add_trace_context,
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
