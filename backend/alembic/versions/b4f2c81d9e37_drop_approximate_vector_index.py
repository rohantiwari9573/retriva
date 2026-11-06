"""drop the approximate vector index so vector search is exact

The IVFFlat index searched a fraction of the table and applied the tenant filter
afterwards, so queries silently returned too few rows, or none for small tenants.
Rebuilding it on real data doesn't fix that. See docs/retrieval.md for measurements.

No data is lost: an index holds only derived data. The downgrade recreates it, which
can take a while and use significant memory on a large table.

Revision ID: b4f2c81d9e37
Revises: 6f45bc893ebc
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b4f2c81d9e37"
down_revision: str | None = "6f45bc893ebc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_cosine")


def downgrade() -> None:
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding_cosine ON document_chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )
