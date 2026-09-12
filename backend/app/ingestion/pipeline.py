"""The actual ingestion pipeline, independent of Celery and FastAPI.

Celery tasks have no dependency-injection container, and this function's
tests need to run against a fake storage/embedding provider without a live
MinIO or LM Studio - so everything this needs is passed in explicitly rather
than imported as a module-level singleton. app/workers/tasks/document_processing.py
is the thin Celery wrapper that supplies real implementations and handles
retry/failure bookkeeping around a call to this function.

Idempotency: re-running this for the same document_id (a Celery retry, or a
redelivered task after `task_acks_late` triggers on worker loss) always
starts by deleting that document's existing chunks, so a partial or
duplicate run never leaves stray rows - the pipeline is safe to run twice.

Concurrency: the document row is locked with SELECT ... FOR UPDATE for the
duration of processing, so two workers that somehow both pick up the same
document_id serialize instead of racing to delete/insert each other's rows.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.exceptions import StorageError, StorageObjectNotFoundError
from app.core.logging import get_logger
from app.ingestion.chunking import chunk_document
from app.ingestion.errors import (
    EmptyDocumentError,
    PermanentProcessingError,
    TransientProcessingError,
)
from app.ingestion.parsers.registry import get_parser_for_mime_type
from app.ingestion.schema_check import assert_embedding_dimension_matches
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.enums import DocumentStatus
from app.rag.embedding.base import (
    EmbeddingDimensionMismatchError,
    EmbeddingProvider,
    EmbeddingProviderUnavailableError,
)
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.storage.base import StorageProvider

logger = get_logger(__name__)


async def process_document_pipeline(
    document_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    storage: StorageProvider,
    embedding_provider: EmbeddingProvider,
) -> None:
    async with session_factory() as session:
        document = (
            await session.execute(
                select(Document).where(Document.id == document_id).with_for_update()
            )
        ).scalar_one_or_none()

        if document is None:
            # The document row is gone (deleted while queued) - nothing to
            # process, and nothing to fail either.
            logger.warning("document_processing_skipped_not_found", document_id=document_id)
            return

        if document.status == DocumentStatus.READY:
            # Another delivery of the same task already finished this
            # document - the idempotent no-op path for task_acks_late
            # redelivery racing a successful first run.
            logger.info("document_processing_skipped_already_ready", document_id=document_id)
            return

        await assert_embedding_dimension_matches(session, embedding_provider.dimensions)

        chunk_repo = DocumentChunkRepository(session)
        await chunk_repo.delete_for_document(document.id)

        document.status = DocumentStatus.PROCESSING
        document.processing_started_at = datetime.now(UTC)
        document.failure_reason = None
        await session.flush()

        logger.info(
            "document_processing_started",
            document_id=str(document.id),
            organization_id=str(document.organization_id),
            mime_type=document.mime_type,
        )

        try:
            data = await storage.download(document.storage_key)
        except StorageObjectNotFoundError as exc:
            raise PermanentProcessingError(str(exc)) from exc
        except StorageError as exc:
            raise TransientProcessingError(str(exc)) from exc

        parser = get_parser_for_mime_type(document.mime_type)
        parsed = parser.parse(data)

        if not parsed.elements:
            raise EmptyDocumentError("Document contains no extractable text.")

        chunks = chunk_document(
            parsed,
            chunk_size_tokens=settings.CHUNK_SIZE_TOKENS,
            chunk_overlap_tokens=settings.CHUNK_OVERLAP_TOKENS,
        )

        if not chunks:
            raise EmptyDocumentError("Document contains no extractable text.")

        if len(chunks) > settings.MAX_CHUNKS_PER_DOCUMENT:
            raise PermanentProcessingError(
                f"Document produced {len(chunks)} chunks, exceeding the "
                f"{settings.MAX_CHUNKS_PER_DOCUMENT}-chunk processing limit."
            )

        try:
            vectors = await embedding_provider.embed_documents([c.content for c in chunks])
        except EmbeddingDimensionMismatchError as exc:
            raise PermanentProcessingError(str(exc)) from exc
        except EmbeddingProviderUnavailableError as exc:
            raise TransientProcessingError(str(exc)) from exc

        if len(vectors) != len(chunks):
            raise PermanentProcessingError(
                f"Embedding provider returned {len(vectors)} vectors for "
                f"{len(chunks)} chunks."
            )

        chunk_rows = [
            DocumentChunk(
                document_id=document.id,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                page_number=chunk.page_number,
                section=chunk.section,
                char_count=chunk.char_count,
                token_count=chunk.token_count,
                content_hash=chunk.content_hash,
                embedding=vector,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        await chunk_repo.bulk_create(chunk_rows)

        document.status = DocumentStatus.READY
        document.chunk_count = len(chunk_rows)
        document.embedding_model = embedding_provider.model
        document.embedding_dimension = embedding_provider.dimensions
        document.processing_completed_at = datetime.now(UTC)

        await session.commit()

        logger.info(
            "document_processing_completed",
            document_id=str(document.id),
            organization_id=str(document.organization_id),
            chunk_count=len(chunk_rows),
        )
