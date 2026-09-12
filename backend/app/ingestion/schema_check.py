"""Runtime guard against the pgvector column drifting from configuration.

The embedding dimension is baked into the document_chunks.embedding column
at migration-write time (see the Phase 4 migration). If someone later
changes EMBEDDING_MODEL to a model with a different output width and bumps
EMBEDDING_DIMENSIONS to match, without also writing a migration to resize
the column, every insert would fail with an opaque Postgres error. This
check catches that mismatch explicitly, with a message that names both
numbers and says what to do.
"""

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.errors import PermanentProcessingError

_VECTOR_TYPE_RE = re.compile(r"vector\((\d+)\)")


class EmbeddingSchemaMismatchError(PermanentProcessingError):
    """A configuration/schema drift, not a per-document problem - retrying
    the same document changes nothing while EMBEDDING_DIMENSIONS and the
    document_chunks.embedding column disagree, so this is always permanent."""


async def get_embedding_column_dimension(session: AsyncSession) -> int:
    result = await session.execute(
        text(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'document_chunks'::regclass AND attname = 'embedding'"
        )
    )
    format_type = result.scalar_one()
    match = _VECTOR_TYPE_RE.match(format_type)
    if not match:
        raise EmbeddingSchemaMismatchError(
            f"Could not determine document_chunks.embedding column width "
            f"(got '{format_type}')."
        )
    return int(match.group(1))


async def assert_embedding_dimension_matches(session: AsyncSession, configured: int) -> None:
    column_dimension = await get_embedding_column_dimension(session)
    if column_dimension != configured:
        raise EmbeddingSchemaMismatchError(
            f"document_chunks.embedding is vector({column_dimension}) but "
            f"EMBEDDING_DIMENSIONS is configured as {configured}. These must "
            "match - either fix EMBEDDING_DIMENSIONS to the model actually "
            "loaded in LM Studio, or write a migration to resize the column "
            "(and re-embed existing chunks, since old vectors would no "
            "longer be comparable to new ones)."
        )
