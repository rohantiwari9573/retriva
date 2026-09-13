"""Telemetry failure isolation (Phase 8, Step 33): a bad/unreachable OTel
endpoint or OTEL_ENABLED=false must never raise out of init_tracing() or any
instrument_*() helper - see app/core/telemetry.py's module docstring."""

from opentelemetry import trace

from app.core import telemetry
from app.core.config import settings


def test_init_tracing_disabled_does_not_raise(monkeypatch):
    monkeypatch.setattr(settings, "OTEL_ENABLED", False)
    telemetry.init_tracing()  # must not raise


def test_init_tracing_with_unreachable_endpoint_does_not_raise(monkeypatch):
    monkeypatch.setattr(settings, "OTEL_ENABLED", True)
    monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    telemetry.init_tracing()  # constructing the exporter must not raise or block


def test_get_tracer_returns_usable_tracer_even_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "OTEL_ENABLED", False)
    telemetry.init_tracing()
    tracer = telemetry.get_tracer(__name__)
    with tracer.start_as_current_span("test.span"):
        pass  # must not raise - a NoOp tracer still supports the context manager protocol


def test_instrumentation_helpers_are_safe_to_call_repeatedly(monkeypatch):
    """instrument_httpx()/instrument_redis() are called once at FastAPI
    startup and again per Celery worker process - calling them twice in the
    same process (as happens when both main.py and a worker fixture import
    this module in the same test run) must not raise."""
    monkeypatch.setattr(settings, "OTEL_ENABLED", True)
    telemetry.instrument_httpx()
    telemetry.instrument_httpx()
    telemetry.instrument_redis()
    telemetry.instrument_redis()


def test_current_span_context_invalid_outside_any_span():
    ctx = trace.get_current_span().get_span_context()
    assert not ctx.is_valid
