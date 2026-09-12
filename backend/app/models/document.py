import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import DocumentStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.document_chunk import DocumentChunk
    from app.models.organization import Organization
    from app.models.user import User


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """storage_key is derived from this row's own id (see DocumentService),
    never from the client-supplied filename - that's what keeps a filename
    like "../../etc/passwd.pdf" from being a path-traversal vector: it's
    stored as display metadata only and never touches the object key."""

    __tablename__ = "documents"
    __table_args__ = (
        # Prevents re-uploading byte-identical content into the same org -
        # doubles as the idempotency check the spec's background-job section
        # will rely on once ingestion is real (Phase 4).
        UniqueConstraint("organization_id", "content_hash", name="uq_document_org_content_hash"),
        Index("ix_documents_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1000), unique=True, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(150), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        SAEnum(DocumentStatus, name="document_status", native_enum=True),
        nullable=False,
        default=DocumentStatus.UPLOADING,
    )

    # --- Processing metadata (Phase 4) ---
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processing_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Safe-for-display summary only (e.g. "Could not reach embedding service") -
    # never a raw exception message or stack trace; see DocumentProcessingError
    # subclasses' messages in app/ingestion/errors.py, which are all written to
    # be end-user-safe by construction.
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    organization: Mapped["Organization"] = relationship()
    uploader: Mapped["User | None"] = relationship()
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
