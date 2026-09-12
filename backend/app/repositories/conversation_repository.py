import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation


class ConversationRepository:
    """Every read is scoped by organization_id - same tenant-isolation
    boundary as DocumentRepository. A conversation belonging to another org
    is treated as not existing (404), never surfaced as 403."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self, *, organization_id: uuid.UUID, created_by: uuid.UUID, title: str | None
    ) -> Conversation:
        conversation = Conversation(
            organization_id=organization_id, created_by=created_by, title=title
        )
        self.db.add(conversation)
        await self.db.flush()
        await self.db.refresh(conversation)
        return conversation

    async def get_by_id_in_org(
        self, conversation_id: uuid.UUID, organization_id: uuid.UUID
    ) -> Conversation | None:
        result = await self.db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.organization_id == organization_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_org(
        self, organization_id: uuid.UUID, *, page: int, page_size: int
    ) -> tuple[list[Conversation], int]:
        count_result = await self.db.execute(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.organization_id == organization_id)
        )
        total = count_result.scalar_one()

        result = await self.db.execute(
            select(Conversation)
            .where(Conversation.organization_id == organization_id)
            .order_by(Conversation.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(result.scalars()), total

    async def delete(self, conversation: Conversation) -> None:
        await self.db.delete(conversation)
