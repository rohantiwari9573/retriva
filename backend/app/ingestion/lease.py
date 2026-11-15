"""Processing lease: who is processing a document, and since when.

A PROCESSING document is in one of two states:

- queued: processing_token IS NULL. processing_heartbeat_at is when it was
  queued (upload, retry, re-queue after a transient failure or by the sweep).
- claimed: processing_token is the claiming worker's token, and
  processing_heartbeat_at is that worker's last heartbeat.

Every transition is one conditional UPDATE (compare-and-set), using the database
clock, so concurrent workers, duplicate task deliveries, API instances and the
recovery sweep can never both win:

- claim: queued, or claimed with an expired lease (its worker died). Increments
  processing_attempts; a document past DOCUMENT_PROCESSING_MAX_RETRIES + 1
  attempts is failed instead of processed (one that keeps killing its worker).
- heartbeat / release / fail / finish: only while still holding our token.
- sweep: re-queues (or fails, when out of attempts) documents whose queue entry
  or lease has gone stale - an API restart mid-processing, an OOM kill, a lost
  in-process task or broker message. Nothing stays PROCESSING indefinitely.

Before this, the pipeline held one SELECT ... FOR UPDATE transaction for the
whole job (download, parse, external embedding calls), so a killed process left
the committed PROCESSING row with nothing that would ever revisit it.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import CursorResult, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.logging import get_logger
from app.models.document import Document
from app.models.enums import DocumentStatus

logger = get_logger(__name__)

INTERRUPTED_REASON = (
    "Processing was interrupted repeatedly and could not be completed. Please retry the document."
)


def max_attempts() -> int:
    return settings.DOCUMENT_PROCESSING_MAX_RETRIES + 1


def mark_queued(document: Document, *, reset_attempts: bool = True) -> None:
    """In-session (ORM) transition to queued, for the upload path."""
    document.status = DocumentStatus.PROCESSING
    document.processing_token = None
    document.processing_heartbeat_at = datetime.now(UTC)
    document.failure_reason = None
    if reset_attempts:
        document.processing_attempts = 0


@dataclass(frozen=True)
class Claim:
    document_id: uuid.UUID
    token: uuid.UUID
    organization_id: uuid.UUID
    storage_key: str
    mime_type: str
    attempts: int


def _lease_expired():
    return Document.processing_heartbeat_at < func.now() - timedelta(
        seconds=settings.DOCUMENT_PROCESSING_LEASE_SECONDS
    )


async def claim(session_factory: async_sessionmaker[AsyncSession], document_id) -> Claim | None:
    """Claims a queued (or abandoned) document. None if there is nothing to do:
    deleted, not PROCESSING (READY/FAILED), or held by a live worker."""
    token = uuid.uuid4()
    async with session_factory() as session:
        row = (
            await session.execute(
                update(Document)
                .where(
                    Document.id == document_id,
                    Document.status == DocumentStatus.PROCESSING,
                    or_(Document.processing_token.is_(None), _lease_expired()),
                )
                .values(
                    processing_token=token,
                    processing_heartbeat_at=func.now(),
                    processing_started_at=func.now(),
                    processing_attempts=Document.processing_attempts + 1,
                    failure_reason=None,
                )
                .returning(
                    Document.organization_id,
                    Document.storage_key,
                    Document.mime_type,
                    Document.processing_attempts,
                )
                .execution_options(synchronize_session=False)
            )
        ).one_or_none()
        await session.commit()
    if row is None:
        return None
    return Claim(
        document_id=uuid.UUID(str(document_id)),
        token=token,
        organization_id=row.organization_id,
        storage_key=row.storage_key,
        mime_type=row.mime_type,
        attempts=row.processing_attempts,
    )


def _held(claim_: Claim):
    return and_(
        Document.id == claim_.document_id,
        Document.status == DocumentStatus.PROCESSING,
        Document.processing_token == claim_.token,
    )


async def heartbeat(session_factory: async_sessionmaker[AsyncSession], claim_: Claim) -> bool:
    """Extends the lease. False if we no longer hold it (re-claimed after expiry,
    deleted, or failed by someone else) - the caller should stop."""
    async with session_factory() as session:
        result = cast(
            CursorResult,
            await session.execute(
                update(Document)
                .where(_held(claim_))
                .values(processing_heartbeat_at=func.now())
                .execution_options(synchronize_session=False)
            ),
        )
        await session.commit()
    return result.rowcount == 1


async def release_for_retry(
    session_factory: async_sessionmaker[AsyncSession], claim_: Claim
) -> bool:
    """Back to queued after a transient failure; keeps the attempt count."""
    async with session_factory() as session:
        result = cast(
            CursorResult,
            await session.execute(
                update(Document)
                .where(_held(claim_))
                .values(
                    processing_token=None,
                    processing_heartbeat_at=func.now(),
                    retry_count=claim_.attempts,
                )
                .execution_options(synchronize_session=False)
            ),
        )
        await session.commit()
    return result.rowcount == 1


async def fail(
    session_factory: async_sessionmaker[AsyncSession], claim_: Claim, reason: str
) -> bool:
    """Terminal FAILED, only while we still hold the lease."""
    async with session_factory() as session:
        result = cast(
            CursorResult,
            await session.execute(
                update(Document)
                .where(_held(claim_))
                .values(
                    status=DocumentStatus.FAILED,
                    failure_reason=reason,
                    retry_count=max(claim_.attempts - 1, 0),
                    processing_token=None,
                    processing_heartbeat_at=None,
                    processing_completed_at=func.now(),
                )
                .execution_options(synchronize_session=False)
            ),
        )
        await session.commit()
    return result.rowcount == 1


@dataclass(frozen=True)
class SweepResult:
    requeued: list[uuid.UUID]
    failed: list[uuid.UUID]


async def sweep(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    exclude: set[str] | frozenset[str] = frozenset(),
    limit: int = 100,
    queue_stale_seconds: int | None = None,
) -> SweepResult:
    """Re-queues stale PROCESSING documents, or fails those out of attempts.

    Stale means: queued for longer than DOCUMENT_PROCESSING_QUEUE_STALE_SECONDS
    with no worker claiming it, claimed with no heartbeat for
    DOCUMENT_PROCESSING_LEASE_SECONDS, or a NULL heartbeat (stranded before the
    lease columns existed). `exclude` holds ids this process has scheduled
    itself (queued in memory, waiting for a slot), which are not stale.
    `queue_stale_seconds` overrides the queue threshold (0 at inline startup).
    """
    stale_after = (
        settings.DOCUMENT_PROCESSING_QUEUE_STALE_SECONDS
        if queue_stale_seconds is None
        else queue_stale_seconds
    )
    queue_stale = func.now() - timedelta(seconds=stale_after)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    Document.id,
                    Document.processing_token,
                    Document.processing_heartbeat_at,
                    Document.processing_attempts,
                )
                .where(
                    Document.status == DocumentStatus.PROCESSING,
                    or_(
                        Document.processing_heartbeat_at.is_(None),
                        and_(
                            Document.processing_token.is_(None),
                            Document.processing_heartbeat_at <= queue_stale,
                        ),
                        and_(Document.processing_token.is_not(None), _lease_expired()),
                    ),
                )
                .order_by(Document.processing_heartbeat_at.asc().nulls_first())
                .limit(limit)
            )
        ).all()

        requeued: list[uuid.UUID] = []
        failed: list[uuid.UUID] = []
        for row in rows:
            if str(row.id) in exclude:
                continue
            unchanged = and_(
                Document.id == row.id,
                Document.status == DocumentStatus.PROCESSING,
                Document.processing_token.is_(None)
                if row.processing_token is None
                else Document.processing_token == row.processing_token,
                Document.processing_heartbeat_at.is_(None)
                if row.processing_heartbeat_at is None
                else Document.processing_heartbeat_at == row.processing_heartbeat_at,
            )
            if row.processing_attempts >= max_attempts():
                values = {
                    "status": DocumentStatus.FAILED,
                    "failure_reason": INTERRUPTED_REASON,
                    "processing_token": None,
                    "processing_heartbeat_at": None,
                    "processing_completed_at": func.now(),
                }
                target = failed
            else:
                values = {"processing_token": None, "processing_heartbeat_at": func.now()}
                target = requeued
            result = cast(
                CursorResult,
                await session.execute(
                    update(Document)
                    .where(unchanged)
                    .values(**values)
                    .execution_options(synchronize_session=False)
                ),
            )
            if result.rowcount == 1:
                target.append(row.id)
        await session.commit()

    for document_id in requeued:
        logger.warning("document_processing_requeued_stale", document_id=str(document_id))
    for document_id in failed:
        logger.error("document_processing_failed_interrupted", document_id=str(document_id))
    return SweepResult(requeued=requeued, failed=failed)
