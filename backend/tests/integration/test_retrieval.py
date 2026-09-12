"""Integration tests for VectorRetriever, KeywordRetriever, and
HybridRetriever against a real Postgres/pgvector database (via db_session) -
no live LM Studio, embeddings come from DeterministicTestEmbeddingProvider.

DeterministicTestEmbeddingProvider is content-keyed: embedding the exact
text of a stored chunk reproduces that chunk's own vector (cosine distance
0), which is what makes a genuine "the expected chunk ranks first" assertion
possible without a real embedding model - see its docstring. This does NOT
let us test semantic similarity (that two different-but-related sentences
embed close together), only exact-content retrieval and, more importantly,
tenant-isolation and status-filtering behavior in the actual SQL.
"""

import uuid

import pytest

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.enums import DocumentStatus
from app.models.organization import Organization
from app.rag.embedding.testing import DeterministicTestEmbeddingProvider
from app.rag.retrieval.hybrid import HybridRetriever
from app.rag.retrieval.keyword import KeywordRetriever
from app.rag.retrieval.vector import VectorRetriever

EMBED = DeterministicTestEmbeddingProvider(dimensions=768)


async def _make_org(db_session) -> Organization:
    org = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    db_session.add(org)
    await db_session.flush()
    return org


async def _make_document(db_session, org: Organization, *, status=DocumentStatus.READY) -> Document:
    document = Document(
        organization_id=org.id,
        original_filename="handbook.txt",
        storage_key=f"key-{uuid.uuid4()}",
        mime_type="text/plain",
        size_bytes=100,
        content_hash=uuid.uuid4().hex,
        status=status,
    )
    db_session.add(document)
    await db_session.flush()
    return document


async def _make_chunk(
    db_session, document: Document, *, content: str, index: int, page: int | None = None,
    section: str | None = None,
) -> DocumentChunk:
    vector = await EMBED.embed_query(content)
    chunk = DocumentChunk(
        document_id=document.id,
        chunk_index=index,
        content=content,
        page_number=page,
        section=section,
        char_count=len(content),
        token_count=max(1, len(content) // 4),
        content_hash=uuid.uuid4().hex,
        embedding=vector,
    )
    db_session.add(chunk)
    await db_session.flush()
    return chunk


@pytest.fixture
async def seeded_org(db_session):
    org = await _make_org(db_session)
    document = await _make_document(db_session, org)
    chunk_a = await _make_chunk(
        db_session, document, content="Employees receive 24 days of annual leave per year.",
        index=0, page=14, section="Leave Policy",
    )
    chunk_b = await _make_chunk(
        db_session, document, content="Offboarding requires returning all company equipment.",
        index=1, page=20, section="Offboarding",
    )
    await db_session.commit()
    return org, document, chunk_a, chunk_b


async def test_vector_search_ranks_exact_content_match_first(db_session, seeded_org):
    org, document, chunk_a, chunk_b = seeded_org
    retriever = VectorRetriever(db_session)

    query_embedding = await EMBED.embed_query(chunk_a.content)
    hits = await retriever.search_by_embedding(
        organization_id=org.id, query_embedding=query_embedding, limit=10
    )

    assert hits[0].chunk_id == chunk_a.id
    assert hits[0].score == pytest.approx(1.0, abs=1e-6)


async def test_vector_search_excludes_other_organizations(db_session, seeded_org):
    org, document, chunk_a, chunk_b = seeded_org
    other_org = await _make_org(db_session)
    await db_session.commit()

    retriever = VectorRetriever(db_session)
    query_embedding = await EMBED.embed_query(chunk_a.content)
    hits = await retriever.search_by_embedding(
        organization_id=other_org.id, query_embedding=query_embedding, limit=10
    )

    assert hits == []


async def test_vector_search_excludes_non_ready_documents(db_session, seeded_org):
    org, document, chunk_a, chunk_b = seeded_org
    processing_doc = await _make_document(db_session, org, status=DocumentStatus.PROCESSING)
    stuck_chunk = await _make_chunk(
        db_session, processing_doc, content="This chunk belongs to a still-processing document.",
        index=0,
    )
    await db_session.commit()

    retriever = VectorRetriever(db_session)
    query_embedding = await EMBED.embed_query(stuck_chunk.content)
    hits = await retriever.search_by_embedding(
        organization_id=org.id, query_embedding=query_embedding, limit=10
    )

    assert stuck_chunk.id not in [h.chunk_id for h in hits]


async def test_keyword_search_finds_exact_phrase(db_session, seeded_org):
    org, document, chunk_a, chunk_b = seeded_org
    retriever = KeywordRetriever(db_session)

    hits = await retriever.search(organization_id=org.id, query="annual leave", limit=10)

    assert len(hits) >= 1
    assert hits[0].chunk_id == chunk_a.id


async def test_keyword_search_excludes_other_organizations(db_session, seeded_org):
    org, document, chunk_a, chunk_b = seeded_org
    other_org = await _make_org(db_session)
    await db_session.commit()

    retriever = KeywordRetriever(db_session)
    hits = await retriever.search(organization_id=other_org.id, query="annual leave", limit=10)

    assert hits == []


async def test_keyword_search_no_match_returns_empty_list(db_session, seeded_org):
    org, document, chunk_a, chunk_b = seeded_org
    retriever = KeywordRetriever(db_session)

    hits = await retriever.search(
        organization_id=org.id, query="quantum flux capacitor nonsense", limit=10
    )

    assert hits == []


async def test_keyword_search_handles_empty_query_gracefully(db_session, seeded_org):
    org, document, chunk_a, chunk_b = seeded_org
    retriever = KeywordRetriever(db_session)

    hits = await retriever.search(organization_id=org.id, query="   ", limit=10)

    assert hits == []


async def test_hybrid_retrieve_hydrates_metadata_correctly(db_session, seeded_org):
    org, document, chunk_a, chunk_b = seeded_org
    retriever = HybridRetriever(db_session, EMBED)

    result = await retriever.retrieve(organization_id=org.id, query="annual leave")

    assert result.chunks
    top = result.chunks[0]
    assert top.document_name == "handbook.txt"
    assert top.page_number == 14
    assert top.section == "Leave Policy"
    assert result.best_vector_similarity is not None


async def test_hybrid_retrieve_is_organization_scoped(db_session, seeded_org):
    org, document, chunk_a, chunk_b = seeded_org
    other_org = await _make_org(db_session)
    await db_session.commit()

    retriever = HybridRetriever(db_session, EMBED)
    result = await retriever.retrieve(organization_id=other_org.id, query="annual leave")

    assert result.chunks == []
