import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document_chunk import DocumentChunk


class DocumentChunkRepository:
    """Every method takes document_id, never organization_id directly - chunks
    have no organization_id column of their own (see DocumentChunk's
    docstring), so tenant isolation is enforced by callers only ever holding
    a document_id they already fetched via DocumentRepository.get_by_id_in_org.
    There is no get-by-chunk-id-alone method for exactly that reason: it
    would make it too easy to bypass the org check by accident."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def delete_for_document(self, document_id: uuid.UUID) -> None:
        await self.db.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
        )

    async def bulk_create(self, chunks: list[DocumentChunk]) -> None:
        self.db.add_all(chunks)
        await self.db.flush()

    async def list_for_document(self, document_id: uuid.UUID) -> list[DocumentChunk]:
        result = await self.db.execute(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)
        )
        return list(result.scalars())

    async def count_for_document(self, document_id: uuid.UUID) -> int:
        result = await self.db.execute(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
        )
        return result.scalar_one()
