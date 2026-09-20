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
from app.rag.llm.base import (
    LLMProviderResponseError,
    LLMProviderStreamInterruptedError,
    LLMProviderTimeoutError,
    LLMProviderUnavailableError,
)
from app.rag.llm.testing import (
    FlakyLLMProvider,
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
    RetryingEvent,
    TokenEvent,
)
from app.services.rag_service import RAGService

EMBED = DeterministicTestEmbeddingProvider(dimensions=768)
CHUNK_CONTENT = "Employees receive 24 days of annual leave per year."


def _no_retry_delay(monkeypatch) -> None:
    """Zeroes the automatic-retry backoff for a test - the real delays
    (settings.LLM_STREAM_RETRY_DELAYS_SECONDS, a couple of seconds each)
    exist to be kind to a real rate-limited upstream, not something a
    fast, deterministic test suite should actually wait through."""
    monkeypatch.setattr(
        "app.services.rag_service.settings.LLM_STREAM_RETRY_DELAYS_SECONDS", (0.0, 0.0)
    )
    monkeypatch.setattr(
        "app.services.rag_service.settings.LLM_STREAM_RETRY_MAX_DELAY_SECONDS", 0.0
    )


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


async def _run_with_log_capture(
    monkeypatch, service, **ask_stream_kwargs
) -> tuple[list, list[dict]]:
    """Runs ask_stream() while capturing calls to rag_service's module-level
    structlog logger. Monkeypatching logger.info directly rather than
    reconfiguring structlog's global processor chain (the pattern
    test_observability_security.py uses) - that global toggle proved
    order-dependent/flaky when this file's other async tests ran in the
    same session, so this avoids shared mutable global state entirely."""
    import app.services.rag_service as rag_service_module

    captured: list[dict] = []
    original_info = rag_service_module.logger.info

    def _capture_info(event: str, **kwargs):
        captured.append({"event": event, **kwargs})
        return original_info(event, **kwargs)

    monkeypatch.setattr(rag_service_module.logger, "info", _capture_info)
    events = [e async for e in service.ask_stream(**ask_stream_kwargs)]
    return events, captured


async def test_diagnostic_log_for_uncited_answer_reports_metadata_not_content(
    db_session, monkeypatch
):
    """Temporary diagnostic (rag_answer_before_citation_validation) added to
    distinguish, for a cleanly-completed-but-uncited generation, a normal-
    length answer that just lacks a citation from an unusually short/
    truncated one - see the investigation into a production case where a
    real [DONE] and no ErrorEvent still preceded only ~12 tokens of output.
    This asserts the log line carries exactly the metadata needed for that
    (lengths/counts/booleans) and never the raw answer text itself."""
    org_id, user_id = await _seed_org_with_document(db_session)
    raw_answer = "some answer without a valid citation"
    stub_llm = StubLLMProvider(fixed_response=raw_answer)
    service = RAGService(db_session, EMBED, stub_llm)

    events, log_entries = await _run_with_log_capture(
        monkeypatch,
        service,
        organization_id=org_id,
        user_id=user_id,
        conversation_id=None,
        question=CHUNK_CONTENT,
    )

    diagnostic = next(
        e for e in log_entries if e.get("event") == "rag_answer_before_citation_validation"
    )
    assert diagnostic["raw_answer_character_length"] == len(raw_answer)
    assert diagnostic["raw_answer_whitespace_token_count"] == len(raw_answer.split())
    assert diagnostic["raw_answer_has_source_marker"] is False
    assert diagnostic["number_of_validated_citations"] == 0
    assert diagnostic["stream_completed_normally"] is True
    assert diagnostic["tokens_generated"] > 0

    # The actual point of this diagnostic: metadata only, never content.
    for entry in log_entries:
        assert raw_answer not in str(entry)

    # Behavior is unchanged - still the existing insufficient-evidence path.
    complete = next(e for e in events if isinstance(e, MessageCompleteEvent))
    assert "couldn't find enough information" in complete.answer


async def test_diagnostic_log_for_cited_answer_reports_metadata_not_content(
    db_session, monkeypatch
):
    org_id, user_id = await _seed_org_with_document(db_session)
    service = RAGService(db_session, EMBED, StubLLMProvider())  # cites every SOURCE tag it sees

    events, log_entries = await _run_with_log_capture(
        monkeypatch,
        service,
        organization_id=org_id,
        user_id=user_id,
        conversation_id=None,
        question=CHUNK_CONTENT,
    )

    diagnostic = next(
        e for e in log_entries if e.get("event") == "rag_answer_before_citation_validation"
    )
    assert diagnostic["raw_answer_has_source_marker"] is True
    assert diagnostic["number_of_validated_citations"] > 0

    complete = next(e for e in events if isinstance(e, MessageCompleteEvent))
    assert "[SOURCE-1]" in complete.answer
    assert "couldn't find enough information" not in complete.answer


async def test_llm_unavailable_yields_error_event_and_does_not_persist_assistant_message(
    db_session, monkeypatch
):
    # UnavailableLLMProvider's status_code-less error is retryable (see
    # _classify_stream_retry) - it still always fails, so the final
    # outcome (one ErrorEvent, no persisted assistant message) is
    # unchanged, just reached after the automatic retries exhaust.
    _no_retry_delay(monkeypatch)
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


async def test_interrupted_stream_retries_then_yields_error_after_exhausting_attempts(
    db_session, monkeypatch
):
    """LLMProviderStreamInterruptedError is retryable (see
    _classify_stream_retry), so a provider that always drops the
    connection now gets 3 total attempts (the original test's name/intent
    - "an interrupted stream yields an error event, never a misleading
    persisted answer" - still holds, it now happens after exhausting the
    retry budget rather than on the very first attempt)."""
    _no_retry_delay(monkeypatch)
    org_id, user_id = await _seed_org_with_document(db_session)
    provider = InterruptingLLMProvider(tokens_before_failure=3)
    service = RAGService(db_session, EMBED, provider)

    events = [
        e
        async for e in service.ask_stream(
            organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]

    token_events = [e for e in events if isinstance(e, TokenEvent)]
    assert len(token_events) == 9  # 3 tokens x 3 total attempts (1 initial + 2 retries)
    retrying_events = [e for e in events if isinstance(e, RetryingEvent)]
    assert [e.attempt for e in retrying_events] == [2, 3]
    assert all(e.max_attempts == 3 for e in retrying_events)
    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(error_events) == 1
    assert error_events[0].code == "LLM_STREAM_INTERRUPTED"
    assert not any(isinstance(e, MessageCompleteEvent) for e in events)

    messages = (await db_session.execute(select(Message))).scalars().all()
    assert [m.role for m in messages] == [MessageRole.USER]  # no misleading assistant message
    # Exactly 3 attempts were made against the provider - not more, not
    # fewer (proves the retry budget is enforced, not open-ended).
    assert provider.call_count == 3


async def test_successful_first_attempt_makes_exactly_one_call_and_no_retry_event(
    db_session, monkeypatch
):
    """A provider that never fails must never trigger a retry - the base
    case every other test in this block is a variation of."""
    _no_retry_delay(monkeypatch)
    org_id, user_id = await _seed_org_with_document(db_session)
    provider = FlakyLLMProvider(failures=[])
    service = RAGService(db_session, EMBED, provider)

    events = [
        e
        async for e in service.ask_stream(
            organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]

    assert provider.call_count == 1
    assert not any(isinstance(e, RetryingEvent) for e in events)
    complete = next(e for e in events if isinstance(e, MessageCompleteEvent))
    assert "[SOURCE-1]" in complete.answer


@pytest.mark.parametrize("status_code", [429, 503])
async def test_retryable_status_then_success_retries_once_and_succeeds(
    db_session, monkeypatch, status_code
):
    """A single transient failure (429 or 503) on the first attempt,
    succeeding on the automatic retry - the scenario this whole feature
    exists for."""
    _no_retry_delay(monkeypatch)
    org_id, user_id = await _seed_org_with_document(db_session)
    provider = FlakyLLMProvider(
        failures=[LLMProviderUnavailableError("upstream busy", status_code=status_code)]
    )
    service = RAGService(db_session, EMBED, provider)

    events = [
        e
        async for e in service.ask_stream(
            organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]

    assert provider.call_count == 2
    retrying_events = [e for e in events if isinstance(e, RetryingEvent)]
    assert len(retrying_events) == 1
    assert retrying_events[0].attempt == 2
    assert retrying_events[0].max_attempts == 3
    assert not any(isinstance(e, ErrorEvent) for e in events)
    complete = next(e for e in events if isinstance(e, MessageCompleteEvent))
    assert "[SOURCE-1]" in complete.answer

    messages = (await db_session.execute(select(Message))).scalars().all()
    assert [m.role for m in messages] == [MessageRole.USER, MessageRole.ASSISTANT]


async def test_timeout_error_is_retryable(db_session, monkeypatch):
    """A timeout (never reached the model at all) is retryable - see
    _classify_stream_retry."""
    _no_retry_delay(monkeypatch)
    org_id, user_id = await _seed_org_with_document(db_session)
    provider = FlakyLLMProvider(
        failures=[LLMProviderTimeoutError("did not respond within 120s")]
    )
    service = RAGService(db_session, EMBED, provider)

    events = [
        e
        async for e in service.ask_stream(
            organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]

    assert provider.call_count == 2
    retrying_events = [e for e in events if isinstance(e, RetryingEvent)]
    assert len(retrying_events) == 1
    complete = next(e for e in events if isinstance(e, MessageCompleteEvent))
    assert "[SOURCE-1]" in complete.answer


async def test_two_failures_then_success_uses_exactly_two_retries(db_session, monkeypatch):
    """Attempt 1 fails, attempt 2 fails, attempt 3 succeeds - exactly the
    maximum retry budget (LLM_STREAM_MAX_RETRIES=2), never exceeded."""
    _no_retry_delay(monkeypatch)
    org_id, user_id = await _seed_org_with_document(db_session)
    provider = FlakyLLMProvider(
        failures=[
            LLMProviderUnavailableError("upstream busy", status_code=429),
            LLMProviderUnavailableError("upstream busy", status_code=503),
        ]
    )
    service = RAGService(db_session, EMBED, provider)

    events = [
        e
        async for e in service.ask_stream(
            organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]

    assert provider.call_count == 3
    retrying_events = [e for e in events if isinstance(e, RetryingEvent)]
    assert [e.attempt for e in retrying_events] == [2, 3]
    complete = next(e for e in events if isinstance(e, MessageCompleteEvent))
    assert "[SOURCE-1]" in complete.answer


async def test_non_retryable_status_never_triggers_automatic_retry(db_session, monkeypatch):
    """A 401 (auth) is never transient - retrying it wastes an attempt on
    something that will never succeed, so it must fail immediately."""
    _no_retry_delay(monkeypatch)
    org_id, user_id = await _seed_org_with_document(db_session)
    provider = FlakyLLMProvider(
        failures=[LLMProviderUnavailableError("bad credentials", status_code=401)]
    )
    service = RAGService(db_session, EMBED, provider)

    events = [
        e
        async for e in service.ask_stream(
            organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]

    assert provider.call_count == 1  # no retry attempted
    assert not any(isinstance(e, RetryingEvent) for e in events)
    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(error_events) == 1
    assert error_events[0].code == "LLM_UNAVAILABLE"


async def test_malformed_response_error_never_triggers_automatic_retry(db_session, monkeypatch):
    """LLMProviderResponseError signals a real incompatibility (unusable
    response shape), not a transient condition - never retried."""
    _no_retry_delay(monkeypatch)
    org_id, user_id = await _seed_org_with_document(db_session)
    provider = FlakyLLMProvider(
        failures=[LLMProviderResponseError("unexpected response shape")]
    )
    service = RAGService(db_session, EMBED, provider)

    events = [
        e
        async for e in service.ask_stream(
            organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]

    assert provider.call_count == 1
    assert not any(isinstance(e, RetryingEvent) for e in events)
    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(error_events) == 1
    assert error_events[0].code == "LLM_STREAM_INTERRUPTED"


async def test_legitimate_insufficient_evidence_never_triggers_automatic_retry(db_session):
    """No retrieved chunks -> the insufficient-evidence answer is returned
    without ever calling the LLM at all (existing behavior) - critically,
    this is not an exception, so nothing in the retry path can fire."""
    org = Organization(name="Empty Org 2", slug=f"empty2-{uuid.uuid4().hex[:8]}")
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

    assert stub_llm.calls == []
    assert not any(isinstance(e, RetryingEvent) for e in events)
    complete = next(e for e in events if isinstance(e, MessageCompleteEvent))
    assert "couldn't find enough information" in complete.answer


async def test_partial_tokens_on_failed_attempt_are_not_duplicated_into_successful_retry(
    db_session, monkeypatch
):
    """Attempt 1 streams real partial tokens, then fails; attempt 2
    succeeds cleanly. The final persisted/complete answer must reflect
    ONLY attempt 2 - attempt 1's partial tokens were real TokenEvents at
    the time (the frontend must discard them on RetryingEvent, see
    InterruptedAssistantBubble/use-chat-stream.tsx), but the backend's own
    final answer must never concatenate them in."""
    _no_retry_delay(monkeypatch)
    org_id, user_id = await _seed_org_with_document(db_session)
    provider = FlakyLLMProvider(
        failures=[
            (
                ["Candidate ", "appears ", "to "],
                LLMProviderStreamInterruptedError("connection lost mid-generation"),
            )
        ]
    )
    service = RAGService(db_session, EMBED, provider)

    events = [
        e
        async for e in service.ask_stream(
            organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]

    assert provider.call_count == 2
    # The raw SSE stream legitimately DOES include attempt 1's partial
    # TokenEvents - the frontend needs them in real time before the
    # failure is even known, and RetryingEvent is the signal that tells
    # it to discard them (see InterruptedAssistantBubble). What must never
    # happen is that partial text leaking into the *final*, authoritative
    # answer, checked below.
    token_events = [e for e in events if isinstance(e, TokenEvent)]
    assert "".join(e.text for e in token_events).startswith("Candidate appears to ")
    # The RetryingEvent must sit between attempt 1's tokens and attempt
    # 2's - the ordering signal the frontend relies on to know exactly
    # when to reset its accumulated buffer.
    token_index = next(i for i, e in enumerate(events) if isinstance(e, TokenEvent))
    retrying_index = next(i for i, e in enumerate(events) if isinstance(e, RetryingEvent))
    assert token_index < retrying_index
    complete = next(e for e in events if isinstance(e, MessageCompleteEvent))
    # Attempt 1's partial text must never leak into the final answer.
    assert "Candidate" not in complete.answer
    assert "[SOURCE-1]" in complete.answer

    messages = (await db_session.execute(select(Message))).scalars().all()
    assistant_messages = [m for m in messages if m.role == MessageRole.ASSISTANT]
    assert len(assistant_messages) == 1
    assert "Candidate" not in assistant_messages[0].content


async def test_retry_after_header_bounds_the_delay(db_session, monkeypatch):
    """A provider-supplied Retry-After is honored but capped at
    LLM_STREAM_RETRY_MAX_DELAY_SECONDS - never allowed to stall a request
    for as long as a real upstream might ask (e.g. a daily quota reset)."""
    monkeypatch.setattr(
        "app.services.rag_service.settings.LLM_STREAM_RETRY_MAX_DELAY_SECONDS", 0.05
    )
    org_id, user_id = await _seed_org_with_document(db_session)
    provider = FlakyLLMProvider(
        failures=[
            LLMProviderUnavailableError(
                "upstream busy", status_code=429, retry_after_seconds=999.0
            )
        ]
    )
    service = RAGService(db_session, EMBED, provider)

    import time as time_module

    start = time_module.perf_counter()
    events = [
        e
        async for e in service.ask_stream(
            organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]
    elapsed = time_module.perf_counter() - start

    assert elapsed < 2.0  # nowhere near the requested 999s - the cap held
    assert provider.call_count == 2
    assert any(isinstance(e, MessageCompleteEvent) for e in events)


async def test_all_retries_exhausted_persists_no_duplicate_assistant_messages(
    db_session, monkeypatch
):
    """One user submission, all attempts fail: exactly one USER message
    and zero ASSISTANT messages - automatic retries must never persist
    an intermediate/failed attempt as if it were a real answer."""
    _no_retry_delay(monkeypatch)
    org_id, user_id = await _seed_org_with_document(db_session)
    provider = FlakyLLMProvider(
        failures=[
            LLMProviderUnavailableError("upstream busy", status_code=429),
            LLMProviderUnavailableError("upstream busy", status_code=503),
            LLMProviderUnavailableError("upstream busy", status_code=429),
        ]
    )
    service = RAGService(db_session, EMBED, provider)

    events = [
        e
        async for e in service.ask_stream(
            organization_id=org_id, user_id=user_id, conversation_id=None, question=CHUNK_CONTENT
        )
    ]

    assert provider.call_count == 3
    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(error_events) == 1
    assert not any(isinstance(e, MessageCompleteEvent) for e in events)

    messages = (await db_session.execute(select(Message))).scalars().all()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.USER


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
