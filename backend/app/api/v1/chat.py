"""Chat/RAG and conversation routes.

Nested under /organizations/{organization_id}, matching every other
org-scoped resource in this codebase (documents, members) - not a flat
/api/v1/chat, so organization access is resolved and enforced by the same
get_org_context/require_role dependencies everywhere else, rather than a
one-off org_id read from the request body.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1.deps import OrgContext, get_org_context, require_role
from app.core.config import settings
from app.core.database import get_db, get_session_factory
from app.core.exceptions import AppError, NotFoundError
from app.core.logging import get_logger
from app.core.rate_limit import rate_limit
from app.models.enums import MessageRole, OrgRole
from app.rag.embedding.base import EmbeddingProvider
from app.rag.embedding.dependency import get_embedding_provider
from app.rag.llm.base import LLMProvider
from app.rag.llm.dependency import get_llm_provider
from app.rag.query_rewrite.dependency import get_query_rewriter
from app.rag.retrieval.hybrid import HybridRetriever
from app.rag.streaming_events import ErrorEvent, format_sse
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.message_repository import MessageRepository
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    CitationPublic,
    ConversationDetail,
    ConversationListResponse,
    ConversationPublic,
    MessagePublic,
    RetrievalDebugHit,
    RetrievalDebugRequest,
    RetrievalDebugResponse,
    RetrievalMeta,
)
from app.services.conversation_service import ConversationService
from app.services.rag_service import RAGService

logger = get_logger(__name__)

router = APIRouter()


@router.post(
    "/{organization_id}/chat",
    response_model=ChatResponse,
    dependencies=[Depends(rate_limit("chat", settings.RATE_LIMIT_CHAT_PER_MINUTE))],
)
async def chat(
    body: ChatRequest,
    ctx: OrgContext = Depends(get_org_context),  # VIEWER+ may ask, per RBAC design
    db: AsyncSession = Depends(get_db),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
    llm_provider: LLMProvider = Depends(get_llm_provider),
) -> ChatResponse:
    service = RAGService(db, embedding_provider, llm_provider)
    result = await service.ask(
        organization_id=ctx.organization.id,
        user_id=ctx.membership.user_id,
        conversation_id=body.conversation_id,
        question=body.message,
    )
    return ChatResponse(
        conversation_id=result.conversation_id,
        message_id=result.message_id,
        answer=result.answer,
        citations=[CitationPublic(**asdict(c)) for c in result.citations],
        retrieval=RetrievalMeta(
            chunks_considered=result.chunks_considered, chunks_used=result.chunks_used
        ),
    )


@router.post(
    "/{organization_id}/chat/stream",
    dependencies=[Depends(rate_limit("chat", settings.RATE_LIMIT_CHAT_PER_MINUTE))],
)
async def chat_stream(
    body: ChatRequest,
    ctx: OrgContext = Depends(get_org_context),  # VIEWER+ may ask, per RBAC design
    db: AsyncSession = Depends(get_db),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
    llm_provider: LLMProvider = Depends(get_llm_provider),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> StreamingResponse:
    """SSE counterpart to POST /chat - see docs/streaming.md for the event
    protocol. Kept as a separate endpoint rather than changing /chat's
    contract: existing clients of the non-streaming endpoint are
    unaffected, and the two have genuinely different response shapes
    (a single JSON body vs. an event stream).

    Auth/org membership/conversation ownership are all resolved BEFORE the
    stream starts (using this request's normal `db` session, torn down
    when this function returns) - once the 200 + text/event-stream headers
    are sent, an HTTP error response is no longer possible, so anything
    that should be a real 404/403 must be checked here, not inside the
    generator. The generator itself opens its own session via
    session_factory rather than depending on `db`: a yield-dependency like
    get_db is torn down as soon as this function returns, which happens as
    soon as the StreamingResponse object is constructed - long before the
    generator body (which runs during response streaming, after this
    function has already returned) does its DB writes. See
    get_session_factory()'s docstring for why this needs its own
    dependency rather than reading app.core.database.AsyncSessionLocal
    directly (test isolation).
    """
    if not settings.STREAMING_ENABLED:
        raise AppError(
            "Streaming responses are disabled on this deployment.",
            code="STREAMING_DISABLED",
            status_code=503,
        )

    organization_id = ctx.organization.id
    user_id = ctx.membership.user_id
    conversation_id = body.conversation_id
    question = body.message

    if conversation_id is not None:
        # Resolved here (not inside ask_stream()) purely so a cross-tenant
        # or nonexistent conversation id produces a normal 404 JSON
        # response instead of an in-stream error event.
        conversation = await ConversationRepository(db).get_by_id_in_org(
            conversation_id, organization_id
        )
        if conversation is None:
            raise NotFoundError("Conversation not found.", code="CONVERSATION_NOT_FOUND")

    async def event_stream() -> AsyncIterator[str]:
        async with session_factory() as stream_db:
            query_rewriter = get_query_rewriter(llm_provider)
            service = RAGService(stream_db, embedding_provider, llm_provider, query_rewriter)
            try:
                async for event in service.ask_stream(
                    organization_id=organization_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    question=question,
                ):
                    yield format_sse(event)
            except Exception as exc:
                # Anything that escapes ask_stream() here is a bug, not an
                # expected failure mode (those are already turned into
                # ErrorEvents inside ask_stream()) - logged with the real
                # exception, reported to the client as a generic error
                # rather than leaking internals.
                logger.error("chat_stream_unhandled_error", exc_info=exc)
                yield format_sse(
                    ErrorEvent(code="INTERNAL_ERROR", message="An unexpected error occurred.")
                )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/{organization_id}/conversations/{conversation_id}/regenerate",
    dependencies=[Depends(rate_limit("chat", settings.RATE_LIMIT_CHAT_PER_MINUTE))],
)
async def regenerate_chat_stream(
    conversation_id: uuid.UUID,
    ctx: OrgContext = Depends(get_org_context),  # VIEWER+ may ask, per RBAC design
    db: AsyncSession = Depends(get_db),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
    llm_provider: LLMProvider = Depends(get_llm_provider),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> StreamingResponse:
    """SSE regenerate: reruns retrieval + generation for the conversation's
    most recent user question and appends a NEW assistant message - it
    never duplicates the question or deletes the previous answer. Same
    session-lifecycle reasoning as chat_stream() above. See
    docs/streaming.md and RAGService.regenerate_stream()."""
    if not settings.STREAMING_ENABLED:
        raise AppError(
            "Streaming responses are disabled on this deployment.",
            code="STREAMING_DISABLED",
            status_code=503,
        )

    organization_id = ctx.organization.id

    conversation = await ConversationRepository(db).get_by_id_in_org(
        conversation_id, organization_id
    )
    if conversation is None:
        raise NotFoundError("Conversation not found.", code="CONVERSATION_NOT_FOUND")
    last_user_message = await MessageRepository(db).get_last_by_role(
        conversation_id, role=MessageRole.USER
    )
    if last_user_message is None:
        raise NotFoundError(
            "This conversation has no question to regenerate an answer for.",
            code="NO_MESSAGE_TO_REGENERATE",
        )

    async def event_stream() -> AsyncIterator[str]:
        async with session_factory() as stream_db:
            query_rewriter = get_query_rewriter(llm_provider)
            service = RAGService(stream_db, embedding_provider, llm_provider, query_rewriter)
            try:
                async for event in service.regenerate_stream(
                    organization_id=organization_id, conversation_id=conversation_id
                ):
                    yield format_sse(event)
            except Exception as exc:
                logger.error("chat_regenerate_unhandled_error", exc_info=exc)
                yield format_sse(
                    ErrorEvent(code="INTERNAL_ERROR", message="An unexpected error occurred.")
                )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{organization_id}/conversations", response_model=ConversationListResponse)
async def list_conversations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    ctx: OrgContext = Depends(get_org_context),
    db: AsyncSession = Depends(get_db),
) -> ConversationListResponse:
    service = ConversationService(db)
    conversations, total = await service.list_for_org(
        ctx.organization.id, page=page, page_size=page_size
    )
    return ConversationListResponse(
        items=[ConversationPublic.model_validate(c) for c in conversations],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{organization_id}/conversations/{conversation_id}", response_model=ConversationDetail
)
async def get_conversation(
    conversation_id: uuid.UUID,
    ctx: OrgContext = Depends(get_org_context),
    db: AsyncSession = Depends(get_db),
) -> ConversationDetail:
    service = ConversationService(db)
    conversation, messages = await service.get_with_messages(conversation_id, ctx.organization.id)
    return ConversationDetail(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=[MessagePublic.model_validate(m) for m in messages],
    )


@router.delete(
    "/{organization_id}/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_conversation(
    conversation_id: uuid.UUID,
    ctx: OrgContext = Depends(require_role(OrgRole.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> None:
    service = ConversationService(db)
    conversation = await service.get_or_404(conversation_id, ctx.organization.id)
    await service.delete(conversation)


@router.post(
    "/{organization_id}/retrieval/debug",
    response_model=RetrievalDebugResponse,
)
async def retrieval_debug(
    body: RetrievalDebugRequest,
    ctx: OrgContext = Depends(require_role(OrgRole.ADMIN)),
    db: AsyncSession = Depends(get_db),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
) -> RetrievalDebugResponse:
    """Admin-only. Exposes raw per-retriever scores and the final fused
    ranking for debugging/evaluation - never wired into the normal chat
    response, and never available below ADMIN."""
    retriever = HybridRetriever(db, embedding_provider)
    result = await retriever.retrieve(organization_id=ctx.organization.id, query=body.query)
    return RetrievalDebugResponse(
        query=body.query,
        candidates_considered=result.candidates_considered,
        best_vector_similarity=result.best_vector_similarity,
        results=[
            RetrievalDebugHit(
                chunk_id=c.chunk_id,
                document_id=c.document_id,
                document_name=c.document_name,
                vector_score=c.vector_score,
                keyword_score=c.keyword_score,
                fused_score=c.fused_score,
                page=c.page_number,
                section=c.section,
                excerpt=c.content[:280],
            )
            for c in result.chunks
        ],
    )
