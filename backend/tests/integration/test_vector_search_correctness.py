"""Vector search must return what exact nearest-neighbour search returns.

An approximate index on document_chunks silently returned too few rows: with 683 chunks
in one tenant, 7 of 20. See docs/retrieval.md. These run the real retrievers against real
Postgres, with ground truth computed here in Python, independently of the database.
"""

import math
import random
import uuid

import pytest
from sqlalchemy import text

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.enums import DocumentStatus
from app.models.organization import Organization
from app.rag.embedding.testing import DeterministicTestEmbeddingProvider
from app.rag.retrieval.hybrid import HybridRetriever
from app.rag.retrieval.vector import VectorRetriever

DIMENSIONS = 768
EMBED = DeterministicTestEmbeddingProvider(dimensions=DIMENSIONS)


def _vector(rng: random.Random) -> list[float]:
    return [rng.gauss(0, 1) for _ in range(DIMENSIONS)]


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return 1 - dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


async def _make_tenant(db_session, size: int, seed: int) -> tuple[uuid.UUID, dict]:
    """A tenant with `size` chunks. Returns its id and {chunk_id: vector}."""
    rng = random.Random(seed)
    org = Organization(name=f"Tenant {seed}", slug=f"tenant-{uuid.uuid4().hex[:10]}")
    db_session.add(org)
    await db_session.flush()
    document = Document(
        organization_id=org.id,
        original_filename="corpus.txt",
        storage_key=f"key-{uuid.uuid4()}",
        mime_type="text/plain",
        size_bytes=size,
        content_hash=uuid.uuid4().hex,
        status=DocumentStatus.READY,
    )
    db_session.add(document)
    await db_session.flush()
    chunks = {}
    for index in range(size):
        vector = _vector(rng)
        chunk_id = uuid.uuid4()
        chunks[chunk_id] = vector
        db_session.add(
            DocumentChunk(
                id=chunk_id,
                document_id=document.id,
                chunk_index=index,
                content=f"chunk number {index} of tenant {seed}",
                char_count=20,
                token_count=5,
                content_hash=uuid.uuid4().hex,
                embedding=vector,
            )
        )
    await db_session.flush()
    return org.id, chunks


async def _refresh_statistics(db_session) -> None:
    """The planner chooses its plan from these, so tests must see realistic stats."""
    await db_session.execute(text("ANALYZE document_chunks"))
    await db_session.execute(text("ANALYZE documents"))


def _exact_top(chunks: dict, query: list[float], k: int) -> list[uuid.UUID]:
    return sorted(chunks, key=lambda chunk_id: _cosine_distance(chunks[chunk_id], query))[:k]


async def _assert_matches_exact(db_session, org_id, chunks, *, k: int, seed: int) -> None:
    rng = random.Random(seed)
    retriever = VectorRetriever(db_session)
    for _ in range(5):
        query = _vector(rng)
        hits = await retriever.search_by_embedding(
            organization_id=org_id, query_embedding=query, limit=k
        )
        assert [hit.chunk_id for hit in hits] == _exact_top(chunks, query, k)


async def test_no_approximate_index_filters_tenants_after_the_scan(db_session) -> None:
    """An ANN index on this column loses a tenant's rows, however well it's built.
    Adding one back needs a retrieval strategy that handles tenant filtering."""
    rows = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'document_chunks' "
            "AND (indexdef ILIKE '%ivfflat%' OR indexdef ILIKE '%hnsw%')"
        )
    )
    assert rows.all() == []


@pytest.mark.parametrize("k", [20, 30])
async def test_reported_defect_683_chunks_one_tenant(db_session, k: int) -> None:
    """The reproduced case: this returned 7 of 20 rows with no error."""
    org_id, chunks = await _make_tenant(db_session, 683, seed=1)
    await _refresh_statistics(db_session)

    await _assert_matches_exact(db_session, org_id, chunks, k=k, seed=2)


async def test_small_tenants_keep_their_results_next_to_a_large_one(db_session) -> None:
    await _make_tenant(db_session, 1500, seed=3)
    small_id, small = await _make_tenant(db_session, 40, seed=4)
    tiny_id, tiny = await _make_tenant(db_session, 5, seed=5)
    await _refresh_statistics(db_session)

    await _assert_matches_exact(db_session, small_id, small, k=20, seed=6)
    await _assert_matches_exact(db_session, tiny_id, tiny, k=5, seed=7)


async def test_results_never_cross_tenants(db_session) -> None:
    first_id, first = await _make_tenant(db_session, 300, seed=8)
    second_id, second = await _make_tenant(db_session, 300, seed=9)
    await _refresh_statistics(db_session)
    retriever = VectorRetriever(db_session)
    # A vector stored by the other tenant must not pull that tenant's chunk in.
    query = next(iter(second.values()))

    hits = await retriever.search_by_embedding(
        organization_id=first_id, query_embedding=query, limit=30
    )

    assert len(hits) == 30
    assert {hit.chunk_id for hit in hits} <= set(first)
    assert second_id != first_id


async def test_results_stay_correct_after_more_chunks_arrive(db_session) -> None:
    org_id, chunks = await _make_tenant(db_session, 200, seed=10)
    await _refresh_statistics(db_session)
    await _assert_matches_exact(db_session, org_id, chunks, k=20, seed=11)

    rng = random.Random(12)
    document_id = (
        await db_session.execute(
            text("SELECT id FROM documents WHERE organization_id = :org"), {"org": org_id}
        )
    ).scalar_one()
    for index in range(200, 500):
        vector = _vector(rng)
        chunk_id = uuid.uuid4()
        chunks[chunk_id] = vector
        db_session.add(
            DocumentChunk(
                id=chunk_id,
                document_id=document_id,
                chunk_index=index,
                content=f"added chunk {index}",
                char_count=11,
                token_count=3,
                content_hash=uuid.uuid4().hex,
                embedding=vector,
            )
        )
    await db_session.flush()

    await _assert_matches_exact(db_session, org_id, chunks, k=20, seed=13)
    await _refresh_statistics(db_session)
    await _assert_matches_exact(db_session, org_id, chunks, k=20, seed=14)


async def test_results_stay_correct_after_reindexing(db_session) -> None:
    org_id, chunks = await _make_tenant(db_session, 300, seed=15)
    await db_session.execute(text("REINDEX TABLE document_chunks"))
    await _refresh_statistics(db_session)

    await _assert_matches_exact(db_session, org_id, chunks, k=20, seed=16)


async def test_a_tenant_with_fewer_chunks_than_requested_gets_all_of_them(db_session) -> None:
    org_id, chunks = await _make_tenant(db_session, 3, seed=17)
    await _refresh_statistics(db_session)

    hits = await VectorRetriever(db_session).search_by_embedding(
        organization_id=org_id, query_embedding=_vector(random.Random(18)), limit=20
    )

    assert {hit.chunk_id for hit in hits} == set(chunks)


async def test_chunks_of_unfinished_documents_are_still_excluded(db_session) -> None:
    org_id, chunks = await _make_tenant(db_session, 30, seed=19)
    await db_session.execute(
        text("UPDATE documents SET status = 'PROCESSING' WHERE organization_id = :org"),
        {"org": org_id},
    )

    hits = await VectorRetriever(db_session).search_by_embedding(
        organization_id=org_id, query_embedding=next(iter(chunks.values())), limit=20
    )

    assert hits == []


# --- the real hybrid pipeline ------------------------------------------------


async def _seed_retrievable_text(db_session, org_id: uuid.UUID, content: str) -> uuid.UUID:
    document_id = (
        await db_session.execute(
            text("SELECT id FROM documents WHERE organization_id = :org"), {"org": org_id}
        )
    ).scalar_one()
    chunk_id = uuid.uuid4()
    db_session.add(
        DocumentChunk(
            id=chunk_id,
            document_id=document_id,
            chunk_index=10_000,
            content=content,
            char_count=len(content),
            token_count=8,
            content_hash=uuid.uuid4().hex,
            embedding=await EMBED.embed_query(content),
        )
    )
    await db_session.flush()
    return chunk_id


async def test_hybrid_returns_a_full_result_set_when_keywords_match_nothing(db_session) -> None:
    """Keyword search finds nothing here, so every result depends on vector search:
    this is where lost vector rows became thin, or empty, context for the LLM."""
    org_id, _ = await _make_tenant(db_session, 683, seed=20)
    await _refresh_statistics(db_session)

    result = await HybridRetriever(db_session, EMBED).retrieve(
        organization_id=org_id, query="xyzzy plugh", candidate_pool=30, top_k=8
    )

    assert len(result.chunks) == 8
    assert result.candidates_considered == 30


async def test_hybrid_finds_a_chunk_through_vector_search_alone(db_session) -> None:
    org_id, _ = await _make_tenant(db_session, 683, seed=21)
    content = "Employees receive twenty four days of annual leave"
    target = await _seed_retrievable_text(db_session, org_id, content)
    await _refresh_statistics(db_session)

    # The query's words aren't all in the chunk, so full-text search can't match it,
    # but its embedding is the chunk's own.
    hits = await VectorRetriever(db_session).search_by_embedding(
        organization_id=org_id,
        query_embedding=await EMBED.embed_query(content),
        limit=30,
    )

    assert hits[0].chunk_id == target
    assert hits[0].score == pytest.approx(1.0)


async def test_chat_cites_the_right_source_within_a_large_corpus(client, db_session) -> None:
    await client.post(
        "/api/v1/auth/register",
        json={"email": f"c-{uuid.uuid4().hex[:8]}@example.com", "password": "correct-horse-99"},
    )
    organization = (await client.post("/api/v1/organizations", json={"name": "Citing"})).json()
    org_id = uuid.UUID(organization["id"])

    document = Document(
        organization_id=org_id,
        original_filename="handbook.txt",
        storage_key=f"key-{uuid.uuid4()}",
        mime_type="text/plain",
        size_bytes=100,
        content_hash=uuid.uuid4().hex,
        status=DocumentStatus.READY,
    )
    db_session.add(document)
    await db_session.flush()
    rng = random.Random(22)
    for index in range(300):
        db_session.add(
            DocumentChunk(
                document_id=document.id,
                chunk_index=index,
                content=f"filler passage {index}",
                char_count=15,
                token_count=3,
                content_hash=uuid.uuid4().hex,
                embedding=_vector(rng),
            )
        )
    answer_text = "Employees receive 24 days of annual leave per year."
    db_session.add(
        DocumentChunk(
            document_id=document.id,
            chunk_index=300,
            content=answer_text,
            page_number=14,
            section="Leave Policy",
            char_count=len(answer_text),
            token_count=12,
            content_hash=uuid.uuid4().hex,
            embedding=await EMBED.embed_query(answer_text),
        )
    )
    await db_session.commit()

    response = await client.post(
        f"/api/v1/organizations/{org_id}/chat", json={"message": answer_text}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["citations"], body
    assert {citation["document_name"] for citation in body["citations"]} == {"handbook.txt"}
    assert any("24 days" in citation["excerpt"] for citation in body["citations"])
