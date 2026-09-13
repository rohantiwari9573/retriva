"""Integration tests for app.ingestion.pipeline.process_document_pipeline -
the real DB (via db_session/pipeline_session_factory), the in-memory storage
fake, and the deterministic embedding provider (no live LM Studio; see
tests/e2e/test_lmstudio_e2e.py for that)."""

import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.ingestion.errors import EmptyDocumentError, PermanentProcessingError
from app.ingestion.pipeline import process_document_pipeline
from app.ingestion.schema_check import EmbeddingSchemaMismatchError
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.enums import DocumentStatus
from app.models.organization import Organization
from app.models.user import User
from app.rag.embedding.testing import DeterministicTestEmbeddingProvider

TXT_CONTENT = (
    b"Paragraph one talks about onboarding.\n\n"
    b"Paragraph two talks about benefits.\n\n"
    b"Paragraph three talks about offboarding."
)


async def _make_org_and_document(
    db_session, storage, *, mime_type="text/plain", content=TXT_CONTENT
) -> Document:
    org = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    user = User(email=f"{uuid.uuid4().hex}@example.com", hashed_password="x")
    db_session.add_all([org, user])
    await db_session.flush()

    extension = {"text/plain": ".txt", "text/markdown": ".md"}[mime_type]
    document = Document(
        organization_id=org.id,
        uploaded_by=user.id,
        original_filename=f"doc{extension}",
        storage_key=f"organizations/{org.id}/documents/pending{extension}",
        mime_type=mime_type,
        size_bytes=len(content),
        content_hash=uuid.uuid4().hex,
        status=DocumentStatus.PROCESSING,
    )
    db_session.add(document)
    await db_session.flush()
    document.storage_key = f"organizations/{org.id}/documents/{document.id}{extension}"
    await storage.upload(document.storage_key, content, mime_type)
    await db_session.commit()
    return document


async def test_pipeline_success_marks_ready_and_creates_chunks(
    db_session, pipeline_session_factory, fake_storage, fake_embedding_provider
):
    document = await _make_org_and_document(db_session, fake_storage)

    await process_document_pipeline(
        str(document.id),
        session_factory=pipeline_session_factory,
        storage=fake_storage,
        embedding_provider=fake_embedding_provider,
    )

    await db_session.refresh(document)
    assert document.status == DocumentStatus.READY
    assert document.chunk_count > 0
    assert document.embedding_model == fake_embedding_provider.model
    assert document.embedding_dimension == fake_embedding_provider.dimensions
    assert document.processing_completed_at is not None

    chunks = (
        await db_session.execute(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document.id)
            .order_by(DocumentChunk.chunk_index)
        )
    ).scalars().all()
    assert len(chunks) == document.chunk_count
    assert all(len(c.embedding) == fake_embedding_provider.dimensions for c in chunks)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


async def test_pipeline_is_idempotent_on_rerun(
    db_session, pipeline_session_factory, fake_storage, fake_embedding_provider
):
    document = await _make_org_and_document(db_session, fake_storage)

    await process_document_pipeline(
        str(document.id),
        session_factory=pipeline_session_factory,
        storage=fake_storage,
        embedding_provider=fake_embedding_provider,
    )
    await db_session.refresh(document)
    first_chunk_count = document.chunk_count

    # Force back to PROCESSING to simulate a redelivered task and re-run.
    document.status = DocumentStatus.PROCESSING
    await db_session.commit()

    await process_document_pipeline(
        str(document.id),
        session_factory=pipeline_session_factory,
        storage=fake_storage,
        embedding_provider=fake_embedding_provider,
    )
    await db_session.refresh(document)

    chunks = (
        await db_session.execute(
            select(DocumentChunk).where(DocumentChunk.document_id == document.id)
        )
    ).scalars().all()
    assert len(chunks) == first_chunk_count  # not doubled
    assert document.status == DocumentStatus.READY


async def test_pipeline_no_ops_when_already_ready(
    db_session, pipeline_session_factory, fake_storage, fake_embedding_provider
):
    document = await _make_org_and_document(db_session, fake_storage)
    await process_document_pipeline(
        str(document.id),
        session_factory=pipeline_session_factory,
        storage=fake_storage,
        embedding_provider=fake_embedding_provider,
    )
    await db_session.refresh(document)
    completed_at = document.processing_completed_at

    # A second, concurrent delivery of the same task should be a no-op.
    await process_document_pipeline(
        str(document.id),
        session_factory=pipeline_session_factory,
        storage=fake_storage,
        embedding_provider=fake_embedding_provider,
    )
    await db_session.refresh(document)
    assert document.processing_completed_at == completed_at


async def test_empty_document_raises_empty_document_error(
    db_session, pipeline_session_factory, fake_storage, fake_embedding_provider
):
    document = await _make_org_and_document(db_session, fake_storage, content=b"   \n\n  ")

    with pytest.raises(EmptyDocumentError):
        await process_document_pipeline(
            str(document.id),
            session_factory=pipeline_session_factory,
            storage=fake_storage,
            embedding_provider=fake_embedding_provider,
        )


async def test_missing_storage_object_raises_permanent_error(
    db_session, pipeline_session_factory, fake_storage, fake_embedding_provider
):
    document = await _make_org_and_document(db_session, fake_storage)
    fake_storage.objects.pop(document.storage_key)

    with pytest.raises(PermanentProcessingError):
        await process_document_pipeline(
            str(document.id),
            session_factory=pipeline_session_factory,
            storage=fake_storage,
            embedding_provider=fake_embedding_provider,
        )


async def test_dimension_mismatch_raises_schema_error(
    db_session, pipeline_session_factory, fake_storage
):
    document = await _make_org_and_document(db_session, fake_storage)
    wrong_dimension_provider = DeterministicTestEmbeddingProvider(dimensions=999)

    with pytest.raises(EmbeddingSchemaMismatchError):
        await process_document_pipeline(
            str(document.id),
            session_factory=pipeline_session_factory,
            storage=fake_storage,
            embedding_provider=wrong_dimension_provider,
        )


async def test_exceeding_max_chunks_raises_permanent_error(
    db_session, pipeline_session_factory, fake_storage, fake_embedding_provider, monkeypatch
):
    monkeypatch.setattr(settings, "MAX_CHUNKS_PER_DOCUMENT", 1)
    monkeypatch.setattr(settings, "CHUNK_SIZE_TOKENS", 2)
    monkeypatch.setattr(settings, "CHUNK_OVERLAP_TOKENS", 0)
    document = await _make_org_and_document(db_session, fake_storage)

    with pytest.raises(PermanentProcessingError, match="chunk processing limit"):
        await process_document_pipeline(
            str(document.id),
            session_factory=pipeline_session_factory,
            storage=fake_storage,
            embedding_provider=fake_embedding_provider,
        )


async def test_exceeding_max_text_length_raises_permanent_error(
    db_session, pipeline_session_factory, fake_storage, fake_embedding_provider, monkeypatch
):
    monkeypatch.setattr(settings, "MAX_DOCUMENT_TEXT_LENGTH", 10)
    document = await _make_org_and_document(db_session, fake_storage)

    with pytest.raises(PermanentProcessingError, match="processing limit"):
        await process_document_pipeline(
            str(document.id),
            session_factory=pipeline_session_factory,
            storage=fake_storage,
            embedding_provider=fake_embedding_provider,
        )


async def test_markdown_document_preserves_section_metadata(
    db_session, pipeline_session_factory, fake_storage, fake_embedding_provider, monkeypatch
):
    # Force each paragraph into its own chunk so section metadata from both
    # headings survives - with the default chunk size this whole tiny
    # document would fit in a single chunk (correctly tagged with only the
    # *first* paragraph's section, per chunk_document's documented behavior).
    monkeypatch.setattr(settings, "CHUNK_SIZE_TOKENS", 3)
    monkeypatch.setattr(settings, "CHUNK_OVERLAP_TOKENS", 0)
    md_content = b"# Handbook\n\nIntro paragraph.\n\n## Leave Policy\n\nLeave policy details.\n"
    document = await _make_org_and_document(
        db_session, fake_storage, mime_type="text/markdown", content=md_content
    )

    await process_document_pipeline(
        str(document.id),
        session_factory=pipeline_session_factory,
        storage=fake_storage,
        embedding_provider=fake_embedding_provider,
    )

    chunks = (
        await db_session.execute(
            select(DocumentChunk).where(DocumentChunk.document_id == document.id)
        )
    ).scalars().all()
    sections = {c.section for c in chunks}
    assert "Handbook" in sections
    assert "Handbook > Leave Policy" in sections
