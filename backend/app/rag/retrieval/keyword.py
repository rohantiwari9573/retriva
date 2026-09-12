"""PostgreSQL native full-text keyword retrieval - no Elasticsearch/
OpenSearch, since Postgres's built-in text search is more than sufficient
at this project's scale and avoids running a second search engine.

Uses `websearch_to_tsquery`, not `plainto_tsquery` or a hand-rolled
`to_tsquery`: it accepts the query the way a user actually types it
(quoted phrases, `-word` exclusion, bare `OR`) without raising a syntax
error on stray punctuation the way `to_tsquery` would - the right choice
for a query that comes straight from a chat box.

Matches against `document_chunks.content_tsv`, a STORED generated column
(see the Phase 5 migration) with a GIN index - not a query-time
`to_tsvector(content)` call, which can't use a plain GIN index and would
recompute the tsvector on every single row on every query.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.retrieval.types import RankedHit

_SEARCH_SQL = text(
    """
    SELECT dc.id AS chunk_id,
           ts_rank(dc.content_tsv, websearch_to_tsquery('english', :query)) AS rank
    FROM document_chunks dc
    JOIN documents d ON d.id = dc.document_id
    WHERE d.organization_id = :organization_id
      AND d.status = 'READY'
      AND dc.content_tsv @@ websearch_to_tsquery('english', :query)
    ORDER BY rank DESC
    LIMIT :limit
    """
)


class KeywordRetriever:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def search(
        self, *, organization_id: uuid.UUID, query: str, limit: int
    ) -> list[RankedHit]:
        if not query.strip():
            return []
        result = await self.db.execute(
            _SEARCH_SQL,
            {"organization_id": str(organization_id), "query": query, "limit": limit},
        )
        return [RankedHit(chunk_id=row.chunk_id, score=float(row.rank)) for row in result]
