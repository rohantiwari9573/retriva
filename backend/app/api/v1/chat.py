"""Chat/RAG and conversation routes.

Nested under /organizations/{organization_id}, matching every other
org-scoped resource in this codebase (documents, members) - not a flat
/api/v1/chat, so organization access is resolved and enforced by the same
get_org_context/require_role dependencies everywhere else, rather than a
one-off org_id read from the request body.
"""

import uuid
from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import OrgContext, get_org_context, require_role
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import rate_limit
from app.models.enums import OrgRole
from app.rag.embedding.base import EmbeddingProvider
from app.rag.embedding.dependency import get_embedding_provider
from app.rag.llm.base import LLMProvider
from app.rag.llm.dependency import get_llm_provider
from app.rag.retrieval.hybrid import HybridRetriever
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
