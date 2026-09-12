import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.conversation import Conversation
from app.models.message import Message
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.message_repository import MessageRepository


class ConversationService:
    """Plain CRUD/listing around Conversation - RAGService owns the actual
    chat pipeline and message creation; this class exists so the sidebar/
    history routes don't need to import RAGService (and its LLM/embedding
    dependencies) just to list or delete conversations."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.conversations = ConversationRepository(db)
        self.messages = MessageRepository(db)

    async def list_for_org(
        self, organization_id: uuid.UUID, *, page: int, page_size: int
    ) -> tuple[list[Conversation], int]:
        return await self.conversations.list_for_org(
            organization_id, page=page, page_size=page_size
        )

    async def get_or_404(
        self, conversation_id: uuid.UUID, organization_id: uuid.UUID
    ) -> Conversation:
        conversation = await self.conversations.get_by_id_in_org(
            conversation_id, organization_id
        )
        if conversation is None:
            raise NotFoundError("Conversation not found.", code="CONVERSATION_NOT_FOUND")
        return conversation

    async def get_with_messages(
        self, conversation_id: uuid.UUID, organization_id: uuid.UUID
    ) -> tuple[Conversation, list[Message]]:
        conversation = await self.get_or_404(conversation_id, organization_id)
        messages = await self.messages.list_for_conversation(conversation.id)
        return conversation, messages

    async def delete(self, conversation: Conversation) -> None:
        await self.conversations.delete(conversation)
