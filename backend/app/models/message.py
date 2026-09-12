import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, Integer, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import MessageRole
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.conversation import Conversation


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single turn in a conversation. `citations` is only ever populated
    for ASSISTANT messages, and only ever contains citations the backend
    itself validated against the chunks actually retrieved for that turn
    (see app/rag/citations.py) - never anything the LLM's raw output is
    trusted to supply unchecked.

    `sequence` (not `created_at`) is the true ordering key for conversation
    history. Postgres's `now()` is fixed for the lifetime of a transaction,
    not per-statement - two messages committed in quick succession *within
    the same transaction* (or, more subtly, under this test suite's
    SAVEPOINT-nested transactions - see tests/conftest.py) can get an
    identical `created_at`, which would silently break "give me the
    messages before this one" ordering. A DB-assigned identity column has
    no such ambiguity."""

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        Index("ix_messages_conversation_sequence", "conversation_id", "sequence"),
    )

    sequence: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), nullable=False, unique=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[MessageRole] = mapped_column(
        SAEnum(MessageRole, name="message_role", native_enum=True), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # Retrieval bookkeeping surfaced back in the API response and useful for
    # later evaluation - not sensitive, so safe to persist and display.
    chunks_considered: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunks_used: Mapped[int | None] = mapped_column(Integer, nullable=True)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
