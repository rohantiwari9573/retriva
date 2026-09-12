"""HybridRetriever: orchestrates vector + keyword retrieval, fuses them via
RRF, and hydrates the winning chunk_ids into full RetrievedChunk records in
one batched query (never one query per chunk).

This is the only class in the retrieval package that touches the embedding
provider - VectorRetriever and KeywordRetriever are pure SQL, HybridRetriever
is where "turn a question into a vector" meets "search with that vector".
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.rag.embedding.base import EmbeddingProvider
from app.rag.retrieval.fusion import FusedResult, reciprocal_rank_fusion
from app.rag.retrieval.keyword import KeywordRetriever
from app.rag.retrieval.types import RetrievedChunk
from app.rag.retrieval.vector import VectorRetriever

logger = get_logger(__name__)


@dataclass(frozen=True)
class HybridRetrievalResult:
    chunks: list[RetrievedChunk]  # top-K, already fused and hydrated
    candidates_considered: int  # size of the union of both candidate sets
    best_vector_similarity: float | None  # for the insufficient-evidence check


class HybridRetriever:
    def __init__(self, db: AsyncSession, embedding_provider: EmbeddingProvider) -> None:
        self.db = db
        self.embedding_provider = embedding_provider
        self.vector = VectorRetriever(db)
        self.keyword = KeywordRetriever(db)

    async def retrieve(
        self,
        *,
        organization_id: uuid.UUID,
        query: str,
        candidate_pool: int | None = None,
        top_k: int | None = None,
    ) -> HybridRetrievalResult:
        candidate_pool = candidate_pool or settings.RETRIEVAL_CANDIDATE_POOL
        top_k = top_k or settings.RETRIEVAL_TOP_K

        query_embedding = await self.embedding_provider.embed_query(query)

        vector_hits = await self.vector.search_by_embedding(
            organization_id=organization_id, query_embedding=query_embedding, limit=candidate_pool
        )
        keyword_hits = await self.keyword.search(
            organization_id=organization_id, query=query, limit=candidate_pool
        )

        fused = reciprocal_rank_fusion(
            vector_hits,
            keyword_hits,
            vector_weight=settings.VECTOR_SEARCH_WEIGHT,
            keyword_weight=settings.KEYWORD_SEARCH_WEIGHT,
            k=settings.RRF_K,
        )
        top_fused = fused[:top_k]

        chunks = await self._hydrate(top_fused, organization_id=organization_id)

        best_vector_similarity = max((h.score for h in vector_hits), default=None)
        candidate_ids = {h.chunk_id for h in vector_hits} | {h.chunk_id for h in keyword_hits}

        return HybridRetrievalResult(
            chunks=chunks,
            candidates_considered=len(candidate_ids),
            best_vector_similarity=best_vector_similarity,
        )

    async def _hydrate(
        self, fused_results: list[FusedResult], *, organization_id: uuid.UUID
    ) -> list[RetrievedChunk]:
        if not fused_results:
            return []

        chunk_ids = [r.chunk_id for r in fused_results]
        stmt = (
            select(DocumentChunk, Document.original_filename)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.id.in_(chunk_ids),
                Document.organization_id == organization_id,
            )
        )
        result = await self.db.execute(stmt)
        by_id = {chunk.id: (chunk, filename) for chunk, filename in result}

        chunks: list[RetrievedChunk] = []
        for fused in fused_results:
            hydrated = by_id.get(fused.chunk_id)
            if hydrated is None:
                # Chunk was deleted/reprocessed between the candidate query
                # and hydration (e.g. a concurrent retry) - skip rather than
                # error, since the fused ranking is already best-effort.
                continue
            chunk, document_name = hydrated
            chunks.append(
                RetrievedChunk(
                    chunk_id=chunk.id,
                    document_id=chunk.document_id,
                    document_name=document_name,
                    content=chunk.content,
                    page_number=chunk.page_number,
                    section=chunk.section,
                    token_count=chunk.token_count,
                    vector_score=fused.vector_score,
                    keyword_score=fused.keyword_score,
                    fused_score=fused.fused_score,
                )
            )
        return chunks
