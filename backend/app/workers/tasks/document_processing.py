"""Celery entrypoint for document ingestion.

This module is a thin wrapper around app.ingestion.pipeline.process_document_pipeline:
its only job is bridging Celery's sync task interface to the pipeline's async
implementation, classifying failures as retryable or not, and persisting the
outcome (READY chunk_count/embedding metadata already happens inside the
pipeline's own transaction; FAILED status happens here, deliberately in a
*separate* session/transaction - see _mark_failed).

Why a separate session for failure handling: if process_document_pipeline
raises, the session it was using gets rolled back when its `async with`
block exits (SQLAlchemy's AsyncSession.close() rolls back any open
transaction) - so nothing written during the failed attempt (including a
status flip to PROCESSING) survives. That's correct for chunk rows, but the
failure itself still needs to be recorded, which requires a fresh
session/transaction opened after the failure is already known.

Why a fresh engine per task invocation, not app.core.database.engine: that
module-level engine's asyncpg connection pool binds to whatever event loop
first uses it. Celery invokes this task synchronously and bridges into
asyncio via asyncio.run() below, which creates a *new* event loop every
call - reusing the FastAPI process's engine here would eventually hand out a
connection whose pool is bound to a closed loop. NullPool with a
per-task engine avoids that at the cost of a fresh TCP connection per task,
which is an acceptable trade for a background job that isn't
latency-sensitive per-call.
"""

import asyncio
from datetime import UTC, datetime

import structlog
from celery.exceptions import Retry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.logging import get_logger
from app.ingestion.errors import (
    DocumentProcessingError,
    PermanentProcessingError,
    TransientProcessingError,
)
from app.ingestion.pipeline import process_document_pipeline
from app.models.document import Document
from app.models.enums import DocumentStatus
from app.rag.embedding.dependency import get_embedding_provider
from app.storage.dependency import get_storage_provider
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


class _RetryRequested(Exception):
    def __init__(self, original: Exception) -> None:
        self.original = original
        super().__init__(str(original))


def _session_factory() -> tuple[async_sessionmaker[AsyncSession], AsyncEngine]:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False), engine


async def _mark_failed(document_id: str, reason: str, retry_count: int) -> None:
    session_factory, engine = _session_factory()
    try:
        async with session_factory() as session:
            document = (
                await session.execute(select(Document).where(Document.id == document_id))
            ).scalar_one_or_none()
            if document is None:
                return
            document.status = DocumentStatus.FAILED
            document.failure_reason = reason
            document.retry_count = retry_count
            document.processing_completed_at = datetime.now(UTC)
            await session.commit()
    finally:
        await engine.dispose()


async def _record_retry_attempt(document_id: str, retry_count: int) -> None:
    session_factory, engine = _session_factory()
    try:
        async with session_factory() as session:
            document = (
                await session.execute(select(Document).where(Document.id == document_id))
            ).scalar_one_or_none()
            if document is not None:
                document.retry_count = retry_count
                await session.commit()
    finally:
        await engine.dispose()


async def _run_pipeline(document_id: str) -> None:
    session_factory, engine = _session_factory()
    try:
        await process_document_pipeline(
            document_id,
            session_factory=session_factory,
            storage=get_storage_provider(),
            embedding_provider=get_embedding_provider(),
        )
    except TransientProcessingError as exc:
        raise _RetryRequested(exc) from exc
    # EmbeddingSchemaMismatchError is a PermanentProcessingError subclass
    # (config/column drift - retrying the same document can't fix it) and is
    # deliberately left to propagate to the caller's PermanentProcessingError
    # handler rather than being retried here.
    finally:
        await engine.dispose()


@celery_app.task(
    bind=True,
    name="app.workers.tasks.document_processing.process_document",
    max_retries=settings.DOCUMENT_PROCESSING_MAX_RETRIES,
)
def process_document(self, document_id: str) -> None:
    structlog.contextvars.bind_contextvars(document_id=document_id, task_id=self.request.id)
    try:
        logger.info("document_processing_task_received", attempt=self.request.retries + 1)
        asyncio.run(_run_pipeline(document_id))
    except _RetryRequested as exc:
        countdown = settings.DOCUMENT_PROCESSING_RETRY_BACKOFF_SECONDS * (
            2**self.request.retries
        )
        asyncio.run(_record_retry_attempt(document_id, self.request.retries + 1))
        logger.warning(
            "document_processing_retry_scheduled",
            reason=str(exc.original),
            countdown=countdown,
            attempt=self.request.retries + 1,
        )
        try:
            # self.retry() raises Retry to signal an actual retry (the
            # normal, expected path against a real worker/broker) - that
            # must propagate up to Celery's own task machinery unchanged.
            # On exhaustion it raises either MaxRetriesExceededError, or
            # (when `exc=` was passed, as it always is here) re-raises the
            # *original* exception directly - see celery.app.task.Task.retry:
            # "if exc: raise_with_context(exc)" takes priority over raising
            # MaxRetriesExceededError once retries > max_retries. Both
            # exhaustion shapes are handled the same way below.
            raise self.retry(
                exc=exc.original,
                countdown=countdown,
                max_retries=settings.DOCUMENT_PROCESSING_MAX_RETRIES,
            )
        except Retry:
            raise
        except Exception as exhausted:  # noqa: BLE001
            reason = f"Processing failed after multiple attempts: {exc.original}"
            asyncio.run(_mark_failed(document_id, reason, self.request.retries))
            logger.error(
                "document_processing_failed_retries_exhausted",
                reason=str(exc.original),
                attempts=self.request.retries,
                exhausted_via=type(exhausted).__name__,
            )
    except PermanentProcessingError as exc:
        asyncio.run(_mark_failed(document_id, str(exc), self.request.retries))
        logger.error("document_processing_failed_permanent", reason=str(exc))
    except DocumentProcessingError as exc:
        # Any pipeline error not explicitly classified transient/permanent -
        # fail closed rather than retry indefinitely.
        asyncio.run(_mark_failed(document_id, str(exc), self.request.retries))
        logger.error("document_processing_failed_unclassified", reason=str(exc))
    except Exception as exc:  # noqa: BLE001 - last-resort safety net
        # Never leak the raw exception message to the document's public
        # failure_reason - it could contain a file path, connection string
        # fragment, or other internal detail. Full detail goes to the log only.
        asyncio.run(
            _mark_failed(
                document_id,
                "An unexpected error occurred while processing this document.",
                self.request.retries,
            )
        )
        logger.error("document_processing_failed_unexpected", exc_info=exc)
    finally:
        structlog.contextvars.clear_contextvars()
