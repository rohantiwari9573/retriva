"""Deterministic score fusion for hybrid retrieval - Reciprocal Rank Fusion
(RRF), not a weighted sum of raw scores.

Why RRF instead of "normalize then add": pgvector cosine similarity lives in
roughly [-1, 1] (in practice usually [0, 1] for real embedding models) while
Postgres `ts_rank` is an unbounded, corpus- and query-dependent value with no
fixed ceiling - a two-word match can easily outscore a five-word match
depending on term frequency weights. Min-max normalizing *within each
candidate set* looks appealing but is mathematically deceptive here: it
forces the single best hit in every query to score exactly 1.0 regardless of
how relevant it actually is, which would make a downstream confidence
threshold on the fused score meaningless (a query with no good matches at
all would still produce a fused top score of 1.0). RRF sidesteps the whole
problem by fusing *rank position*, not value - a chunk ranked #1 by vector
search contributes `weight / (k + 1)` regardless of what the raw cosine
number happened to be. This is why the confidence threshold applied
downstream (RETRIEVAL_MIN_SIMILARITY) is checked against the raw vector
similarity of the best hit, never against a fused RRF score - see
app/rag/retrieval/hybrid.py.

k (RRF_K, default 60) is the standard damping constant from the original RRF
paper (Cormack et al., 2009) - it flattens the difference between rank 1 and
rank 2 so a single retriever's top pick doesn't totally dominate fusion.
"""

import uuid
from dataclasses import dataclass

from app.rag.retrieval.types import RankedHit


@dataclass(frozen=True)
class FusedResult:
    chunk_id: uuid.UUID
    fused_score: float
    vector_score: float | None
    vector_rank: int | None
    keyword_score: float | None
    keyword_rank: int | None


def reciprocal_rank_fusion(
    vector_hits: list[RankedHit],
    keyword_hits: list[RankedHit],
    *,
    vector_weight: float,
    keyword_weight: float,
    k: int,
) -> list[FusedResult]:
    """Both hit lists must already be sorted best-first (rank 1 = best) by
    their own retriever - fuse() only reads position, never re-sorts by the
    input `score` field itself. Returns every chunk that appeared in either
    list, sorted by fused_score descending, ties broken by chunk_id for a
    deterministic order across runs."""

    vector_by_id = {hit.chunk_id: (rank, hit.score) for rank, hit in enumerate(vector_hits, 1)}
    keyword_by_id = {hit.chunk_id: (rank, hit.score) for rank, hit in enumerate(keyword_hits, 1)}

    all_ids = set(vector_by_id) | set(keyword_by_id)
    results: list[FusedResult] = []
    for chunk_id in all_ids:
        vector_rank, vector_score = vector_by_id.get(chunk_id, (None, None))
        keyword_rank, keyword_score = keyword_by_id.get(chunk_id, (None, None))

        fused = 0.0
        if vector_rank is not None:
            fused += vector_weight / (k + vector_rank)
        if keyword_rank is not None:
            fused += keyword_weight / (k + keyword_rank)

        results.append(
            FusedResult(
                chunk_id=chunk_id,
                fused_score=fused,
                vector_score=vector_score,
                vector_rank=vector_rank,
                keyword_score=keyword_score,
                keyword_rank=keyword_rank,
            )
        )

    results.sort(key=lambda r: (-r.fused_score, str(r.chunk_id)))
    return results
