import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.message import Message
    from app.models.organization import Organization
    from app.models.user import User


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A chat thread, scoped to one organization. Tenant isolation mirrors
    Document: every read goes through a membership-checked
    ConversationRepository method that filters on organization_id, and a
    conversation belonging to another org returns 404, never 403 - same
    "don't reveal it exists" posture as everything else in this project."""

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_org_created", "organization_id", "created_at"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Derived from the first user message (truncated) - a display
    # convenience for the sidebar, not an LLM-generated summary (that would
    # be an extra LLM call this phase doesn't need).
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)

    organization: Mapped["Organization"] = relationship()
    creator: Mapped["User | None"] = relationship()
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Message.sequence",
    )
