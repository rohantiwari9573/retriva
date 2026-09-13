"""Celery application instance.

Kept as a separate module from FastAPI's app so `celery -A app.workers.celery_app`
doesn't import the web app's routing layer.
"""

import contextlib

from celery import Celery
from celery.signals import worker_process_init

from app.core.config import settings
from app.core.logging import configure_logging

celery_app = Celery(
    "nexus",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.workers.tasks.document_processing"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
)


@worker_process_init.connect
def _init_worker_observability(**kwargs) -> None:
    """Runs once per forked worker process, not at module import time -
    Celery's prefork pool forks after this module is imported, and a
    TracerProvider/BatchSpanProcessor created before the fork would leave
    every child worker sharing (and corrupting) the parent's background
    export thread. worker_process_init fires after the fork, in the child,
    which is the correct place to build per-process SDK state - the same
    reason app/workers/tasks/document_processing.py creates a fresh engine
    per task rather than reusing one built before any forking happened.
    """
    # Local imports: keep this module importable (by Beat, by `celery -A
    # ... inspect`, by tests) without eagerly pulling in the OTel SDK.
    from prometheus_client import start_http_server

    from app.core.telemetry import init_tracing, instrument_celery, instrument_httpx

    configure_logging()
    init_tracing()
    instrument_celery()
    instrument_httpx()

    if settings.PROMETHEUS_ENABLED:
        # The worker has no HTTP server of its own (see docker-compose.yml's
        # healthcheck comment on why) - celery_*/document_processing_*
        # Counters/Histograms (app/core/metrics.py) are recorded in this
        # process but would otherwise have nowhere for Prometheus to scrape
        # them from. Correct only under a single-process worker (see this
        # module's module docstring reference in docs/observability.md,
        # "Celery instrumentation": Celery's default prefork pool forks
        # multiple children, each with its own in-memory registry, which
        # start_http_server alone can't merge - the worker is run with
        # --concurrency=1 in docker-compose.yml specifically so this one
        # process's registry is the whole picture, not a fraction of it.
        # Already-bound (e.g. a second worker_process_init in the same
        # container from a prior restart) is suppressed - metrics from this
        # process are lost, not the process itself; never crash the worker.
        with contextlib.suppress(OSError):
            start_http_server(settings.WORKER_METRICS_PORT)
