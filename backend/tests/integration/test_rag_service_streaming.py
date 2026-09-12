"""Integration tests for RAGService.ask_stream() - the streaming
counterpart to ask() (see tests/integration/test_rag_service.py for the
non-streaming pipeline tests, which this deliberately doesn't duplicate).
Real Postgres via db_session, DeterministicTestEmbeddingProvider, and the
streaming-capable LLM test doubles from app/rag/llm/testing.py."""

import uuid

import pytest
from sqlalchemy import select

from app.core.exceptions import NotFoundError
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.enums import DocumentStatus, MessageRole
from app.models.message import Message
from app.models.organization import Organization
from app.models.user import User
from app.rag.embedding.testing import DeterministicTestEmbeddingProvider
from app.rag.llm.testing import (
    InterruptingLLMProvider,
    StubLLMProvider,
    UnavailableLLMProvider,
)
from app.rag.query_rewrite.base import QueryRewriteResult
from app.rag.query_rewrite.testing import FailingQueryRewriter
from app.rag.streaming_events import (
    CitationsEvent,
    ErrorEvent,
    MessageCompleteEvent,
    MessageStartEvent,
    TokenEvent,
)
from app.services.rag_service import RAGService

EMBED = DeterministicTestEmbeddingProvider(dimensions=768)
CHUNK_CONTENT = "Employees receive 24 days of annual leave per year."


async def _seed_org_with_document(db_session) -> tuple[uuid.UUID, uuid.UUID]:
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

    vector = await EMBED.embed_query(CHUNK_CONTENT)
    chunk = DocumentChunk(
        document_id=document.id,
        chunk_index=0,
        content=CHUNK_CONTENT,
        page_number=14,
        section="Leave Policy",
        char_count=len(CHUNK_CONTENT),
        token_count=12,
        content_hash=uuid.uuid4().hex,
        embedding=vector,
    )
    db_session.add(chunk)
    await db_session.commit()
    return org.id, user.id


async def test_stream_yields_full_event_sequence_with_valid_citation(db_session):
    org_id, user_id = await _seed_org_with_document(db_session)
    service = RAGService(db_session, EMBED, StubLLMProvider())

    events = [
        e
        async for e in service.ask_stream(
            organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]

    assert isinstance(events[0], MessageStartEvent)
    token_events = [e for e in events if isinstance(e, TokenEvent)]
    assert len(token_events) > 0
    assert "".join(e.text for e in token_events).strip() != ""

    citations_events = [e for e in events if isinstance(e, CitationsEvent)]
    assert len(citations_events) == 1
    assert len(citations_events[0].citations) == 1
    assert citations_events[0].citations[0].document_name == "handbook.txt"

    complete_events = [e for e in events if isinstance(e, MessageCompleteEvent)]
    assert len(complete_events) == 1
    assert "[SOURCE-1]" in complete_events[0].answer
    assert complete_events[0].chunks_used == 1

    # Event ordering: start, then all tokens, then citations, then complete.
    kinds = [type(e).__name__ for e in events]
    assert kinds[0] == "MessageStartEvent"
    assert kinds[-2:] == ["CitationsEvent", "MessageCompleteEvent"]

    messages = (
        await db_session.execute(
            select(Message).where(Message.conversation_id == events[0].conversation_id)
        )
    ).scalars().all()
    assert [m.role for m in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert messages[1].citations is not None


async def test_stream_with_no_documents_returns_insufficient_evidence_without_calling_llm(
    db_session,
):
    org = Organization(name="Empty Org", slug=f"empty-{uuid.uuid4().hex[:8]}")
    user = User(email=f"{uuid.uuid4().hex}@example.com", hashed_password="x")
    db_session.add_all([org, user])
    await db_session.flush()
    await db_session.commit()

    stub_llm = StubLLMProvider()
    service = RAGService(db_session, EMBED, stub_llm)

    events = [
        e
        async for e in service.ask_stream(
            organization_id=org.id, user_id=user.id, conversation_id=None, question="Anything?"
        )
    ]

    assert stub_llm.calls == []  # no evidence -> no LLM round-trip at all
    token_events = [e for e in events if isinstance(e, TokenEvent)]
    assert token_events == []
    complete = next(e for e in events if isinstance(e, MessageCompleteEvent))
    assert "couldn't find enough information" in complete.answer


async def test_zero_valid_citations_is_treated_as_insufficient_evidence(db_session):
    org_id, user_id = await _seed_org_with_document(db_session)
    stub_llm = StubLLMProvider(fixed_response="The answer is X. [SOURCE-99]")
    service = RAGService(db_session, EMBED, stub_llm)

    events = [
        e
        async for e in service.ask_stream(
            organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]

    complete = next(e for e in events if isinstance(e, MessageCompleteEvent))
    citations_event = next(e for e in events if isinstance(e, CitationsEvent))
    assert citations_event.citations == []
    assert "couldn't find enough information" in complete.answer


async def test_llm_unavailable_yields_error_event_and_does_not_persist_assistant_message(
    db_session,
):
    org_id, user_id = await _seed_org_with_document(db_session)
    service = RAGService(db_session, EMBED, UnavailableLLMProvider())

    events = [
        e
        async for e in service.ask_stream(
            organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]

    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(error_events) == 1
    assert error_events[0].code == "LLM_UNAVAILABLE"
    assert not any(isinstance(e, MessageCompleteEvent) for e in events)

    messages = (await db_session.execute(select(Message))).scalars().all()
    roles = [m.role for m in messages]
    assert roles.count(MessageRole.USER) == 1  # persisted before the LLM call
    assert roles.count(MessageRole.ASSISTANT) == 0  # never persisted - the turn failed


async def test_interrupted_stream_yields_error_event_with_partial_tokens_but_no_persistence(
    db_session,
):
    org_id, user_id = await _seed_org_with_document(db_session)
    service = RAGService(db_session, EMBED, InterruptingLLMProvider(tokens_before_failure=3))

    events = [
        e
        async for e in service.ask_stream(
            organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]

    token_events = [e for e in events if isinstance(e, TokenEvent)]
    assert len(token_events) == 3  # partial output was streamed before the break
    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(error_events) == 1
    assert error_events[0].code == "LLM_STREAM_INTERRUPTED"
    assert not any(isinstance(e, MessageCompleteEvent) for e in events)

    messages = (await db_session.execute(select(Message))).scalars().all()
    assert [m.role for m in messages] == [MessageRole.USER]  # no misleading assistant message


async def test_retrieval_uses_rewritten_query_but_llm_receives_original_question(db_session):
    """The real discriminating test for the original_query/retrieval_query
    split: the user's literal question ("What about it?") is uncorrelated
    noise to DeterministicTestEmbeddingProvider's content-keyed hashing (see
    its docstring), so retrieval on the raw question would find nothing and
    the turn would be treated as insufficient evidence. A rewriter that
    always rewrites to CHUNK_CONTENT makes retrieval succeed ONLY if
    RAGService actually threads retrieval_query into HybridRetriever.retrieve()
    - and the second assertion proves it does NOT also swap the question the
    LLM is asked to answer."""
    org_id, user_id = await _seed_org_with_document(db_session)

    class _AlwaysRewritesTo:
        def __init__(self, retrieval_query: str) -> None:
            self._retrieval_query = retrieval_query

        async def rewrite(self, *, question, history):
            return QueryRewriteResult(retrieval_query=self._retrieval_query, used_rewrite=True)

    original_question = "What about it?"
    stub_llm = StubLLMProvider()
    service = RAGService(
        db_session, EMBED, stub_llm, query_rewriter=_AlwaysRewritesTo(CHUNK_CONTENT)
    )

    events = [
        e
        async for e in service.ask_stream(
            organization_id=org_id,
            user_id=user_id,
            conversation_id=None,
            question=original_question,
        )
    ]

    citations_event = next(e for e in events if isinstance(e, CitationsEvent))
    assert len(citations_event.citations) == 1  # retrieval succeeded using the rewritten query

    sent_user_message = next(
        m.content for m in stub_llm.calls[-1] if m.role == "user"
    )
    # CHUNK_CONTENT legitimately appears in the CONTEXT section (it's the
    # retrieved chunk's own text) - the property under test is that the
    # QUESTION line carries the original question, not the rewrite.
    assert sent_user_message.endswith(f"QUESTION: {original_question}")


async def test_failing_query_rewriter_falls_back_and_retrieval_still_succeeds(db_session):
    org_id, user_id = await _seed_org_with_document(db_session)
    stub_llm = StubLLMProvider()
    service = RAGService(db_session, EMBED, stub_llm, query_rewriter=FailingQueryRewriter())

    first = [
        e
        async for e in service.ask_stream(
            organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]
    conversation_id = first[0].conversation_id

    events = [
        e
        async for e in service.ask_stream(
            organization_id=org_id,
            user_id=user_id,
            conversation_id=conversation_id,
            question=CHUNK_CONTENT,
        )
    ]

    complete = next(e for e in events if isinstance(e, MessageCompleteEvent))
    assert "[SOURCE-1]" in complete.answer  # retrieval still worked on the original question


async def test_stream_releases_db_transaction_before_calling_llm(db_session):
    org_id, user_id = await _seed_org_with_document(db_session)

    class _TransactionProbeLLMProvider:
        model = "probe"

        def __init__(self) -> None:
            self.was_in_transaction: bool | None = None

        async def generate(self, messages, *, max_tokens=None) -> str:
            return "unused"

        async def stream(self, messages, *, max_tokens=None):
            self.was_in_transaction = db_session.in_transaction()
            yield "Based on the provided documents. [SOURCE-1]"

    probe = _TransactionProbeLLMProvider()
    service = RAGService(db_session, EMBED, probe)

    async for _ in service.ask_stream(
        organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
    ):
        pass

    assert probe.was_in_transaction is False


async def test_cancelling_the_generator_mid_stream_does_not_persist_assistant_message(db_session):
    from app.rag.llm.testing import SlowStreamingLLMProvider

    org_id, user_id = await _seed_org_with_document(db_session)
    slow_llm = SlowStreamingLLMProvider(delay_seconds=0.05, token_count=50)
    service = RAGService(db_session, EMBED, slow_llm)

    gen = service.ask_stream(
        organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
    )
    seen = 0
    async for event in gen:
        if isinstance(event, TokenEvent):
            seen += 1
            if seen >= 2:
                break  # simulates a client disconnect - stop consuming early
    await gen.aclose()

    messages = (await db_session.execute(select(Message))).scalars().all()
    assert [m.role for m in messages] == [MessageRole.USER]
    assert slow_llm.cancelled is True


async def test_regenerate_appends_new_assistant_message_without_duplicating_question(db_session):
    org_id, user_id = await _seed_org_with_document(db_session)
    service = RAGService(db_session, EMBED, StubLLMProvider())

    first = [
        e
        async for e in service.ask_stream(
            organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]
    conversation_id = first[0].conversation_id
    first_assistant_message_id = next(
        e for e in first if isinstance(e, MessageCompleteEvent)
    ).message_id

    events = [
        e
        async for e in service.regenerate_stream(
            organization_id=org_id, conversation_id=conversation_id
        )
    ]

    start_event = next(e for e in events if isinstance(e, MessageStartEvent))
    assert start_event.user_message_id  # reuses the existing user message, doesn't create one
    complete_event = next(e for e in events if isinstance(e, MessageCompleteEvent))
    assert complete_event.message_id != first_assistant_message_id  # a genuinely new message

    messages = (
        await db_session.execute(
            select(Message).where(Message.conversation_id == conversation_id).order_by(
                Message.sequence
            )
        )
    ).scalars().all()
    assert [m.role for m in messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.ASSISTANT,
    ]
    assert len([m for m in messages if m.role == MessageRole.USER]) == 1


async def test_regenerate_with_no_prior_question_raises_not_found(db_session):
    org = Organization(name="Empty Regen Org", slug=f"empty-regen-{uuid.uuid4().hex[:8]}")
    user = User(email=f"{uuid.uuid4().hex}@example.com", hashed_password="x")
    db_session.add_all([org, user])
    await db_session.flush()

    conversation = Conversation(organization_id=org.id, created_by=user.id, title=None)
    db_session.add(conversation)
    await db_session.commit()

    service = RAGService(db_session, EMBED, StubLLMProvider())

    with pytest.raises(NotFoundError, match="regenerate"):
        async for _ in service.regenerate_stream(
            organization_id=org.id, conversation_id=conversation.id
        ):
            pass


async def test_regenerate_cross_tenant_conversation_raises_not_found(db_session):
    org_a_id, user_id = await _seed_org_with_document(db_session)
    org_b = Organization(name="Regen Org B", slug=f"regen-b-{uuid.uuid4().hex[:8]}")
    db_session.add(org_b)
    await db_session.commit()
    # Captured before ask_stream() below - its mid-method rollback() expires
    # ORM objects still attached to this session (see the RAGService.ask()
    # regression test's comment for the full explanation).
    org_b_id = org_b.id

    service = RAGService(db_session, EMBED, StubLLMProvider())
    first = [
        e
        async for e in service.ask_stream(
            organization_id=org_a_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]
    conversation_id = first[0].conversation_id

    with pytest.raises(NotFoundError):
        async for _ in service.regenerate_stream(
            organization_id=org_b_id, conversation_id=conversation_id
        ):
            pass
