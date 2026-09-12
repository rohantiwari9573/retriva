"""Shared value types for the retrieval pipeline.

Kept dependency-free (no SQLAlchemy models, no DB session) so fusion.py can
be unit-tested as a pure function and so nothing here forces a particular
storage layer.
"""

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class RankedHit:
    """One retriever's opinion about one chunk: its identity plus a
    single-source score. `score` is deliberately typed generically -
    vector search fills it with cosine similarity, keyword search with
    ts_rank - fuse() only ever uses *rank within its own list*, never the
    raw score across lists, so the incompatible scales never have to meet."""

    chunk_id: uuid.UUID
    score: float


@dataclass(frozen=True)
class RetrievedChunk:
    """A chunk plus everything the context builder and citation validator
    need, already joined against `documents` - the retrieval layer is the
    only place that touches the DB, so everything downstream works with
    plain data."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    content: str
    page_number: int | None
    section: str | None
    token_count: int
    vector_score: float | None  # raw cosine similarity, None if not a vector hit
    keyword_score: float | None  # raw ts_rank, None if not a keyword hit
    fused_score: float  # RRF score, used only for ranking - not a probability
