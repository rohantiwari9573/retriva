import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import MessageRole
from app.models.message import Message


class MessageRepository:
    """Always takes conversation_id, never a bare message_id lookup - a
    message has no organization_id of its own, so tenant isolation depends
    entirely on the caller already holding a conversation_id fetched via
    ConversationRepository.get_by_id_in_org (same pattern as DocumentChunk)."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        *,
        conversation_id: uuid.UUID,
        role: MessageRole,
        content: str,
        citations: list[dict[str, Any]] | None = None,
        chunks_considered: int | None = None,
        chunks_used: int | None = None,
    ) -> Message:
        message = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            citations=citations,
            chunks_considered=chunks_considered,
            chunks_used=chunks_used,
        )
        self.db.add(message)
        await self.db.flush()
        await self.db.refresh(message)
        return message

    async def list_for_conversation(self, conversation_id: uuid.UUID) -> list[Message]:
        result = await self.db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.sequence)
        )
        return list(result.scalars())

    async def get_last_by_role(
        self, conversation_id: uuid.UUID, *, role: MessageRole
    ) -> Message | None:
        """Most recent message of the given role in the conversation, by
        `sequence` (see list_recent_before's docstring for why not
        `created_at`) - used by RAGService.regenerate_stream() to find the
        question a regenerate request re-answers."""
        result = await self.db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.role == role)
            .order_by(Message.sequence.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_recent_before(
        self, conversation_id: uuid.UUID, *, before: Message, limit: int
    ) -> list[Message]:
        """Oldest-first slice of the `limit` messages immediately preceding
        `before` - used to build bounded conversation history for the
        prompt (CONVERSATION_HISTORY_MAX_MESSAGES), not the full thread.
        Ordered/filtered by `sequence`, not `created_at` - see Message's
        docstring for why timestamp ordering is unsafe here."""
        if limit <= 0:
            return []
        result = await self.db.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.sequence < before.sequence,
            )
            .order_by(Message.sequence.desc())
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))
