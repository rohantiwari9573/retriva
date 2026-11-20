"""Background recovery of stale PROCESSING documents, run by the API process.

Runs once shortly after startup (which is what recovers documents stranded by
the crash or restart that just happened) and then every
DOCUMENT_PROCESSING_SWEEP_INTERVAL_SECONDS. Each sweep re-queues abandoned
documents and dispatches them again, or fails those out of attempts; see
app/ingestion/lease.py. Safe with several API instances: each re-queue is a
compare-and-set, so only one instance wins any given document.
"""

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.logging import get_logger
from app.ingestion import lease
from app.ingestion.pipeline import cleanup_stale_workdirs
from app.ingestion.runner import dispatch, get_inline_runner

logger = get_logger(__name__)

STARTUP_DELAY_SECONDS = 5.0


async def sweep_once(
    session_factory: async_sessionmaker[AsyncSession], *, at_startup: bool = False
) -> lease.SweepResult:
    inline = settings.DOCUMENT_PROCESSING_MODE == "inline"
    exclude = get_inline_runner().scheduled if inline else frozenset()
    await asyncio.to_thread(cleanup_stale_workdirs)
    # In inline mode, documents are queued in the memory of the process that
    # accepted them. A process that has just started holds none, so every
    # unclaimed queued document belonged to a process that is gone: re-queue it
    # now rather than after DOCUMENT_PROCESSING_QUEUE_STALE_SECONDS. (Harmless
    # with several instances: a duplicate dispatch loses the claim.)
    result = await lease.sweep(
        session_factory, exclude=exclude, queue_stale_seconds=0 if inline and at_startup else None
    )
    for document_id in result.requeued:
        try:
            await dispatch(document_id)
        except Exception as exc:  # broker down: stays queued, next sweep retries
            logger.error(
                "document_processing_redispatch_failed", document_id=str(document_id), exc_info=exc
            )
    if result.requeued or result.failed:
        logger.info(
            "document_processing_sweep",
            requeued=len(result.requeued),
            failed=len(result.failed),
        )
    return result


async def sweep_forever(session_factory: async_sessionmaker[AsyncSession]) -> None:
    interval = settings.DOCUMENT_PROCESSING_SWEEP_INTERVAL_SECONDS
    await asyncio.sleep(min(STARTUP_DELAY_SECONDS, interval))
    first = True
    while True:
        try:
            await sweep_once(session_factory, at_startup=first)
            first = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # database down etc.: try again next interval
            logger.warning("document_processing_sweep_failed", exc_info=exc)
        await asyncio.sleep(interval)
