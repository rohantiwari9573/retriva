import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import MessageRole


class ChatRequest(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str = Field(..., min_length=1, max_length=4000)


class CitationPublic(BaseModel):
    id: str
    document_id: uuid.UUID
    document_name: str
    chunk_id: uuid.UUID
    page: int | None
    section: str | None
    excerpt: str


class RetrievalMeta(BaseModel):
    chunks_considered: int
    chunks_used: int


class ChatResponse(BaseModel):
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    answer: str
    citations: list[CitationPublic]
    retrieval: RetrievalMeta


class MessagePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: MessageRole
    content: str
    citations: list[CitationPublic] | None
    chunks_considered: int | None
    chunks_used: int | None
    created_at: datetime


class ConversationPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationPublic):
    messages: list[MessagePublic]


class ConversationListResponse(BaseModel):
    items: list[ConversationPublic]
    total: int
    page: int
    page_size: int


class RetrievalDebugRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)


class RetrievalDebugHit(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    vector_score: float | None
    keyword_score: float | None
    fused_score: float
    page: int | None
    section: str | None
    excerpt: str


class RetrievalDebugResponse(BaseModel):
    query: str
    candidates_considered: int
    best_vector_similarity: float | None
    results: list[RetrievalDebugHit]
