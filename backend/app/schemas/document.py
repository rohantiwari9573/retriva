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


class DocumentListResponse(BaseModel):
    items: list[DocumentPublic]
    total: int
    page: int
    page_size: int


class DocumentDownloadResponse(BaseModel):
    url: str
    expires_in: int
