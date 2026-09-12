"""Real end-to-end RAG test: real LM Studio embedding model + real LM
Studio chat model + a real seeded document + real hybrid retrieval + a real
generated answer with real citations. Skipped automatically if LM Studio
isn't reachable - see tests/e2e/test_lmstudio_e2e.py (Phase 4) for the same
pattern applied to embeddings alone.

Per the Phase 5 spec: do not claim this E2E was performed unless it
actually ran (not skipped) against a real, reachable LM Studio instance -
check the pytest output for "skipped" vs "passed" on this file specifically
before citing it as verification. Uses a known document with a known
answer (an Employee Handbook leave policy), exactly as the spec asks.
"""

import uuid

import httpx
import pytest

from app.core.config import settings
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.enums import DocumentStatus
from app.models.organization import Organization
from app.models.user import User
from app.rag.llm.lmstudio import LMStudioLLMProvider
from app.services.rag_service import RAGService

HANDBOOK_TEXT = (
    "Employee Handbook\n\n"
    "Section: Leave Policy\n"
    "All full-time employees are entitled to an annual leave allowance of "
    "24 days per calendar year, accrued monthly and carried over up to a "
    "maximum of 5 days into the following year."
)


def _lmstudio_reachable() -> bool:
    try:
        response = httpx.get(f"{settings.EMBEDDING_BASE_URL}/models", timeout=2.0)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    not _lmstudio_reachable(),
    reason=(
        f"LM Studio not reachable at {settings.EMBEDDING_BASE_URL} - start LM Studio's "
        "local server with an embedding model AND a chat model loaded to run this test."
    ),
)


async def test_real_rag_pipeline_answers_with_valid_citation(db_session):
    from app.rag.embedding.lmstudio import LMStudioEmbeddingProvider

    embedding_provider = LMStudioEmbeddingProvider()

    org = Organization(name="E2E Org", slug=f"e2e-{uuid.uuid4().hex[:8]}")
    user = User(email=f"{uuid.uuid4().hex}@example.com", hashed_password="x")
    db_session.add_all([org, user])
    await db_session.flush()

    document = Document(
        organization_id=org.id,
        original_filename="Employee Handbook.pdf",
        storage_key=f"key-{uuid.uuid4()}",
        mime_type="application/pdf",
        size_bytes=len(HANDBOOK_TEXT),
        content_hash=uuid.uuid4().hex,
        status=DocumentStatus.READY,
        uploaded_by=user.id,
    )
    db_session.add(document)
    await db_session.flush()

    vector = await embedding_provider.embed_query(HANDBOOK_TEXT)
    assert len(vector) == settings.EMBEDDING_DIMENSIONS  # real embedding model reachable

    chunk = DocumentChunk(
        document_id=document.id,
        chunk_index=0,
        content=HANDBOOK_TEXT,
        page_number=1,
        section="Leave Policy",
        char_count=len(HANDBOOK_TEXT),
        token_count=len(HANDBOOK_TEXT) // 4,
        content_hash=uuid.uuid4().hex,
        embedding=vector,
    )
    db_session.add(chunk)
    await db_session.commit()

    llm_provider = LMStudioLLMProvider()
    service = RAGService(db_session, embedding_provider, llm_provider)

    result = await service.ask(
        organization_id=org.id,
        user_id=user.id,
        conversation_id=None,
        question="What is the annual leave allowance?",
    )

    assert result.chunks_used >= 1  # real semantic retrieval found the chunk
    assert len(result.citations) >= 1  # real chat model produced a valid citation
    assert result.citations[0].document_name == "Employee Handbook.pdf"
    assert result.citations[0].section == "Leave Policy"
    # A real model answering this specific, unambiguous question should
    # mention the number from the source text somewhere in its answer.
    assert "24" in result.answer
