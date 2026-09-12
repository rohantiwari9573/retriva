"""Semantic vector retrieval via pgvector cosine distance.

Uses the existing ivfflat index and the existing embedding column - see
docs/retrieval.md for why the index isn't retuned in this phase (it was
built against an empty table in the Phase 4 migration, so its centroids
are untrained; Postgres will generally fall back to a sequential scan at
today's corpus size anyway, which is strictly more accurate).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.enums import DocumentStatus
from app.rag.retrieval.types import RankedHit


class VectorRetriever:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def search_by_embedding(
        self, *, organization_id: uuid.UUID, query_embedding: list[float], limit: int
    ) -> list[RankedHit]:
        distance = DocumentChunk.embedding.cosine_distance(query_embedding)
        similarity = (1 - distance).label("similarity")
        stmt = (
            select(DocumentChunk.id, similarity)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                Document.organization_id == organization_id,
                Document.status == DocumentStatus.READY,
            )
            .order_by(distance)
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return [RankedHit(chunk_id=row.id, score=float(row.similarity)) for row in result]
