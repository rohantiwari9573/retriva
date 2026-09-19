"""Integration tests for RAGService - the orchestration class, not the API
route. Uses DeterministicTestEmbeddingProvider (real DB retrieval, no live
LM Studio) and StubLLMProvider (deterministic citation-tag echo, no real
language understanding - see app/rag/llm/testing.py)."""

import uuid

import pytest
from sqlalchemy import select

from app.core.exceptions import EmbeddingUnavailableError, LLMUnavailableError, NotFoundError
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.enums import DocumentStatus, MessageRole
from app.models.message import Message
from app.models.organization import Organization
from app.models.user import User
from app.rag.embedding.base import EmbeddingProviderUnavailableError
from app.rag.embedding.testing import DeterministicTestEmbeddingProvider
from app.rag.llm.base import ChatMessage
from app.rag.llm.testing import StubLLMProvider, UnavailableLLMProvider
from app.services.rag_service import RAGService

EMBED = DeterministicTestEmbeddingProvider(dimensions=768)

# DeterministicTestEmbeddingProvider is content-keyed (see its docstring),
# not semantically meaningful - a paraphrased question embeds essentially
# independently of the chunk it's "about" and would never pass
# RETRIEVAL_MIN_SIMILARITY. Tests that need retrieval to actually succeed
# use this exact string as both the seeded chunk's content and the
# question, mirroring tests/integration/test_retrieval.py's approach.
CHUNK_CONTENT = "Employees receive 24 days of annual leave per year."


class _AlwaysFailsEmbeddingProvider:
    model = "broken"
    dimensions = 768

    async def embed_query(self, text: str) -> list[float]:
        raise EmbeddingProviderUnavailableError("Stub: embedding backend unavailable.")

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingProviderUnavailableError("Stub: embedding backend unavailable.")


async def _seed_org_with_document(db_session) -> tuple[Organization, DocumentChunk]:
    org = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    user = User(email=f"{uuid.uuid4().hex}@example.com", hashed_password="x")
    db_session.add_all([org, user])
    await db_session.flush()

    document = Document(
        organization_id=org.id,
        original_filename="handbook.txt",
        storage_key=f"key-{uuid.uuid4()}",
        mime_type="text/plain",
        size_bytes=100,
        content_hash=uuid.uuid4().hex,
        status=DocumentStatus.READY,
        uploaded_by=user.id,
    )
    db_session.add(document)
    await db_session.flush()

    content = CHUNK_CONTENT
    vector = await EMBED.embed_query(content)
    chunk = DocumentChunk(
        document_id=document.id,
        chunk_index=0,
        content=content,
        page_number=14,
        section="Leave Policy",
        char_count=len(content),
        token_count=12,
        content_hash=uuid.uuid4().hex,
        embedding=vector,
    )
    db_session.add(chunk)
    await db_session.commit()
    return org, chunk, user.id


async def test_ask_with_good_evidence_returns_grounded_answer_with_citation(db_session):
    org, chunk, user_id = await _seed_org_with_document(db_session)
    service = RAGService(db_session, EMBED, StubLLMProvider())

    result = await service.ask(
        organization_id=org.id,
        user_id=user_id,
        conversation_id=None,
        question=CHUNK_CONTENT,
    )

    assert len(result.citations) == 1
    assert result.citations[0].document_name == "handbook.txt"
    assert result.chunks_used == 1
    assert "[SOURCE-1]" in result.answer

    messages = (
        await db_session.execute(
            select(Message)
            .where(Message.conversation_id == result.conversation_id)
            .order_by(Message.created_at)
        )
    ).scalars().all()
    assert [m.role for m in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert messages[0].content == CHUNK_CONTENT
    assert messages[1].citations is not None


async def test_ask_with_no_documents_returns_insufficient_evidence_without_calling_llm(
    db_session,
):
    org = Organization(name="Empty Org", slug=f"empty-{uuid.uuid4().hex[:8]}")
    user = User(email=f"{uuid.uuid4().hex}@example.com", hashed_password="x")
    db_session.add_all([org, user])
    await db_session.flush()
    await db_session.commit()

    stub_llm = StubLLMProvider()
    service = RAGService(db_session, EMBED, stub_llm)

    result = await service.ask(
        organization_id=org.id, user_id=user.id, conversation_id=None, question="Anything?"
    )

    assert result.citations == []
    assert "couldn't find enough information" in result.answer
    assert stub_llm.calls == []  # never called - no evidence means no LLM round-trip


async def test_ask_reuses_existing_conversation_and_includes_history(db_session):
    org, chunk, user_id = await _seed_org_with_document(db_session)
    # Captured before the first ask() call: RAGService.ask() rolls back the
    # session's transaction before calling the LLM (to release the pooled
    # connection during generation - see rag_service.py), which expires
    # every ORM object still attached to this session, `org` included.
    # Reading org.id again after that would trigger a lazy reload outside an
    # awaited context. A real caller never hits this - route handlers
    # resolve org context to plain values before invoking RAGService - this
    # is purely an artifact of this test reusing one ORM object across two
    # `ask()` calls on the same session.
    org_id = org.id
    stub_llm = StubLLMProvider()
    service = RAGService(db_session, EMBED, stub_llm)

    first = await service.ask(
        organization_id=org_id,
        user_id=user_id,
        conversation_id=None,
        question=CHUNK_CONTENT,
    )
    # Same exact string again as the second "question" - a real follow-up
    # question would paraphrase, which DeterministicTestEmbeddingProvider
    # cannot resolve semantically (see module docstring above); this test's
    # purpose is to prove history threading, not retrieval robustness.
    second = await service.ask(
        organization_id=org_id,
        user_id=user_id,
        conversation_id=first.conversation_id,
        question=CHUNK_CONTENT,
    )

    assert second.conversation_id == first.conversation_id
    all_messages = (
        await db_session.execute(
            select(Message).where(Message.conversation_id == first.conversation_id)
        )
    ).scalars().all()
    assert len(all_messages) == 4  # 2 user + 2 assistant
    assert len(stub_llm.calls) == 2

    # The second call's prompt should include the first turn as history.
    second_call_messages = stub_llm.calls[-1]
    history_contents = [
        m.content for m in second_call_messages if m.role in ("user", "assistant")
    ]
    assert any(CHUNK_CONTENT in c for c in history_contents[:-1])


async def test_ask_with_unknown_conversation_id_raises_not_found(db_session):
    org, chunk, user_id = await _seed_org_with_document(db_session)
    service = RAGService(db_session, EMBED, StubLLMProvider())

    with pytest.raises(NotFoundError):
        await service.ask(
            organization_id=org.id,
            user_id=user_id,
            conversation_id=uuid.uuid4(),
            question="What is the leave policy?",
        )


async def test_ask_with_conversation_from_another_org_raises_not_found(db_session):
    org, chunk, user_id = await _seed_org_with_document(db_session)
    other_org, _, other_user_id = await _seed_org_with_document(db_session)
    # Plain UUIDs, not the ORM objects - service.ask() below may commit (the
    # real-citation success path does), which unconditionally expires every
    # object the session is tracking, `org` included, even though `org` is
    # never touched by that call. Reading `org.id` afterward would trigger
    # an implicit, un-awaited lazy-reload and crash with MissingGreenlet -
    # the same class of bug already fixed in app/evaluation/eval_fixtures.py.
    org_id, user_id, other_org_id, other_user_id = org.id, user_id, other_org.id, other_user_id
    service = RAGService(db_session, EMBED, StubLLMProvider())

    conv = await service.ask(
        organization_id=other_org_id,
        user_id=other_user_id,
        conversation_id=None,
        question="What is the leave policy?",
    )

    with pytest.raises(NotFoundError):
        await service.ask(
            organization_id=org_id,
            user_id=user_id,
            conversation_id=conv.conversation_id,
            question="Tell me more.",
        )


async def test_zero_valid_citations_is_treated_as_insufficient_evidence(db_session):
    org, chunk, user_id = await _seed_org_with_document(db_session)
    # Fixed response cites a source number that will never be valid.
    stub_llm = StubLLMProvider(fixed_response="The answer is X. [SOURCE-99]")
    service = RAGService(db_session, EMBED, stub_llm)

    result = await service.ask(
        organization_id=org.id,
        user_id=user_id,
        conversation_id=None,
        question=CHUNK_CONTENT,
    )

    assert len(stub_llm.calls) == 1  # retrieval succeeded and the LLM was actually invoked
    assert result.citations == []
    assert "couldn't find enough information" in result.answer


async def test_llm_unavailable_raises_and_user_message_already_persisted(db_session):
    org, chunk, user_id = await _seed_org_with_document(db_session)
    service = RAGService(db_session, EMBED, UnavailableLLMProvider())

    with pytest.raises(LLMUnavailableError):
        await service.ask(
            organization_id=org.id,
            user_id=user_id,
            conversation_id=None,
            question=CHUNK_CONTENT,
        )

    # The user's question was committed before the LLM call - it survives
    # even though the assistant's turn never completed.
    messages = (await db_session.execute(select(Message))).scalars().all()
    user_messages = [m for m in messages if m.role == MessageRole.USER]
    assert any(m.content == CHUNK_CONTENT for m in user_messages)


async def test_embedding_unavailable_raises_clear_error(db_session):
    org, chunk, user_id = await _seed_org_with_document(db_session)
    service = RAGService(db_session, _AlwaysFailsEmbeddingProvider(), StubLLMProvider())

    with pytest.raises(EmbeddingUnavailableError):
        await service.ask(
            organization_id=org.id,
            user_id=user_id,
            conversation_id=None,
            question="What is the annual leave allowance?",
        )


async def test_ask_releases_db_transaction_before_calling_llm(db_session):
    # A local LLM generation can take 30-120s; RAGService must not hold the
    # SQLAlchemy transaction (and the pooled connection behind it) open for
    # that whole span, or a handful of concurrent chats would exhaust
    # DB_POOL_SIZE. Regression test for that fix.
    org, chunk, user_id = await _seed_org_with_document(db_session)

    class _TransactionProbeLLMProvider:
        model = "probe"

        def __init__(self) -> None:
            self.was_in_transaction: bool | None = None

        async def generate(self, messages, *, max_tokens=None) -> str:
            self.was_in_transaction = db_session.in_transaction()
            return "Based on the provided documents. [SOURCE-1]"

    probe = _TransactionProbeLLMProvider()
    service = RAGService(db_session, EMBED, probe)

    await service.ask(
        organization_id=org.id,
        user_id=user_id,
        conversation_id=None,
        question=CHUNK_CONTENT,
    )

    assert probe.was_in_transaction is False


async def test_conversation_history_message_role_mapping():
    # Unit-level sanity check that ChatMessage roles map correctly - covered
    # indirectly above, but explicit here for clarity/documentation.
    assert ChatMessage(role="user", content="x").role == "user"
