"""Running document-processing attempts: retries, backoff, failure bookkeeping,
and the in-process scheduler used in inline mode.

run_attempt() is one attempt, synchronous, in its own event loop and with its own
NullPool engine (it runs in a worker thread in inline mode, or in a Celery worker
process). It never raises: it classifies the outcome so the caller only decides
whether and when to try again.

Inline mode (DOCUMENT_PROCESSING_MODE=inline, e.g. one Render free instance): the
InlineRunner runs at most DOCUMENT_PROCESSING_CONCURRENCY attempts at a time on a
dedicated executor (never asyncio's default one, which S3 and other to_thread
work share), waits for a free slot *before* claiming the lease, and sleeps out
retry backoff in the event loop with no slot held. Before, every upload or retry
started its own thread immediately (five concurrent 25 MiB retries ran five full
pipelines at once and the API was OOM-killed) and Task.apply() ran retries back
to back with no backoff at all.
"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.logging import get_logger
from app.ingestion import lease
from app.ingestion.errors import (
    DocumentProcessingError,
    PermanentProcessingError,
    TransientProcessingError,
)
from app.ingestion.pipeline import PipelineOutcome, claim_of, process_document_pipeline
from app.rag.embedding.dependency import get_embedding_provider
from app.storage.dependency import get_storage_provider

logger = get_logger(__name__)

UNEXPECTED_REASON = "An unexpected error occurred while processing this document."
# Attempts that could not even claim the document (database unreachable) before
# leaving it to the recovery sweep.
MAX_UNCLAIMED_RETRIES = 5


class AttemptKind(StrEnum):
    DONE = "done"  # READY
    SKIPPED = "skipped"  # nothing to do / another worker has it / lease lost
    RETRY = "retry"  # transient failure, released back to queued; try again later
    FAILED = "failed"  # terminal FAILED written (or exhausted)


@dataclass(frozen=True)
class AttemptResult:
    kind: AttemptKind
    attempts: int = 0
    reason: str | None = None

    def backoff_seconds(self) -> float:
        base = settings.DOCUMENT_PROCESSING_RETRY_BACKOFF_SECONDS
        return float(base * 2 ** max(self.attempts - 1, 0))


def _session_factory() -> tuple[async_sessionmaker[AsyncSession], AsyncEngine]:
    # A fresh engine per attempt: the attempt runs in its own event loop (a worker
    # thread or a Celery process), and a pooled connection is bound to the loop
    # that created it.
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False), engine


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, TransientProcessingError):
        return True
    # Database unreachable / connection dropped mid-attempt (OperationalError,
    # InterfaceError, asyncpg connection errors wrapped in DBAPIError with
    # connection_invalidated) and raw socket errors: worth retrying.
    if isinstance(exc, OperationalError | InterfaceError):
        return True
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    return isinstance(exc, ConnectionError | TimeoutError | OSError) and not isinstance(
        exc, DocumentProcessingError
    )


async def _attempt(document_id: str) -> AttemptResult:
    session_factory, engine = _session_factory()
    try:
        try:
            storage = get_storage_provider()
            embedding_provider = get_embedding_provider()
        except Exception as exc:
            # Misconfiguration: claim the document only to fail it with a safe
            # reason, rather than leave it queued forever.
            logger.error("document_processing_providers_unavailable", exc_info=exc)
            claim_ = await lease.claim(session_factory, document_id)
            if claim_ is None:
                return AttemptResult(AttemptKind.SKIPPED, reason=type(exc).__name__)
            await lease.fail(session_factory, claim_, UNEXPECTED_REASON)
            return AttemptResult(AttemptKind.FAILED, claim_.attempts, UNEXPECTED_REASON)
        try:
            outcome = await process_document_pipeline(
                document_id,
                session_factory=session_factory,
                storage=storage,
                embedding_provider=embedding_provider,
            )
        except Exception as exc:
            claim_ = claim_of(exc)
            if claim_ is not None:
                return await _handle_failure(session_factory, claim_, exc)
            # The claim itself failed (e.g. database unreachable).
            if _is_transient(exc):
                logger.warning("document_processing_claim_failed", reason=type(exc).__name__)
                return AttemptResult(AttemptKind.RETRY, attempts=0, reason=type(exc).__name__)
            logger.error("document_processing_claim_failed_unexpected", exc_info=exc)
            return AttemptResult(AttemptKind.SKIPPED, reason=type(exc).__name__)
        if outcome == PipelineOutcome.COMPLETED:
            return AttemptResult(AttemptKind.DONE)
        if outcome == PipelineOutcome.EXHAUSTED:
            return AttemptResult(AttemptKind.FAILED, reason=lease.INTERRUPTED_REASON)
        return AttemptResult(AttemptKind.SKIPPED, reason=outcome.value)
    finally:
        await engine.dispose()


async def _handle_failure(
    session_factory: async_sessionmaker[AsyncSession], claim: "lease.Claim", error: Exception
) -> AttemptResult:
    try:
        if _is_transient(error):
            if claim.attempts >= lease.max_attempts():
                reason = f"Processing failed after multiple attempts: {error}"
                await lease.fail(session_factory, claim, reason)
                logger.error(
                    "document_processing_failed_retries_exhausted",
                    reason=str(error),
                    attempts=claim.attempts,
                )
                return AttemptResult(AttemptKind.FAILED, claim.attempts, reason)
            released = await lease.release_for_retry(session_factory, claim)
            result = AttemptResult(
                AttemptKind.RETRY if released else AttemptKind.SKIPPED,
                claim.attempts,
                str(error),
            )
            logger.warning(
                "document_processing_retry_scheduled",
                reason=str(error),
                attempt=claim.attempts,
                countdown=result.backoff_seconds(),
            )
            return result
        if isinstance(error, PermanentProcessingError | DocumentProcessingError):
            # Permanent, or unclassified pipeline error: fail closed, no retry.
            await lease.fail(session_factory, claim, str(error))
            logger.error("document_processing_failed_permanent", reason=str(error))
            return AttemptResult(AttemptKind.FAILED, claim.attempts, str(error))
        # Never leak a raw exception message into failure_reason (it could contain
        # a path or connection details); full detail goes to the log only.
        await lease.fail(session_factory, claim, UNEXPECTED_REASON)
        logger.error("document_processing_failed_unexpected", exc_info=error)
        return AttemptResult(AttemptKind.FAILED, claim.attempts, UNEXPECTED_REASON)
    except Exception as bookkeeping_error:
        # Couldn't even record the outcome (database down): the lease simply
        # expires and the recovery sweep re-queues the document.
        logger.error(
            "document_processing_outcome_not_recorded",
            document_id=str(claim.document_id),
            exc_info=bookkeeping_error,
        )
        return AttemptResult(AttemptKind.SKIPPED, claim.attempts, "outcome not recorded")


def run_attempt(document_id: str) -> AttemptResult:
    """One attempt, synchronously, in a fresh event loop. Never raises."""
    return asyncio.run(_attempt(document_id))


class InlineRunner:
    """Bounded in-process processing for inline mode. One instance per API process."""

    def __init__(self, concurrency: int | None = None) -> None:
        self._concurrency = concurrency or settings.DOCUMENT_PROCESSING_CONCURRENCY
        self._executor = ThreadPoolExecutor(
            max_workers=self._concurrency, thread_name_prefix="document-processing"
        )
        self._slots: asyncio.Semaphore | None = None
        self._scheduled: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    @property
    def scheduled(self) -> frozenset[str]:
        """Documents queued or running in this process (the sweep skips them)."""
        return frozenset(self._scheduled)

    def schedule(self, document_id: uuid.UUID | str) -> None:
        key = str(document_id)
        if key in self._scheduled:
            return
        self._scheduled.add(key)
        task = asyncio.get_running_loop().create_task(self._drive(key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _drive(self, document_id: str) -> None:
        if self._slots is None:
            self._slots = asyncio.Semaphore(self._concurrency)
        loop = asyncio.get_running_loop()
        unclaimed_retries = 0
        try:
            while True:
                async with self._slots:
                    result = await loop.run_in_executor(self._executor, run_attempt, document_id)
                if result.kind != AttemptKind.RETRY:
                    return
                if result.attempts == 0:
                    # Couldn't even claim (database unreachable). Give up after a few
                    # tries; the document stays queued and the sweep re-queues it.
                    unclaimed_retries += 1
                    if unclaimed_retries > MAX_UNCLAIMED_RETRIES:
                        logger.warning(
                            "document_processing_left_for_sweep", document_id=document_id
                        )
                        return
                # Back off with no slot held, so other documents can run meanwhile.
                await asyncio.sleep(result.backoff_seconds())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let a scheduler task die silently
            logger.error("document_processing_scheduler_error", exc_info=exc)
        finally:
            self._scheduled.discard(document_id)

    def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        # Running attempts can't be interrupted; their leases expire and the next
        # process's sweep picks the documents up.
        self._executor.shutdown(wait=False, cancel_futures=True)


_inline_runner: InlineRunner | None = None


def get_inline_runner() -> InlineRunner:
    global _inline_runner
    if _inline_runner is None:
        _inline_runner = InlineRunner()
    return _inline_runner


def reset_inline_runner() -> None:
    """Tests and app shutdown."""
    global _inline_runner
    if _inline_runner is not None:
        _inline_runner.shutdown()
    _inline_runner = None


async def dispatch(document_id: uuid.UUID | str) -> None:
    """Hands a queued document to whatever processes documents in this deployment:
    the bounded in-process runner (inline mode) or the Celery broker. Raises if the
    broker can't be reached (callers decide what that means for the document)."""
    if settings.DOCUMENT_PROCESSING_MODE == "inline":
        get_inline_runner().schedule(document_id)
        return
    from app.workers.tasks.document_processing import process_document  # avoid import cycle

    await asyncio.to_thread(process_document.delay, str(document_id))
