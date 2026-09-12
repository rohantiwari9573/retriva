import uuid
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.core.database import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.document import Document


class DocumentChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A chunk is never queried on its own authority - every read must join
    through `document` (see DocumentChunkRepository), so tenant isolation for
    chunks is inherited entirely from the owning document's organization_id.
    There is deliberately no organization_id column here: duplicating it
    would create a second source of truth that could drift from the
    document's actual org (e.g. if a document were ever transferred)."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        # The idempotency guarantee the Celery task relies on: re-running
        # process_document deletes existing chunks for the document before
        # re-inserting (see app/ingestion/pipeline.py), but this constraint
        # is the backstop against two concurrent workers both inserting for
        # the same document.
        UniqueConstraint("document_id", "chunk_index", name="uq_chunk_document_index"),
        Index("ix_document_chunks_document_id", "document_id"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section: Mapped[str | None] = mapped_column(String(500), nullable=True)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(settings.EMBEDDING_DIMENSIONS), nullable=False
    )

    document: Mapped["Document"] = relationship(back_populates="chunks")
