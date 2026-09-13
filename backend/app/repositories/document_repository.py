import uuid
from typing import cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.enums import DocumentStatus


class DocumentRepository:
    """Every read here is scoped by organization_id - the tenant-isolation
    boundary for documents, same role get_org_context plays for org
    metadata/membership itself."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id_in_org(
        self, document_id: uuid.UUID, org_id: uuid.UUID
    ) -> Document | None:
        result = await self.db.execute(
            select(Document).where(
                Document.id == document_id, Document.organization_id == org_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_content_hash(
        self, org_id: uuid.UUID, content_hash: str
    ) -> Document | None:
        result = await self.db.execute(
            select(Document).where(
                Document.organization_id == org_id, Document.content_hash == content_hash
            )
        )
        return result.scalar_one_or_none()

    async def list_for_org(
        self, org_id: uuid.UUID, *, page: int, page_size: int
    ) -> tuple[list[Document], int]:
        count_result = await self.db.execute(
            select(func.count())
            .select_from(Document)
            .where(Document.organization_id == org_id)
        )
        total = count_result.scalar_one()

        result = await self.db.execute(
            select(Document)
            .where(Document.organization_id == org_id)
            .order_by(Document.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(result.scalars()), total

    async def create(
        self,
        *,
        org_id: uuid.UUID,
        uploaded_by: uuid.UUID,
        original_filename: str,
        mime_type: str,
        size_bytes: int,
        content_hash: str,
    ) -> Document:
        document = Document(
            organization_id=org_id,
            uploaded_by=uploaded_by,
            original_filename=original_filename,
            # Placeholder, replaced with a key derived from the row's own id
            # once flush() assigns one (see DocumentService). Must still be
            # unique on its own - storage_key has a UNIQUE constraint, and a
            # shared literal like "" would let two concurrent uploads collide
            # on that constraint before either gets to set its real key.
            storage_key=f"pending-{uuid.uuid4()}",
            mime_type=mime_type,
            size_bytes=size_bytes,
            content_hash=content_hash,
            status=DocumentStatus.UPLOADING,
        )
        self.db.add(document)
        await self.db.flush()
        await self.db.refresh(document)
        return document

    async def delete(self, document: Document) -> None:
        await self.db.delete(document)

    async def mark_processing_if_failed(
        self, document_id: uuid.UUID, org_id: uuid.UUID
    ) -> bool:
        """Atomically transition FAILED -> PROCESSING, returning whether this
        call was the one that made the transition.

        A plain read-then-write (check document.status, then set it) is a
        TOCTOU race: two concurrent retry requests can both read FAILED
        before either writes PROCESSING, both enqueue a worker, and the
        second worker's row-lock wait turns into a second, wasted processing
        run. The WHERE clause makes Postgres itself the arbiter - only the
        request whose UPDATE matches a still-FAILED row gets rowcount 1; a
        concurrent loser gets 0 and must not enqueue anything.
        """
        result = cast(
            CursorResult,
            await self.db.execute(
                update(Document)
                .where(
                    Document.id == document_id,
                    Document.organization_id == org_id,
                    Document.status == DocumentStatus.FAILED,
                )
                .values(status=DocumentStatus.PROCESSING, failure_reason=None)
            ),
        )
        return result.rowcount == 1
