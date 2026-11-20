"""document processing lease: token, heartbeat and attempt count

Lets a worker claim a document, heartbeat while it works, and lets a recovery
sweep find documents whose worker died (API restart, OOM kill, worker crash), so
no document can stay in PROCESSING indefinitely. See app/ingestion/lease.py.

Additive only: two nullable columns, one NOT NULL column with a server default
(no table rewrite on PostgreSQL 11+), and a small partial index covering only
PROCESSING rows. Existing documents get processing_attempts = 0 and NULL lease
columns; a PROCESSING row with a NULL heartbeat is treated as stale by the sweep,
which is what recovers documents stranded before this migration.

Revision ID: c7a3e5d10b42
Revises: b4f2c81d9e37
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "c7a3e5d10b42"
down_revision: str | None = "b4f2c81d9e37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOCK_TIMEOUT = "5s"


def upgrade() -> None:
    # ALTER TABLE needs an ACCESS EXCLUSIVE lock; never queue indefinitely behind a
    # long ingestion transaction. On timeout the whole migration rolls back.
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column("documents", sa.Column("processing_token", UUID(as_uuid=True), nullable=True))
    op.add_column(
        "documents",
        sa.Column("processing_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("processing_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_documents_processing_heartbeat",
        "documents",
        ["processing_heartbeat_at"],
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )
    op.execute("SET LOCAL lock_timeout TO DEFAULT")


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_index("ix_documents_processing_heartbeat", table_name="documents")
    op.drop_column("documents", "processing_attempts")
    op.drop_column("documents", "processing_heartbeat_at")
    op.drop_column("documents", "processing_token")
    op.execute("SET LOCAL lock_timeout TO DEFAULT")
