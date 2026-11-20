"""The actual ingestion pipeline, independent of Celery and FastAPI.

Celery tasks have no dependency-injection container, and this function's
tests need to run against a fake storage/embedding provider without a live
MinIO or LM Studio - so everything this needs is passed in explicitly rather
than imported as a module-level singleton. app/ingestion/runner.py supplies real
implementations and owns retry/failure bookkeeping around a call to this.

Shape of one attempt (bounded memory, no long transactions):

1. claim the document (app/ingestion/lease.py): a short conditional UPDATE.
   Duplicate deliveries and documents another live worker holds are skipped.
2. stream the stored file to a private temp file (deleted afterwards, also on
   failure) and parse it from disk; parsers stop at MAX_DOCUMENT_TEXT_LENGTH.
3. chunk, then embed in groups of DOCUMENT_EMBED_GROUP_SIZE, keeping each vector
   as float32 (array("f"), ~3 KB per chunk instead of ~25 KB as a list of Python
   floats) and heartbeating the lease between groups.
4. finish in one short transaction: lock the row, check we still hold the lease,
   replace this document's chunks (inserted in batches) and mark it READY. All or
   nothing: a crash before this commit leaves no partial chunks behind, and a
   worker that lost its lease writes nothing.

Before, the whole job ran inside one SELECT ... FOR UPDATE transaction (including
the download and every embedding call) and held every chunk's ORM object and
Python-float vector at once.
"""

import asyncio
import contextlib
import os
import tempfile
import time
from array import array
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.exceptions import StorageError, StorageObjectNotFoundError
from app.core.logging import get_logger
from app.core.metrics import (
    document_chunk_duration_seconds,
    document_embedding_duration_seconds,
    document_parse_duration_seconds,
    document_persist_duration_seconds,
    document_processing_duration_seconds,
)
from app.core.telemetry import get_tracer
from app.ingestion import isolated, lease
from app.ingestion.chunking import Chunk
from app.ingestion.errors import (
    PermanentProcessingError,
    TransientProcessingError,
)
from app.ingestion.schema_check import assert_embedding_dimension_matches
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.enums import DocumentStatus
from app.rag.embedding.base import (
    EmbeddingDimensionMismatchError,
    EmbeddingProvider,
    EmbeddingProviderUnavailableError,
)
from app.storage.base import StorageProvider

logger = get_logger(__name__)
tracer = get_tracer(__name__)

WORKDIR_PREFIX = "retriva-ingest-"
# A leftover work directory older than this is never in use: attempts take seconds
# to minutes (provider and storage calls all have timeouts).
STALE_WORKDIR_SECONDS = 3600
_active_workdirs: set[str] = set()


def cleanup_stale_workdirs(max_age_seconds: float = STALE_WORKDIR_SECONDS) -> int:
    """Removes ingestion work directories left by killed attempts. Skips any in use
    by this process and any younger than max_age_seconds (another process's)."""
    import shutil

    removed = 0
    root = tempfile.gettempdir()
    now = time.time()
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(WORKDIR_PREFIX):
            continue
        path = os.path.join(root, name)
        if path in _active_workdirs:
            continue
        try:
            if now - os.path.getmtime(path) < max_age_seconds:
                continue
            shutil.rmtree(path)
            removed += 1
        except OSError:
            continue
    if removed:
        logger.info("document_processing_workdirs_removed", count=removed)
    return removed


class PipelineOutcome(StrEnum):
    COMPLETED = "completed"
    # Nothing to do: deleted, not PROCESSING, or held by another live worker.
    SKIPPED = "skipped"
    # We lost the lease mid-way (it expired and another worker took over, or the
    # document was deleted/failed meanwhile); nothing was written.
    SUPERSEDED = "superseded"
    # Out of attempts (kept dying mid-way); marked FAILED.
    EXHAUSTED = "exhausted"


# Attribute set on any exception raised after the document was claimed: the
# lease that was held, so the runner can release it (transient) or fail the
# document (permanent) conditionally. The exception itself propagates unchanged.
CLAIM_ATTRIBUTE = "processing_claim"


def claim_of(exc: BaseException) -> "lease.Claim | None":
    return getattr(exc, CLAIM_ATTRIBUTE, None)


async def process_document_pipeline(
    document_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    storage: StorageProvider,
    embedding_provider: EmbeddingProvider,
) -> PipelineOutcome:
    claim = await lease.claim(session_factory, document_id)
    if claim is None:
        logger.info("document_processing_skipped", document_id=document_id)
        return PipelineOutcome.SKIPPED
    if claim.attempts > lease.max_attempts():
        await lease.fail(session_factory, claim, lease.INTERRUPTED_REASON)
        logger.error(
            "document_processing_failed_interrupted",
            document_id=document_id,
            attempts=claim.attempts,
        )
        return PipelineOutcome.EXHAUSTED
    try:
        return await _run_claimed(
            claim,
            session_factory=session_factory,
            storage=storage,
            embedding_provider=embedding_provider,
        )
    except Exception as exc:
        setattr(exc, CLAIM_ATTRIBUTE, claim)
        raise


async def _run_claimed(
    claim: "lease.Claim",
    *,
    session_factory: async_sessionmaker[AsyncSession],
    storage: StorageProvider,
    embedding_provider: EmbeddingProvider,
) -> PipelineOutcome:
    pipeline_start = time.perf_counter()
    with tracer.start_as_current_span("document.processing") as processing_span:
        processing_span.set_attribute("document.mime_type", claim.mime_type)
        logger.info(
            "document_processing_started",
            document_id=str(claim.document_id),
            organization_id=str(claim.organization_id),
            mime_type=claim.mime_type,
            attempt=claim.attempts,
        )
        async with session_factory() as session:
            await assert_embedding_dimension_matches(session, embedding_provider.dimensions)

        async with _keep_lease_alive(claim, session_factory):
            chunks = await _download_parse_and_chunk(claim, storage)
        if not await lease.heartbeat(session_factory, claim):
            return _superseded(claim, "after parsing")

        async with _keep_lease_alive(claim, session_factory):
            vectors = await _embed(claim, chunks, embedding_provider, session_factory)
        if vectors is None:
            return _superseded(claim, "while embedding")

        with tracer.start_as_current_span("document.persist"):
            persist_start = time.perf_counter()
            written = await _finish(claim, chunks, vectors, embedding_provider, session_factory)
            if not written:
                return _superseded(claim, "before committing")
            document_persist_duration_seconds.observe(time.perf_counter() - persist_start)

        processing_span.set_attribute("document.processing.status", "success")
        processing_span.set_attribute("document.chunk_count", len(chunks))

    document_processing_duration_seconds.observe(time.perf_counter() - pipeline_start)
    logger.info(
        "document_processing_completed",
        document_id=str(claim.document_id),
        organization_id=str(claim.organization_id),
        chunk_count=len(chunks),
    )
    return PipelineOutcome.COMPLETED


def _superseded(claim: "lease.Claim", where: str) -> PipelineOutcome:
    logger.warning(
        "document_processing_lease_lost", document_id=str(claim.document_id), where=where
    )
    return PipelineOutcome.SUPERSEDED


async def _download_parse_and_chunk(claim: "lease.Claim", storage: StorageProvider) -> list[Chunk]:
    # A private directory (0700) holds the downloaded original; it is removed when
    # this block exits, whatever happens inside it. A process killed mid-way can't
    # run that cleanup, so cleanup_stale_workdirs() (startup and every sweep)
    # removes leftovers that no live attempt in this process is using.
    with tempfile.TemporaryDirectory(prefix=WORKDIR_PREFIX) as workdir:
        _active_workdirs.add(workdir)
        try:
            return await _parse_and_chunk_in(workdir, claim, storage)
        finally:
            _active_workdirs.discard(workdir)


async def _parse_and_chunk_in(
    workdir: str, claim: "lease.Claim", storage: StorageProvider
) -> list[Chunk]:
    path = os.path.join(workdir, "source")
    try:
        with open(path, "wb") as target:
            await storage.download_to_file(claim.storage_key, target)
    except StorageObjectNotFoundError as exc:
        raise PermanentProcessingError(str(exc)) from exc
    except StorageError as exc:
        raise TransientProcessingError(str(exc)) from exc

    # Parse + chunk in a memory- and time-limited child process (see
    # app/ingestion/isolated.py): a crafted file can only kill the child.
    with tracer.start_as_current_span("document.parse") as span:
        parse_start = time.perf_counter()
        chunks = await isolated.parse_and_chunk(path, claim.mime_type, workdir)
        elapsed = time.perf_counter() - parse_start
        document_parse_duration_seconds.labels(document_type=claim.mime_type).observe(elapsed)
        document_chunk_duration_seconds.observe(elapsed)
        span.set_attribute("document.chunk_count", len(chunks))
    return chunks


@contextlib.asynccontextmanager
async def _keep_lease_alive(
    claim: "lease.Claim", session_factory: async_sessionmaker[AsyncSession]
):
    """Heartbeats in the background while long steps run (download, parse in the
    child process, embedding), so a slow but legitimate document keeps its lease
    and is never re-queued under a live worker. Heartbeat failures are ignored
    here: the step-boundary heartbeats decide whether we still hold the lease."""
    interval = max(1.0, min(60.0, settings.DOCUMENT_PROCESSING_LEASE_SECONDS / 4))

    async def beat() -> None:
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                await lease.heartbeat(session_factory, claim)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _embed(
    claim: "lease.Claim",
    chunks: list[Chunk],
    embedding_provider: EmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
) -> list[array] | None:
    """float32 vectors for every chunk, or None if the lease was lost meanwhile."""
    group = settings.DOCUMENT_EMBED_GROUP_SIZE
    vectors: list[array] = []
    with tracer.start_as_current_span("document.embed"):
        embed_start = time.perf_counter()
        for start in range(0, len(chunks), group):
            texts = [c.content for c in chunks[start : start + group]]
            try:
                batch = await embedding_provider.embed_documents(texts)
            except EmbeddingDimensionMismatchError as exc:
                raise PermanentProcessingError(str(exc)) from exc
            except EmbeddingProviderUnavailableError as exc:
                raise TransientProcessingError(str(exc)) from exc
            if len(batch) != len(texts):
                raise PermanentProcessingError(
                    f"Embedding provider returned {len(batch)} vectors for {len(texts)} chunks."
                )
            vectors.extend(array("f", vector) for vector in batch)
            del batch
            if start + group < len(chunks) and not await lease.heartbeat(session_factory, claim):
                return None
        document_embedding_duration_seconds.observe(time.perf_counter() - embed_start)
    return vectors


async def _finish(
    claim: "lease.Claim",
    chunks: list[Chunk],
    vectors: list[array],
    embedding_provider: EmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Replaces the document's chunks and marks it READY, atomically, only while
    we still hold the lease. Returns False (writing nothing) otherwise."""
    async with session_factory() as session:
        document = (
            await session.execute(
                select(Document)
                .where(Document.id == claim.document_id)
                .with_for_update()
                # Re-read under the lock even if this session already holds the
                # row: the lease columns were changed by Core UPDATEs.
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            document is None
            or document.status != DocumentStatus.PROCESSING
            or document.processing_token != claim.token
        ):
            await session.rollback()
            return False

        await session.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == claim.document_id)
        )
        batch_size = settings.DOCUMENT_INSERT_BATCH_SIZE
        for start in range(0, len(chunks), batch_size):
            rows = [
                {
                    "document_id": claim.document_id,
                    "chunk_index": chunk.chunk_index,
                    "content": chunk.content,
                    "page_number": chunk.page_number,
                    "section": chunk.section,
                    "char_count": chunk.char_count,
                    "token_count": chunk.token_count,
                    "content_hash": chunk.content_hash,
                    "embedding": vector.tolist(),
                }
                for chunk, vector in zip(
                    chunks[start : start + batch_size],
                    vectors[start : start + batch_size],
                    strict=True,
                )
            ]
            await session.execute(insert(DocumentChunk), rows)

        document.status = DocumentStatus.READY
        document.chunk_count = len(chunks)
        document.embedding_model = embedding_provider.model
        document.embedding_dimension = embedding_provider.dimensions
        document.processing_completed_at = datetime.now(UTC)
        document.processing_token = None
        document.processing_heartbeat_at = None
        document.failure_reason = None
        document.retry_count = max(claim.attempts - 1, 0)
        await session.commit()
    return True
