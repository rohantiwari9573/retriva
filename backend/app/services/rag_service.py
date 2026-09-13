"""RAGService: orchestrates the full retrieval-augmented answer pipeline.

Deliberately NOT a FastAPI route handler's job - this class has no
knowledge of HTTP, and every step it performs (validate access, retrieve,
fuse, build context, call the LLM, validate citations, persist) is a plain
method call, which is what makes it possible to test the whole pipeline
against a StubLLMProvider/DeterministicTestEmbeddingProvider without a real
LM Studio.

Latency is measured per stage (embedding happens inside HybridRetriever, so
"retrieval_latency_ms" below covers query-embedding + vector search +
keyword search + fusion + hydration together - see docs/rag.md for why
those aren't split further: they're one sequential critical path per
request, not independently parallelizable work worth separate metrics).
"""

import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    EmbeddingUnavailableError,
    LLMTimeoutError,
    LLMUnavailableError,
    NotFoundError,
    RetrievalFailedError,
)
from app.core.logging import get_logger
from app.core.metrics import (
    llm_time_to_first_token_seconds,
    rag_context_build_duration_seconds,
    rag_insufficient_evidence_total,
    rag_requests_total,
    stream_completed_total,
    stream_duration_seconds,
    stream_failed_total,
    stream_interrupted_total,
    stream_requests_total,
    stream_time_to_first_token_seconds,
)
from app.core.telemetry import get_tracer
from app.models.conversation import Conversation
from app.models.enums import MessageRole
from app.models.message import Message
from app.rag.citations import Citation, validate_citations
from app.rag.context_builder import build_context
from app.rag.embedding.base import EmbeddingProvider, EmbeddingProviderUnavailableError
from app.rag.llm.base import (
    ChatMessage,
    LLMProvider,
    LLMProviderResponseError,
    LLMProviderStreamInterruptedError,
    LLMProviderTimeoutError,
    LLMProviderUnavailableError,
)
from app.rag.prompts.templates import INSUFFICIENT_EVIDENCE_ANSWER, build_messages
from app.rag.query_rewrite.base import QueryRewriter
from app.rag.retrieval.hybrid import HybridRetriever
from app.rag.streaming_events import (
    CitationsEvent,
    ErrorEvent,
    MessageCompleteEvent,
    MessageStartEvent,
    StreamEvent,
    TokenEvent,
)
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.message_repository import MessageRepository

logger = get_logger(__name__)
tracer = get_tracer(__name__)

_TITLE_MAX_CHARS = 200


@dataclass(frozen=True)
class ChatResult:
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    answer: str
    citations: list[Citation]
    chunks_considered: int
    chunks_used: int


class RAGService:
    def __init__(
        self,
        db: AsyncSession,
        embedding_provider: EmbeddingProvider,
        llm_provider: LLMProvider,
        query_rewriter: QueryRewriter | None = None,
    ) -> None:
        self.db = db
        self.conversations = ConversationRepository(db)
        self.messages = MessageRepository(db)
        self.retriever = HybridRetriever(db, embedding_provider)
        self.llm = llm_provider
        # Only needed by ask_stream() - ask() (the Phase 5 non-streaming
        # endpoint, kept for backward compatibility) doesn't rewrite queries,
        # so callers that only use ask() may omit this.
        self.query_rewriter = query_rewriter

    async def ask(
        self,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        question: str,
    ) -> ChatResult:
        total_start = time.perf_counter()
        conversation = await self._resolve_conversation(
            organization_id=organization_id,
            user_id=user_id,
            conversation_id=conversation_id,
            question=question,
        )
        # Captured as a plain value, not read off `conversation` again later:
        # the mid-method rollback() below (to release the DB connection
        # during LLM generation) expires every ORM object in the session,
        # and re-reading conversation.id afterward would trigger a lazy
        # SELECT outside of an awaited context (MissingGreenlet).
        resolved_conversation_id = conversation.id

        # Commit the user's message before any retrieval/LLM work - a local
        # LLM call can take 30-120s, and holding get_db's transaction open
        # that whole time would tie up a DB connection from the pool for
        # every concurrent chat (DB_POOL_SIZE=10 by default). Same
        # explicit-mid-handler-commit precedent as Phase 3/4's early-commit
        # failure paths.
        user_message = await self.messages.create(
            conversation_id=resolved_conversation_id, role=MessageRole.USER, content=question
        )
        await self.db.commit()

        history = await self._load_history(resolved_conversation_id, before=user_message)

        retrieval_start = time.perf_counter()
        try:
            retrieval = await self.retriever.retrieve(
                organization_id=organization_id, query=question
            )
        except EmbeddingProviderUnavailableError as exc:
            raise EmbeddingUnavailableError(str(exc)) from exc
        except SQLAlchemyError as exc:
            raise RetrievalFailedError(str(exc)) from exc
        retrieval_latency_ms = (time.perf_counter() - retrieval_start) * 1000

        insufficient = not retrieval.chunks or (
            retrieval.best_vector_similarity is not None
            and retrieval.best_vector_similarity < settings.RETRIEVAL_MIN_SIMILARITY
        )
        rag_requests_total.labels(streaming="false").inc()

        llm_latency_ms = 0.0
        if insufficient:
            rag_insufficient_evidence_total.inc()
            answer_text = INSUFFICIENT_EVIDENCE_ANSWER
            citations: list[Citation] = []
            chunks_used = 0
        else:
            with tracer.start_as_current_span("rag.context_build"):
                context_build_start = time.perf_counter()
                built_context = build_context(retrieval.chunks)
                rag_context_build_duration_seconds.observe(
                    time.perf_counter() - context_build_start
                )
            messages = build_messages(
                context_text=built_context.text,
                question=question,
                conversation_history=history,
            )

            # Nothing left to persist until the LLM responds - release the
            # connection SQLAlchemy's autobegin opened for the retrieval
            # queries above rather than holding it (and its pool slot) for
            # the 30-120s a local LLM generation can take. rollback(), not
            # commit(): this transaction never wrote anything, so rollback
            # is the honest description of "nothing to save" and ends the
            # transaction identically.
            await self.db.rollback()

            llm_start = time.perf_counter()
            try:
                with tracer.start_as_current_span("rag.llm_generation"):
                    raw_answer = await self.llm.generate(messages)
            except LLMProviderTimeoutError as exc:
                raise LLMTimeoutError(str(exc)) from exc
            except LLMProviderUnavailableError as exc:
                raise LLMUnavailableError(str(exc)) from exc
            except LLMProviderResponseError as exc:
                raise LLMUnavailableError(str(exc)) from exc
            llm_latency_ms = (time.perf_counter() - llm_start) * 1000

            with tracer.start_as_current_span("rag.citation_validation"):
                validated = validate_citations(raw_answer, built_context)
            if not validated.citations:
                # The model answered but cited nothing we can verify -
                # trusting an uncited factual claim is exactly what the
                # citation mechanism exists to prevent, so this is treated
                # the same as insufficient evidence rather than returned as-is.
                rag_insufficient_evidence_total.inc()
                answer_text = INSUFFICIENT_EVIDENCE_ANSWER
                citations = []
                chunks_used = 0
            else:
                answer_text = validated.answer
                citations = validated.citations
                chunks_used = len(built_context.blocks)

        assistant_message = await self.messages.create(
            conversation_id=resolved_conversation_id,
            role=MessageRole.ASSISTANT,
            content=answer_text,
            citations=[asdict(c) for c in citations] or None,
            chunks_considered=retrieval.candidates_considered,
            chunks_used=chunks_used,
        )
        await self.db.commit()

        total_latency_ms = (time.perf_counter() - total_start) * 1000
        logger.info(
            "chat_completed",
            organization_id=str(organization_id),
            conversation_id=str(resolved_conversation_id),
            chunks_considered=retrieval.candidates_considered,
            chunks_used=chunks_used,
            citations_count=len(citations),
            retrieval_latency_ms=round(retrieval_latency_ms, 1),
            llm_latency_ms=round(llm_latency_ms, 1),
            total_latency_ms=round(total_latency_ms, 1),
        )

        return ChatResult(
            conversation_id=resolved_conversation_id,
            message_id=assistant_message.id,
            answer=answer_text,
            citations=citations,
            chunks_considered=retrieval.candidates_considered,
            chunks_used=chunks_used,
        )

    async def ask_stream(
        self,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        question: str,
    ) -> AsyncIterator[StreamEvent]:
        """Streaming counterpart to ask(): same pipeline (resolve
        conversation -> persist user message -> load history -> retrieve ->
        build context -> generate -> validate citations -> persist assistant
        message), plus a query-rewrite step before retrieval, yielding
        StreamEvent objects as the turn progresses instead of returning one
        ChatResult at the end.

        Never raises past the first yielded event: any failure after
        MessageStartEvent has been emitted is reported as an ErrorEvent
        (the SSE stream has already started, so a normal JSON error
        response is no longer possible - see docs/streaming.md) and this
        generator then returns without persisting an assistant message,
        rather than pretending a failed turn succeeded.
        """
        conversation = await self._resolve_conversation(
            organization_id=organization_id,
            user_id=user_id,
            conversation_id=conversation_id,
            question=question,
        )
        resolved_conversation_id = conversation.id

        user_message = await self.messages.create(
            conversation_id=resolved_conversation_id, role=MessageRole.USER, content=question
        )
        await self.db.commit()
        yield MessageStartEvent(
            conversation_id=resolved_conversation_id, user_message_id=user_message.id
        )

        history = await self._load_history(resolved_conversation_id, before=user_message)

        async for event in self._answer_stream(
            organization_id=organization_id,
            resolved_conversation_id=resolved_conversation_id,
            question=question,
            history=history,
        ):
            yield event

    async def regenerate_stream(
        self, *, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> AsyncIterator[StreamEvent]:
        """Regenerates a new answer for the conversation's most recent user
        message, without duplicating that question or destroying the
        previous assistant reply - it appends a brand-new ASSISTANT message
        after it. `Message.sequence` keeps ordering honest; "show only the
        latest reply" (if desired) is a frontend rendering concern, not a
        delete here.

        Emits the same event sequence as ask_stream(), except
        MessageStartEvent.user_message_id refers to the EXISTING user
        message being re-answered, not a newly created one.
        """
        conversation = await self.conversations.get_by_id_in_org(conversation_id, organization_id)
        if conversation is None:
            raise NotFoundError("Conversation not found.", code="CONVERSATION_NOT_FOUND")
        resolved_conversation_id = conversation.id

        last_user_message = await self.messages.get_last_by_role(
            resolved_conversation_id, role=MessageRole.USER
        )
        if last_user_message is None:
            raise NotFoundError(
                "This conversation has no question to regenerate an answer for.",
                code="NO_MESSAGE_TO_REGENERATE",
            )
        question = last_user_message.content

        yield MessageStartEvent(
            conversation_id=resolved_conversation_id, user_message_id=last_user_message.id
        )

        history = await self._load_history(resolved_conversation_id, before=last_user_message)

        async for event in self._answer_stream(
            organization_id=organization_id,
            resolved_conversation_id=resolved_conversation_id,
            question=question,
            history=history,
        ):
            yield event

    async def _answer_stream(
        self,
        *,
        organization_id: uuid.UUID,
        resolved_conversation_id: uuid.UUID,
        question: str,
        history: list[ChatMessage],
    ) -> AsyncIterator[StreamEvent]:
        """Shared tail of ask_stream()/regenerate_stream(): rewrite ->
        retrieve -> build context -> generate -> validate citations ->
        persist assistant message -> citations/message_complete events.
        Both callers have already emitted MessageStartEvent and resolved
        the question + preceding history by this point."""
        stream_requests_total.inc()
        try:
            async for event in self._answer_stream_impl(
                organization_id=organization_id,
                resolved_conversation_id=resolved_conversation_id,
                question=question,
                history=history,
            ):
                yield event
        except GeneratorExit:
            # Starlette tears this generator down via aclose() on client
            # disconnect - GeneratorExit is not an Exception subclass, so it
            # would otherwise propagate silently past every except clause
            # below with no operational signal at all. Must re-raise: a
            # generator that swallows GeneratorExit instead of stopping
            # raises RuntimeError in the interpreter.
            stream_interrupted_total.labels(reason="client_disconnect").inc()
            raise

    async def _answer_stream_impl(
        self,
        *,
        organization_id: uuid.UUID,
        resolved_conversation_id: uuid.UUID,
        question: str,
        history: list[ChatMessage],
    ) -> AsyncIterator[StreamEvent]:
        total_start = time.perf_counter()
        rag_requests_total.labels(streaming="true").inc()

        # Nothing written since the caller's last commit - release the
        # connection before the (possibly slow) query-rewrite LLM call,
        # same reasoning as the rollback in ask().
        await self.db.rollback()

        rewrite_start = time.perf_counter()
        retrieval_query = question
        rewrite_fallback_count = 0
        if self.query_rewriter is not None:
            rewrite_result = await self.query_rewriter.rewrite(question=question, history=history)
            retrieval_query = rewrite_result.retrieval_query
            if not rewrite_result.used_rewrite:
                rewrite_fallback_count = 1
        query_rewrite_latency_ms = (time.perf_counter() - rewrite_start) * 1000

        retrieval_start = time.perf_counter()
        try:
            retrieval = await self.retriever.retrieve(
                organization_id=organization_id, query=retrieval_query
            )
        except EmbeddingProviderUnavailableError as exc:
            stream_failed_total.labels(reason="provider_error").inc()
            yield ErrorEvent(code="EMBEDDING_UNAVAILABLE", message=str(exc))
            return
        except SQLAlchemyError as exc:
            stream_failed_total.labels(reason="provider_error").inc()
            yield ErrorEvent(code=RetrievalFailedError.code, message=str(exc))
            return
        retrieval_latency_ms = (time.perf_counter() - retrieval_start) * 1000

        insufficient = not retrieval.chunks or (
            retrieval.best_vector_similarity is not None
            and retrieval.best_vector_similarity < settings.RETRIEVAL_MIN_SIMILARITY
        )

        context_build_start = time.perf_counter()
        if insufficient:
            rag_insufficient_evidence_total.inc()
            answer_text = INSUFFICIENT_EVIDENCE_ANSWER
            citations: list[Citation] = []
            chunks_used = 0
            tokens_generated = 0
            ttft_ms: float | None = None
            generation_latency_ms = 0.0
            context_build_latency_ms = (time.perf_counter() - context_build_start) * 1000
        else:
            with tracer.start_as_current_span("rag.context_build"):
                built_context = build_context(retrieval.chunks)
                messages = build_messages(
                    context_text=built_context.text,
                    question=question,  # the ORIGINAL question - retrieval_query is retrieval-only
                    conversation_history=history,
                )
            context_build_latency_ms = (time.perf_counter() - context_build_start) * 1000
            rag_context_build_duration_seconds.observe(context_build_latency_ms / 1000)

            # Release the connection again before the answer-generation
            # stream, which can run for the bulk of the request.
            await self.db.rollback()

            generation_start = time.perf_counter()
            first_token_at: float | None = None
            deltas: list[str] = []
            try:
                # contextlib.aclosing(), not a bare `async for`: if a client
                # disconnects and Starlette tears down this generator via
                # aclose(), GeneratorExit only unwinds *this* frame - the
                # inner LLM stream generator isn't implicitly closed by
                # that, and would otherwise rely on non-deterministic GC to
                # release its connection. aclosing() guarantees the inner
                # generator's aclose() runs as part of this frame's own
                # teardown, so cancellation actually propagates into the
                # provider (closing its httpx stream) instead of leaking it.
                with tracer.start_as_current_span("rag.llm_generation") as llm_span:
                    async with contextlib.aclosing(self.llm.stream(messages)) as token_stream:
                        async for delta in token_stream:
                            if first_token_at is None:
                                first_token_at = time.perf_counter()
                            deltas.append(delta)
                            yield TokenEvent(text=delta)
                    llm_span.set_attribute("llm.streaming", True)
            except LLMProviderTimeoutError as exc:
                stream_failed_total.labels(reason="timeout").inc()
                yield ErrorEvent(code="LLM_TIMEOUT", message=str(exc))
                return
            except LLMProviderUnavailableError as exc:
                stream_failed_total.labels(reason="provider_error").inc()
                yield ErrorEvent(code="LLM_UNAVAILABLE", message=str(exc))
                return
            except (LLMProviderStreamInterruptedError, LLMProviderResponseError) as exc:
                stream_failed_total.labels(reason="provider_interrupted").inc()
                yield ErrorEvent(code="LLM_STREAM_INTERRUPTED", message=str(exc))
                return
            generation_latency_ms = (time.perf_counter() - generation_start) * 1000
            ttft_ms = (
                (first_token_at - generation_start) * 1000 if first_token_at is not None else None
            )
            tokens_generated = len(deltas)
            if ttft_ms is not None:
                llm_time_to_first_token_seconds.labels(provider=settings.LLM_PROVIDER).observe(
                    ttft_ms / 1000
                )
                stream_time_to_first_token_seconds.observe(ttft_ms / 1000)

            raw_answer = "".join(deltas)
            with tracer.start_as_current_span("rag.citation_validation"):
                validated = validate_citations(raw_answer, built_context)
            if not validated.citations:
                answer_text = INSUFFICIENT_EVIDENCE_ANSWER
                citations = []
                chunks_used = 0
            else:
                answer_text = validated.answer
                citations = validated.citations
                chunks_used = len(built_context.blocks)

        with tracer.start_as_current_span("message.persist"):
            assistant_message = await self.messages.create(
                conversation_id=resolved_conversation_id,
                role=MessageRole.ASSISTANT,
                content=answer_text,
                citations=[asdict(c) for c in citations] or None,
                chunks_considered=retrieval.candidates_considered,
                chunks_used=chunks_used,
            )
            await self.db.commit()

        yield CitationsEvent(citations=citations)
        yield MessageCompleteEvent(
            message_id=assistant_message.id,
            answer=answer_text,
            chunks_considered=retrieval.candidates_considered,
            chunks_used=chunks_used,
        )

        total_latency_ms = (time.perf_counter() - total_start) * 1000
        stream_duration_seconds.observe(total_latency_ms / 1000)
        stream_completed_total.inc()
        logger.info(
            "chat_stream_completed",
            organization_id=str(organization_id),
            conversation_id=str(resolved_conversation_id),
            chunks_considered=retrieval.candidates_considered,
            chunks_used=chunks_used,
            citations_count=len(citations),
            query_rewrite_latency_ms=round(query_rewrite_latency_ms, 1),
            rewrite_fallback_count=rewrite_fallback_count,
            retrieval_latency_ms=round(retrieval_latency_ms, 1),
            context_build_latency_ms=round(context_build_latency_ms, 1),
            llm_time_to_first_token_ms=round(ttft_ms, 1) if ttft_ms is not None else None,
            llm_generation_latency_ms=round(generation_latency_ms, 1),
            tokens_generated=tokens_generated,
            total_latency_ms=round(total_latency_ms, 1),
        )

    async def _resolve_conversation(
        self,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        question: str,
    ) -> Conversation:
        if conversation_id is None:
            title = question.strip()[:_TITLE_MAX_CHARS] or None
            return await self.conversations.create(
                organization_id=organization_id, created_by=user_id, title=title
            )
        conversation = await self.conversations.get_by_id_in_org(conversation_id, organization_id)
        if conversation is None:
            raise NotFoundError("Conversation not found.", code="CONVERSATION_NOT_FOUND")
        return conversation

    async def _load_history(
        self, conversation_id: uuid.UUID, *, before: Message
    ) -> list[ChatMessage]:
        rows = await self.messages.list_recent_before(
            conversation_id, before=before, limit=settings.CONVERSATION_HISTORY_MAX_MESSAGES
        )
        return [
            ChatMessage(
                role="user" if row.role == MessageRole.USER else "assistant", content=row.content
            )
            for row in rows
        ]
