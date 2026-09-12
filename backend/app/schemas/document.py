import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import DocumentStatus


class DocumentPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    mime_type: str
    size_bytes: int
    status: DocumentStatus
    uploaded_by: uuid.UUID | None
    created_at: datetime

    # --- Processing metadata (Phase 4) ---
    # Deliberately excludes: storage_key (internal path), embedding vectors,
    # and any raw exception text - failure_reason is always the safe,
    # user-facing string the pipeline/task chose to write, never a stack
    # trace (see app/ingestion/errors.py and
    # app/workers/tasks/document_processing.py's exception handling).
    processing_started_at: datetime | None
    processing_completed_at: datetime | None
    chunk_count: int
    embedding_model: str | None
    failure_reason: str | None
    retry_count: int


class DocumentListResponse(BaseModel):
    items: list[DocumentPublic]
    total: int
    page: int
    page_size: int


class DocumentDownloadResponse(BaseModel):
    url: str
    expires_in: int
