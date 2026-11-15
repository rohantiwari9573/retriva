"""Celery entrypoint for document ingestion.

A thin wrapper around app.ingestion.runner.run_attempt, which claims the
document's processing lease, runs the pipeline, and records the outcome (all
conditional on still holding the lease - see app/ingestion/lease.py). This task
only turns a transient failure into a Celery retry with exponential backoff and
records metrics.

Duplicate deliveries are harmless: task_acks_late redelivery after a worker is
lost, or a re-queue by the recovery sweep, finds the document already claimed by
a live worker (skipped), READY (skipped), or abandoned (claimed and resumed,
counting one more attempt). A document that keeps killing its worker is failed
after DOCUMENT_PROCESSING_MAX_RETRIES + 1 attempts instead of looping forever.
"""

import time

import structlog
from celery.exceptions import Retry

from app.core.logging import get_logger
from app.core.metrics import (
    celery_task_duration_seconds,
    celery_task_failures_total,
    celery_task_retries_total,
    celery_tasks_total,
    document_processing_total,
)
from app.ingestion.runner import MAX_UNCLAIMED_RETRIES, AttemptKind, run_attempt
from app.workers.celery_app import celery_app

logger = get_logger(__name__)

_TASK_NAME = "app.workers.tasks.document_processing.process_document"


@celery_app.task(bind=True, name=_TASK_NAME, max_retries=None)
def process_document(self, document_id: str) -> None:
    structlog.contextvars.bind_contextvars(document_id=document_id, task_id=self.request.id)
    outcome = "success"
    task_start = time.perf_counter()
    try:
        logger.info("document_processing_task_received", delivery=self.request.retries + 1)
        result = run_attempt(document_id)
        if result.kind == AttemptKind.RETRY:
            if result.attempts == 0 and self.request.retries >= MAX_UNCLAIMED_RETRIES:
                # Couldn't claim repeatedly (database down): stop; the document
                # stays queued and the recovery sweep re-queues it later.
                outcome = "skipped"
                return
            outcome = "retry"
            try:
                raise self.retry(countdown=result.backoff_seconds())
            except Retry:
                celery_task_retries_total.labels(task_name=_TASK_NAME).inc()
                raise
        elif result.kind == AttemptKind.FAILED:
            outcome = "failure"
        elif result.kind == AttemptKind.SKIPPED:
            outcome = "skipped"
    finally:
        celery_task_duration_seconds.labels(task_name=_TASK_NAME).observe(
            time.perf_counter() - task_start
        )
        celery_tasks_total.labels(task_name=_TASK_NAME, status=outcome).inc()
        if outcome == "failure":
            celery_task_failures_total.labels(task_name=_TASK_NAME).inc()
            document_processing_total.labels(status="failure").inc()
        elif outcome == "success":
            document_processing_total.labels(status="success").inc()
        structlog.contextvars.clear_contextvars()
