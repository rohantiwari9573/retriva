"""Deterministic retrieval metrics - pure functions over a ranked list of
`RetrievedItem`s and a `QACase`, no database, no network. See
docs/evaluation.md's "Retrieval metrics" section for the definitions this
implements and why each was chosen.

METRIC DEFINITIONS (exact, per the Phase 10 spec's "document precisely,
never silently use an alternative definition" requirement):

- Recall@K: 1.0 if at least one relevant item (per eval_schemas.is_relevant)
  appears in the top K ranked results, else 0.0. This is "success/failure
  per query" recall (sometimes called Hit Rate@K in IR literature), NOT
  "fraction of all relevant items retrieved" - the fixture corpus rarely
  has more than one or two relevant chunks per question, so a
  proportion-of-total-relevant metric would mostly collapse to the same
  0/1 signal while being harder to explain. This choice is stated once,
  here, rather than left ambiguous.
- MRR (Mean Reciprocal Rank): for a single case, 1/rank of the first
  relevant item, or 0.0 if none appears in the ranked list at all (not just
  "not in top K" - MRR looks at the whole list passed in). The dataset-level
  MRR is the mean of per-case values.
- nDCG@K: standard normalized discounted cumulative gain with binary
  relevance (gain = 1 for a relevant item, 0 otherwise) and the standard
  log2 discount, i.e. DCG@K = sum_{i=1}^{K} gain_i / log2(i + 1), normalized
  by the ideal DCG@K (every relevant item placed first). With binary
  relevance and at most a handful of relevant chunks per question, nDCG
  here mostly agrees with Recall@K and MRR but rewards ranking a relevant
  item at position 1 over position 5 within the same top-K - which neither
  Recall@K (binary) nor MRR (only cares about the *first* hit, same value
  for one or many relevant items) directly captures.

Ties in ranking: this module never re-sorts or breaks ties - it trusts the
`rank` field on each `RetrievedItem` exactly as produced by the retriever
under test (which, for pgvector/RRF, already has its own documented,
deterministic tie-break - see app/rag/retrieval/fusion.py). A duplicate
chunk_id appearing twice in `items` (should not happen from a correct
retriever, but is defended against here per the Phase 10 regression-test
requirement) is treated as two independent ranked positions - deduplication
is the retriever's responsibility, not this module's.
"""

import math
from dataclasses import dataclass

from app.evaluation.eval_schemas import QACase, RetrievedItem, is_relevant


@dataclass(frozen=True)
class CaseRetrievalScore:
    case_id: str
    first_relevant_rank: int | None  # None if no relevant item appears anywhere in `items`
    recall_at: dict[int, float]  # k -> 0.0/1.0
    reciprocal_rank: float
    ndcg_at: dict[int, float]  # k -> 0.0..1.0


def _relevant_ranks(items: list[RetrievedItem], case: QACase) -> list[int]:
    """Ranks (1-indexed, as given by the retriever) of every relevant item,
    in the order they appear in `items` - not re-sorted."""
    return [item.rank for item in items if is_relevant(item, case)]


def recall_at_k(items: list[RetrievedItem], case: QACase, k: int) -> float:
    if k <= 0:
        raise ValueError("k must be positive")
    top_k = [item for item in items if item.rank <= k]
    return 1.0 if any(is_relevant(item, case) for item in top_k) else 0.0


def reciprocal_rank(items: list[RetrievedItem], case: QACase) -> float:
    ranks = _relevant_ranks(items, case)
    if not ranks:
        return 0.0
    return 1.0 / min(ranks)


def ndcg_at_k(items: list[RetrievedItem], case: QACase, k: int) -> float:
    if k <= 0:
        raise ValueError("k must be positive")
    top_k = sorted((item for item in items if item.rank <= k), key=lambda item: item.rank)
    gains = [1.0 if is_relevant(item, case) else 0.0 for item in top_k]
    dcg = sum(gain / math.log2(i + 2) for i, gain in enumerate(gains))  # i is 0-indexed here

    num_relevant = len(_relevant_ranks(items, case))
    ideal_hits = min(num_relevant, k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))
    if idcg == 0.0:
        # No relevant item exists anywhere (not just outside top-K) - nDCG
        # is conventionally undefined here; 0.0 is the standard convention
        # (a system that returns nothing relevant scores 0, not "N/A").
        return 0.0
    return dcg / idcg


def score_case(
    items: list[RetrievedItem],
    case: QACase,
    *,
    recall_ks: tuple[int, ...],
    ndcg_ks: tuple[int, ...],
) -> CaseRetrievalScore:
    ranks = _relevant_ranks(items, case)
    return CaseRetrievalScore(
        case_id=case.id,
        first_relevant_rank=min(ranks) if ranks else None,
        recall_at={k: recall_at_k(items, case, k) for k in recall_ks},
        reciprocal_rank=reciprocal_rank(items, case),
        ndcg_at={k: ndcg_at_k(items, case, k) for k in ndcg_ks},
    )


@dataclass(frozen=True)
class AggregateRetrievalMetrics:
    num_cases: int
    recall_at: dict[int, float]
    mrr: float
    ndcg_at: dict[int, float]


def aggregate(scores: list[CaseRetrievalScore]) -> AggregateRetrievalMetrics:
    if not scores:
        raise ValueError("Cannot aggregate an empty list of case scores.")
    recall_ks = sorted(scores[0].recall_at.keys())
    ndcg_ks = sorted(scores[0].ndcg_at.keys())
    n = len(scores)
    return AggregateRetrievalMetrics(
        num_cases=n,
        recall_at={k: sum(s.recall_at[k] for s in scores) / n for k in recall_ks},
        mrr=sum(s.reciprocal_rank for s in scores) / n,
        ndcg_at={k: sum(s.ndcg_at[k] for s in scores) / n for k in ndcg_ks},
    )
