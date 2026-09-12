"""RAGService: orchestrates the full retrieval-augmented answer pipeline.

Deliberately NOT a FastAPI route handler's job - this class has no
knowledge of HTTP, and every step it performs (validate access, retrieve,
fuse, build context, call the LLM, validate citations, persist) is a plain
method call, which is what makes it possible to test the whole pipeline
against a StubLLMProvider/DeterministicTestEmbeddingProvider without a real
LM Studio.

Latency is measured per stage (embedding happens inside HybridRetriever, so
"retrieval_latency_ms" below covers query-embedding + vector search +
keyword search + fusion + hydration together - see docs/rag.md for why
those aren't split further: they're one sequential critical path per
request, not independently parallelizable work worth separate metrics).
"""

import time
import uuid
from dataclasses import asdict, dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import EmbeddingUnavailableError, LLMUnavailableError, NotFoundError
from app.core.logging import get_logger
from app.models.conversation import Conversation
from app.models.enums import MessageRole
from app.models.message import Message
from app.rag.citations import Citation, validate_citations
from app.rag.context_builder import build_context
from app.rag.embedding.base import EmbeddingProvider, EmbeddingProviderUnavailableError
from app.rag.llm.base import (
    ChatMessage,
    LLMProvider,
    LLMProviderResponseError,
    LLMProviderUnavailableError,
)
from app.rag.prompts.templates import INSUFFICIENT_EVIDENCE_ANSWER, build_messages
from app.rag.retrieval.hybrid import HybridRetriever
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.message_repository import MessageRepository

logger = get_logger(__name__)

_TITLE_MAX_CHARS = 200


@dataclass(frozen=True)
class ChatResult:
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    answer: str
    citations: list[Citation]
    chunks_considered: int
    chunks_used: int


class RAGService:
    def __init__(
        self, db: AsyncSession, embedding_provider: EmbeddingProvider, llm_provider: LLMProvider
    ) -> None:
        self.db = db
        self.conversations = ConversationRepository(db)
        self.messages = MessageRepository(db)
        self.retriever = HybridRetriever(db, embedding_provider)
        self.llm = llm_provider

    async def ask(
        self,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        question: str,
    ) -> ChatResult:
        total_start = time.perf_counter()
        conversation = await self._resolve_conversation(
            organization_id=organization_id,
            user_id=user_id,
            conversation_id=conversation_id,
            question=question,
        )

        # Commit the user's message before any retrieval/LLM work - a local
        # LLM call can take 30-120s, and holding get_db's transaction open
        # that whole time would tie up a DB connection from the pool for
        # every concurrent chat (DB_POOL_SIZE=10 by default). Same
        # explicit-mid-handler-commit precedent as Phase 3/4's early-commit
        # failure paths.
        user_message = await self.messages.create(
            conversation_id=conversation.id, role=MessageRole.USER, content=question
        )
        await self.db.commit()

        history = await self._load_history(conversation.id, before=user_message)

        retrieval_start = time.perf_counter()
        try:
            retrieval = await self.retriever.retrieve(
                organization_id=organization_id, query=question
            )
        except EmbeddingProviderUnavailableError as exc:
            raise EmbeddingUnavailableError(str(exc)) from exc
        retrieval_latency_ms = (time.perf_counter() - retrieval_start) * 1000

        insufficient = not retrieval.chunks or (
            retrieval.best_vector_similarity is not None
            and retrieval.best_vector_similarity < settings.RETRIEVAL_MIN_SIMILARITY
        )

        llm_latency_ms = 0.0
        if insufficient:
            answer_text = INSUFFICIENT_EVIDENCE_ANSWER
            citations: list[Citation] = []
            chunks_used = 0
        else:
            built_context = build_context(retrieval.chunks)
            messages = build_messages(
                context_text=built_context.text,
                question=question,
                conversation_history=history,
            )
            llm_start = time.perf_counter()
            try:
                raw_answer = await self.llm.generate(messages)
            except LLMProviderUnavailableError as exc:
                raise LLMUnavailableError(str(exc)) from exc
            except LLMProviderResponseError as exc:
                raise LLMUnavailableError(str(exc)) from exc
            llm_latency_ms = (time.perf_counter() - llm_start) * 1000

            validated = validate_citations(raw_answer, built_context)
            if not validated.citations:
                # The model answered but cited nothing we can verify -
                # trusting an uncited factual claim is exactly what the
                # citation mechanism exists to prevent, so this is treated
                # the same as insufficient evidence rather than returned as-is.
                answer_text = INSUFFICIENT_EVIDENCE_ANSWER
                citations = []
                chunks_used = 0
            else:
                answer_text = validated.answer
                citations = validated.citations
                chunks_used = len(built_context.blocks)

        assistant_message = await self.messages.create(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content=answer_text,
            citations=[asdict(c) for c in citations] or None,
            chunks_considered=retrieval.candidates_considered,
            chunks_used=chunks_used,
        )
        await self.db.commit()

        total_latency_ms = (time.perf_counter() - total_start) * 1000
        logger.info(
            "chat_completed",
            organization_id=str(organization_id),
            conversation_id=str(conversation.id),
            chunks_considered=retrieval.candidates_considered,
            chunks_used=chunks_used,
            citations_count=len(citations),
            retrieval_latency_ms=round(retrieval_latency_ms, 1),
            llm_latency_ms=round(llm_latency_ms, 1),
            total_latency_ms=round(total_latency_ms, 1),
        )

        return ChatResult(
            conversation_id=conversation.id,
            message_id=assistant_message.id,
            answer=answer_text,
            citations=citations,
            chunks_considered=retrieval.candidates_considered,
            chunks_used=chunks_used,
        )

    async def _resolve_conversation(
        self,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        question: str,
    ) -> Conversation:
        if conversation_id is None:
            title = question.strip()[:_TITLE_MAX_CHARS] or None
            return await self.conversations.create(
                organization_id=organization_id, created_by=user_id, title=title
            )
        conversation = await self.conversations.get_by_id_in_org(conversation_id, organization_id)
        if conversation is None:
            raise NotFoundError("Conversation not found.", code="CONVERSATION_NOT_FOUND")
        return conversation

    async def _load_history(
        self, conversation_id: uuid.UUID, *, before: Message
    ) -> list[ChatMessage]:
        rows = await self.messages.list_recent_before(
            conversation_id, before=before, limit=settings.CONVERSATION_HISTORY_MAX_MESSAGES
        )
        return [
            ChatMessage(
                role="user" if row.role == MessageRole.USER else "assistant", content=row.content
            )
            for row in rows
        ]
