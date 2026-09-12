"""phase 5 conversations messages and fts

Revision ID: 6f45bc893ebc
Revises: 284636820202
Create Date: 2026-09-12 15:00:17.190063
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = '6f45bc893ebc'
down_revision: Union[str, None] = '284636820202'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversations_org_created", "conversations", ["organization_id", "created_at"]
    )
    op.create_index(
        op.f("ix_conversations_organization_id"), "conversations", ["organization_id"]
    )

    op.create_table(
        "messages",
        # `sequence` is the true conversation-history ordering key, not
        # created_at - Postgres's now() is fixed for the lifetime of a
        # transaction, so two messages committed close together (or, more
        # subtly, under the test suite's SAVEPOINT-nested transactions) can
        # share an identical created_at. A DB-assigned identity column has
        # no such ambiguity. See app/models/message.py's docstring.
        sa.Column(
            "sequence",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column(
            "role", sa.Enum("USER", "ASSISTANT", name="message_role", native_enum=True),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", JSONB(), nullable=True),
        sa.Column("chunks_considered", sa.Integer(), nullable=True),
        sa.Column("chunks_used", sa.Integer(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sequence"),
    )
    op.create_index(
        "ix_messages_conversation_created", "messages", ["conversation_id", "created_at"]
    )
    op.create_index(
        "ix_messages_conversation_sequence", "messages", ["conversation_id", "sequence"]
    )

    # Full-text search: a generated tsvector column, not a query-time
    # to_tsvector() call - the latter can't be indexed with a plain GIN
    # index (it would need a functional index instead, and every ts_rank
    # query would recompute the tsvector from scratch). `to_tsvector` is
    # STABLE, not IMMUTABLE, so a generated column requires the explicit
    # two-argument form (a fixed 'english' regconfig) - the one-argument
    # form reads a session-configurable default and Postgres rejects it in
    # a generation expression as "not immutable".
    op.execute(
        "ALTER TABLE document_chunks "
        "ADD COLUMN content_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', content)) STORED"
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_content_tsv ON document_chunks "
        "USING gin (content_tsv)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_content_tsv")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS content_tsv")

    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.drop_table("messages")
    # See the Phase 2/3 migrations for why this is required: Alembic
    # autogenerate does not track the Postgres ENUM type's lifecycle
    # independently of the table that uses it.
    sa.Enum(name="message_role").drop(op.get_bind(), checkfirst=True)

    op.drop_index(op.f("ix_conversations_organization_id"), table_name="conversations")
    op.drop_index("ix_conversations_org_created", table_name="conversations")
    op.drop_table("conversations")
