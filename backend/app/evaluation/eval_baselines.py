"""Retrieval baselines: vector-only, keyword-only, and the existing hybrid
RRF pipeline, run against the same corpus/top-K/candidate-pool for a fair
comparison (Phase 10 spec Step 5).

Composes the EXISTING retriever classes (`VectorRetriever`, `KeywordRetriever`,
`HybridRetriever` - app/rag/retrieval/) rather than reimplementing retrieval
logic - vector-only and keyword-only are literally single calls into the
same classes the production hybrid pipeline itself calls, not a parallel
implementation that could silently drift from what production actually
does. Nothing here is imported by app/services/rag_service.py or any
request-serving code path.
"""

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.evaluation.eval_schemas import QACase, RetrievedItem
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.rag.embedding.base import EmbeddingProvider
from app.rag.retrieval.hybrid import HybridRetriever
from app.rag.retrieval.keyword import KeywordRetriever
from app.rag.retrieval.types import RankedHit
from app.rag.retrieval.vector import VectorRetriever

STRATEGIES = ("vector_only", "keyword_only", "hybrid_rrf")


@dataclass(frozen=True)
class CaseRun:
    case: QACase
    items: list[RetrievedItem]
    latency_ms: float


async def _hydrate(
    db: AsyncSession, hits: list[RankedHit], organization_id: uuid.UUID
) -> list[RetrievedItem]:
    """Independent of HybridRetriever._hydrate (production, private) -
    evaluation-only, to keep this module decoupled from production
    internals per docs/evaluation.md's "No data leakage" section. Re-applies
    the organization_id filter (defense in depth, matching the same
    pattern documented in docs/retrieval.md's Tenant isolation section) even
    though every `hits` list here was already produced by an org-scoped
    query.
    """
    if not hits:
        return []
    chunk_ids = [hit.chunk_id for hit in hits]
    rows = (
        await db.execute(
            select(DocumentChunk.id, DocumentChunk.content, Document.original_filename)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(DocumentChunk.id.in_(chunk_ids), Document.organization_id == organization_id)
        )
    ).all()
    by_id = {row.id: row for row in rows}
    items: list[RetrievedItem] = []
    for rank, hit in enumerate(hits, start=1):
        row = by_id.get(hit.chunk_id)
        if row is None:
            continue  # chunk vanished between candidate query and hydration - skip, don't fabricate
        items.append(
            RetrievedItem(
                chunk_id=str(row.id),
                document_name=row.original_filename,
                content=row.content,
                rank=rank,
            )
        )
    return items


async def run_vector_only(
    db: AsyncSession,
    embedding_provider: EmbeddingProvider,
    organization_id: uuid.UUID,
    case: QACase,
    *,
    top_k: int,
) -> CaseRun:
    start = time.perf_counter()
    query_embedding = await embedding_provider.embed_query(case.question)
    hits = await VectorRetriever(db).search_by_embedding(
        organization_id=organization_id, query_embedding=query_embedding, limit=top_k
    )
    items = await _hydrate(db, hits, organization_id)
    return CaseRun(case=case, items=items, latency_ms=(time.perf_counter() - start) * 1000)


async def run_keyword_only(
    db: AsyncSession, organization_id: uuid.UUID, case: QACase, *, top_k: int
) -> CaseRun:
    start = time.perf_counter()
    hits = await KeywordRetriever(db).search(
        organization_id=organization_id, query=case.question, limit=top_k
    )
    items = await _hydrate(db, hits, organization_id)
    return CaseRun(case=case, items=items, latency_ms=(time.perf_counter() - start) * 1000)


async def run_hybrid_rrf(
    db: AsyncSession,
    embedding_provider: EmbeddingProvider,
    organization_id: uuid.UUID,
    case: QACase,
    *,
    top_k: int,
    candidate_pool: int,
) -> CaseRun:
    """Uses the real, unmodified HybridRetriever - not a reimplementation -
    so this baseline can never silently diverge from what production chat
    actually does."""
    start = time.perf_counter()
    result = await HybridRetriever(db, embedding_provider).retrieve(
        organization_id=organization_id,
        query=case.question,
        candidate_pool=candidate_pool,
        top_k=top_k,
    )
    items = [
        RetrievedItem(
            chunk_id=str(chunk.chunk_id),
            document_name=chunk.document_name,
            content=chunk.content,
            rank=rank,
        )
        for rank, chunk in enumerate(result.chunks, start=1)
    ]
    return CaseRun(case=case, items=items, latency_ms=(time.perf_counter() - start) * 1000)


async def run_strategy(
    strategy: str,
    db: AsyncSession,
    embedding_provider: EmbeddingProvider,
    organization_id: uuid.UUID,
    case: QACase,
    *,
    top_k: int | None = None,
    candidate_pool: int | None = None,
) -> CaseRun:
    """Single dispatch point so the CLI/report never has to special-case
    which retriever a strategy name maps to."""
    top_k = top_k or settings.RETRIEVAL_TOP_K
    candidate_pool = candidate_pool or settings.RETRIEVAL_CANDIDATE_POOL
    if strategy == "vector_only":
        return await run_vector_only(db, embedding_provider, organization_id, case, top_k=top_k)
    if strategy == "keyword_only":
        return await run_keyword_only(db, organization_id, case, top_k=top_k)
    if strategy == "hybrid_rrf":
        return await run_hybrid_rrf(
            db,
            embedding_provider,
            organization_id,
            case,
            top_k=top_k,
            candidate_pool=candidate_pool,
        )
    raise ValueError(f"Unknown retrieval strategy: {strategy!r}. Valid: {STRATEGIES}")
