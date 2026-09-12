import uuid

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import OrgContext, get_org_context, require_role
from app.core.database import get_db
from app.models.enums import OrgRole
from app.schemas.document import DocumentDownloadResponse, DocumentListResponse, DocumentPublic
from app.services.document_service import DocumentService
from app.storage.base import StorageProvider
from app.storage.dependency import get_storage_provider

router = APIRouter()


@router.post(
    "/{organization_id}/documents",
    response_model=DocumentPublic,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    file: UploadFile = File(...),
    ctx: OrgContext = Depends(require_role(OrgRole.MEMBER)),
    db: AsyncSession = Depends(get_db),
    storage: StorageProvider = Depends(get_storage_provider),
) -> DocumentPublic:
    service = DocumentService(db, storage)
    document = await service.upload(
        org_id=ctx.organization.id, uploaded_by=ctx.membership.user_id, file=file
    )
    return DocumentPublic.model_validate(document)


@router.get("/{organization_id}/documents", response_model=DocumentListResponse)
async def list_documents(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    ctx: OrgContext = Depends(get_org_context),
    db: AsyncSession = Depends(get_db),
    storage: StorageProvider = Depends(get_storage_provider),
) -> DocumentListResponse:
    service = DocumentService(db, storage)
    documents, total = await service.list_for_org(
        ctx.organization.id, page=page, page_size=page_size
    )
    return DocumentListResponse(
        items=[DocumentPublic.model_validate(d) for d in documents],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{organization_id}/documents/{document_id}", response_model=DocumentPublic)
async def get_document(
    document_id: uuid.UUID,
    ctx: OrgContext = Depends(get_org_context),
    db: AsyncSession = Depends(get_db),
    storage: StorageProvider = Depends(get_storage_provider),
) -> DocumentPublic:
    service = DocumentService(db, storage)
    document = await service.get_or_404(document_id, ctx.organization.id)
    return DocumentPublic.model_validate(document)


@router.get(
    "/{organization_id}/documents/{document_id}/download",
    response_model=DocumentDownloadResponse,
)
async def download_document(
    document_id: uuid.UUID,
    ctx: OrgContext = Depends(get_org_context),
    db: AsyncSession = Depends(get_db),
    storage: StorageProvider = Depends(get_storage_provider),
) -> DocumentDownloadResponse:
    service = DocumentService(db, storage)
    document = await service.get_or_404(document_id, ctx.organization.id)
    url, expires_in = await service.get_download_url(document)
    return DocumentDownloadResponse(url=url, expires_in=expires_in)


@router.delete(
    "/{organization_id}/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_document(
    document_id: uuid.UUID,
    ctx: OrgContext = Depends(require_role(OrgRole.ADMIN)),
    db: AsyncSession = Depends(get_db),
    storage: StorageProvider = Depends(get_storage_provider),
) -> None:
    service = DocumentService(db, storage)
    document = await service.get_or_404(document_id, ctx.organization.id)
    await service.delete(document)
