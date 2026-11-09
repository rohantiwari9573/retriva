"""drop the approximate vector index so vector search is exact

The IVFFlat index searched a fraction of the table and applied the tenant filter
afterwards, so queries silently returned too few rows, or none for small tenants.
Rebuilding it on real data doesn't fix that. See docs/retrieval.md for measurements.

No data is lost: an index holds only derived data. The downgrade recreates it, which
can take a while and use significant memory on a large table.

Both directions wait at most LOCK_TIMEOUT for their table lock. DROP INDEX needs an
ACCESS EXCLUSIVE lock, so without a limit it would queue behind any open transaction
on document_chunks (ingestion holds long ones), and every search would queue behind
it. On timeout Postgres raises "canceling statement due to lock timeout" (SQLSTATE
55P03), the migration's transaction rolls back, and the schema and alembic_version
stay as they were, so it is safe to retry. See docs/vector-index-migration-runbook.md.

Revision ID: b4f2c81d9e37
Revises: 6f45bc893ebc
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b4f2c81d9e37"
down_revision: str | None = "6f45bc893ebc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOCK_TIMEOUT = "5s"


def _with_lock_timeout(statement: str) -> None:
    # SET LOCAL lasts only until the end of the transaction Alembic runs migrations
    # in, so no session or database setting changes. Resetting it afterwards keeps
    # the limit off any later migration applied in the same transaction.
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(statement)
    op.execute("SET LOCAL lock_timeout TO DEFAULT")


def upgrade() -> None:
    _with_lock_timeout("DROP INDEX IF EXISTS ix_document_chunks_embedding_cosine")


def downgrade() -> None:
    _with_lock_timeout(
        "CREATE INDEX ix_document_chunks_embedding_cosine ON document_chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )
