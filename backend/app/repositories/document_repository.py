import uuid

from sqlalchemy import func, select
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
