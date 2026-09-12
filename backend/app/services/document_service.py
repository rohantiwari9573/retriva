"""Document upload/list/download/delete orchestration.

Upload flow (see also docs in app/models/document.py and the storage
package):
  1. Read the incoming stream in bounded chunks, hashing as we go and
     aborting the moment the size cap is exceeded - never trust
     Content-Length or UploadFile.size, both are client-supplied.
  2. Sniff the full buffered content against the claimed extension.
  3. Reject exact-duplicate content already uploaded to this org.
  4. Insert the document row (status=UPLOADING) to get a UUID.
  5. Upload to storage under a key derived from that UUID - never from the
     client-supplied filename, which is kept only as display metadata.
  6. On success, flip to PROCESSING and commit *before* enqueueing the
     Celery task - the worker looks the document up by id in its own
     transaction, so it must already be durably committed by the time the
     task can possibly run.
  7. Enqueue app.workers.tasks.document_processing.process_document. If that
     enqueue itself fails (e.g. Redis/broker unreachable), the document
     would otherwise sit in PROCESSING forever with nothing ever picking it
     up - so this flips it straight to FAILED with a retryable reason
     instead, matching the "no document stuck permanently in PROCESSING"
     requirement.
  8. On storage failure, flip to FAILED and commit that fact immediately -
     otherwise get_db's rollback-on-exception would erase the failure record
     along with everything else in the transaction.

Parsing, chunking, and embedding themselves happen entirely in the Celery
worker (app/ingestion/pipeline.py) - this module's job stops at handing off
a durably-stored, durably-committed document for that worker to pick up.
"""

import asyncio
import hashlib
import uuid

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    ProcessingQueueError,
)
from app.core.logging import get_logger
from app.models.document import Document
from app.models.enums import DocumentStatus
from app.repositories.document_repository import DocumentRepository
from app.services.file_validation import detect_and_validate_file_type
from app.storage.base import StorageProvider
from app.workers.tasks.document_processing import process_document

logger = get_logger(__name__)

_READ_CHUNK_SIZE = 1024 * 1024  # 1 MB


class DocumentService:
    def __init__(self, db: AsyncSession, storage: StorageProvider) -> None:
        self.db = db
        self.storage = storage
        self.documents = DocumentRepository(db)

    async def upload(
        self, *, org_id: uuid.UUID, uploaded_by: uuid.UUID, file: UploadFile
    ) -> Document:
        data, content_hash = await self._read_and_hash(file)
        detected = detect_and_validate_file_type(file.filename or "", data)

        existing = await self.documents.get_by_content_hash(org_id, content_hash)
        if existing is not None:
            raise ConflictError(
                "This exact file has already been uploaded to this organization.",
                code="DUPLICATE_DOCUMENT",
            )

        document = await self.documents.create(
            org_id=org_id,
            uploaded_by=uploaded_by,
            original_filename=_sanitize_display_filename(file.filename or "upload"),
            mime_type=detected.mime_type,
            size_bytes=len(data),
            content_hash=content_hash,
        )

        storage_key = f"organizations/{org_id}/documents/{document.id}{detected.extension}"
        try:
            await self.storage.upload(storage_key, data, detected.mime_type)
        except Exception:
            document.status = DocumentStatus.FAILED
            await self.db.commit()
            raise

        document.storage_key = storage_key
        document.status = DocumentStatus.PROCESSING
        await self.db.commit()

        await self._enqueue_processing(document)
        return document

    async def retry(self, document: Document) -> Document:
        """Re-queue a FAILED document. Only FAILED is a valid source state -
        this is not a general-purpose "restart processing" button; a
        PROCESSING or READY document already has a worker on it or is done."""
        if document.status != DocumentStatus.FAILED:
            raise ConflictError(
                "Only a failed document can be retried.", code="INVALID_STATUS_TRANSITION"
            )
        document.status = DocumentStatus.PROCESSING
        document.failure_reason = None
        await self.db.commit()

        await self._enqueue_processing(document)
        return document

    async def _enqueue_processing(self, document: Document) -> None:
        try:
            await asyncio.to_thread(process_document.delay, str(document.id))
        except Exception as exc:
            # The document is durably PROCESSING but nothing will ever pick
            # it up if the broker is unreachable - fail it now rather than
            # leave it stuck forever with no worker ever assigned.
            logger.error("document_enqueue_failed", document_id=str(document.id), exc_info=exc)
            document.status = DocumentStatus.FAILED
            document.failure_reason = (
                "Could not queue this document for processing. Please try again."
            )
            await self.db.commit()
            raise ProcessingQueueError(
                "Document uploaded, but could not be queued for processing. "
                "Please retry."
            ) from exc

    async def list_for_org(
        self, org_id: uuid.UUID, *, page: int, page_size: int
    ) -> tuple[list[Document], int]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), settings.DOCUMENTS_PAGE_SIZE_MAX)
        return await self.documents.list_for_org(org_id, page=page, page_size=page_size)

    async def get_or_404(self, document_id: uuid.UUID, org_id: uuid.UUID) -> Document:
        document = await self.documents.get_by_id_in_org(document_id, org_id)
        if document is None:
            raise NotFoundError("Document not found.", code="DOCUMENT_NOT_FOUND")
        return document

    async def get_download_url(self, document: Document) -> tuple[str, int]:
        expires_in = settings.DOWNLOAD_URL_EXPIRE_SECONDS
        url = await self.storage.generate_presigned_download_url(
            document.storage_key, document.original_filename, expires_in
        )
        return url, expires_in

    async def delete(self, document: Document) -> None:
        # Storage delete first: if it fails, the DB row survives so the
        # document isn't silently "gone" from the user's perspective while
        # still consuming storage - a stuck-but-visible failure is easier to
        # diagnose and retry than an invisible orphaned object.
        await self.storage.delete(document.storage_key)
        await self.documents.delete(document)

    async def _read_and_hash(self, file: UploadFile) -> tuple[bytes, str]:
        max_bytes = settings.MAX_DOCUMENT_SIZE_MB * 1024 * 1024
        hasher = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0

        while True:
            chunk = await file.read(_READ_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise PayloadTooLargeError(
                    f"File exceeds the maximum size of {settings.MAX_DOCUMENT_SIZE_MB} MB."
                )
            hasher.update(chunk)
            chunks.append(chunk)

        return b"".join(chunks), hasher.hexdigest()


def _sanitize_display_filename(filename: str) -> str:
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    # Strip control characters that could mangle headers/logs/UI when this
    # is later echoed back (e.g. in Content-Disposition) - this is a display
    # safeguard only, not the path-traversal boundary (the storage key never
    # incorporates this value at all).
    name = "".join(ch for ch in name if ch.isprintable())
    return name[:500] or "upload"
