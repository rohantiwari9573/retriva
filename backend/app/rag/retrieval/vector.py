"""Semantic vector retrieval via pgvector cosine distance.

Search is exact: there is no approximate (IVFFlat or HNSW) index on the embedding
column, so Postgres computes the distance to every READY chunk of the organization
and returns the true nearest `limit`. The ix_documents_org_status and
ix_document_chunks_document_id indexes narrow the scan to that one organization's
chunks first, so cost grows with the tenant's corpus, not the whole table.

The IVFFlat index from the Phase 4 migration was dropped in b4f2c81d9e37: it
applied the tenant filter after an approximate scan and silently returned too few
rows. Don't add one back without a strategy for tenant filtering. See
docs/retrieval.md and tests/integration/test_vector_search_correctness.py.
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
