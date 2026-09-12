"""phase 4 document ingestion pipeline

Revision ID: 284636820202
Revises: 31c50429804f
Create Date: 2026-09-12 14:07:30.465030
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy

from app.core.config import settings

revision: str = '284636820202'
down_revision: Union[str, None] = '31c50429804f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Baked in at migration-write time from the settings value active right now.
# Changing EMBEDDING_DIMENSIONS later does NOT retroactively resize this
# column - that requires a new migration (see docs/document-ingestion.md,
# "Changing the embedding model"). The application checks the live column
# width against the configured value at startup and at the start of every
# processing run, and fails loudly rather than silently truncating/padding
# a mismatched vector.
_EMBEDDING_DIMENSIONS = settings.EMBEDDING_DIMENSIONS


def upgrade() -> None:
    # Previously only ever enabled manually via psql during local/CI setup -
    # never tracked in a migration. Doing it here means `alembic upgrade
    # head` alone is sufficient to provision a fresh environment.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.add_column(
        "documents",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("processing_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "documents", sa.Column("embedding_model", sa.String(length=200), nullable=True)
    )
    op.add_column(
        "documents", sa.Column("embedding_dimension", sa.Integer(), nullable=True)
    )
    op.add_column("documents", sa.Column("failure_reason", sa.Text(), nullable=True))
    op.add_column(
        "documents",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "document_chunks",
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(length=500), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "embedding", pgvector.sqlalchemy.Vector(_EMBEDDING_DIMENSIONS), nullable=False
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_chunk_document_index"),
    )
    op.create_index(
        "ix_document_chunks_document_id", "document_chunks", ["document_id"], unique=False
    )
    # IVFFlat approximate-nearest-neighbor index for cosine similarity search
    # (Phase 5's retrieval query). `lists` is a coarse default appropriate for
    # a small-to-moderate corpus; IVFFlat needs at least a few rows before
    # training is meaningful, which is fine since it degrades gracefully to a
    # full scan on an empty/near-empty table rather than failing.
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding_cosine ON document_chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )


def downgrade() -> None:
    op.drop_index("ix_document_chunks_embedding_cosine", table_name="document_chunks")
    op.drop_index("ix_document_chunks_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")

    op.drop_column("documents", "retry_count")
    op.drop_column("documents", "failure_reason")
    op.drop_column("documents", "embedding_dimension")
    op.drop_column("documents", "embedding_model")
    op.drop_column("documents", "chunk_count")
    op.drop_column("documents", "processing_completed_at")
    op.drop_column("documents", "processing_started_at")

    # Deliberately NOT dropping the vector extension: unlike the
    # document_status ENUM (which this migration doesn't touch), the
    # extension is not owned by this migration's tables alone conceptually,
    # and DROP EXTENSION would either fail (dependent objects, if downgrade
    # order changes) or cascade-drop things far outside the tables this
    # migration created. Test isolation runs `downgrade base` then
    # `upgrade head` every session (see tests/conftest.py); leaving the
    # extension installed makes that round-trip idempotent instead of
    # breaking on the second run.
