"""QueryRewriter built on top of an existing LLMProvider - not a second
HTTP client. Rewriting a conversational follow-up into a standalone
retrieval query is just another chat completion, so this class takes
whatever LLMProvider the caller is already using for answer generation
(LMStudioLLMProvider in production, StubLLMProvider in tests) and issues
one extra `generate()` call with a dedicated prompt.

Rewriting is an optimization on retrieval quality, never a source of
truth: any failure (disabled, no history, timeout, malformed/empty/
implausible output) falls back to using the original question for
retrieval, and the fallback is always logged with a reason - never hidden.
"""

import asyncio
import time

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import (
    rag_query_rewrite_duration_seconds,
    rag_query_rewrite_fallbacks_total,
    rag_query_rewrites_total,
)
from app.core.telemetry import get_tracer
from app.rag.llm.base import (
    ChatMessage,
    LLMProvider,
    LLMProviderResponseError,
    LLMProviderUnavailableError,
)
from app.rag.prompts.templates import build_query_rewrite_messages
from app.rag.query_rewrite.base import QueryRewriteResult

logger = get_logger(__name__)

# A rewritten query implausibly longer than the original question suggests
# the model answered instead of rewriting (rule 2 violated) - fall back
# rather than search with model-generated prose.
_MAX_LENGTH_MULTIPLIER = 4


class LMStudioQueryRewriter:
    def __init__(
        self,
        llm_provider: LLMProvider,
        *,
        timeout_seconds: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.llm = llm_provider
        self.timeout_seconds = timeout_seconds or settings.QUERY_REWRITE_TIMEOUT_SECONDS
        self.max_tokens = max_tokens or settings.QUERY_REWRITE_MAX_TOKENS

    async def rewrite(
        self, *, question: str, history: list[ChatMessage]
    ) -> QueryRewriteResult:
        """Thin instrumentation wrapper around _rewrite (Phase 8) - the
        underlying rewrite logic and its fallback semantics are unchanged;
        this only adds a span + duration/outcome metrics around the one
        entry point every caller already uses."""
        tracer = get_tracer(__name__)
        start = time.perf_counter()
        with tracer.start_as_current_span("rag.query_rewrite") as span:
            result = await self._rewrite(question=question, history=history)
            rag_query_rewrite_duration_seconds.observe(time.perf_counter() - start)
            rag_query_rewrites_total.labels(used_rewrite=str(result.used_rewrite).lower()).inc()
            if not result.used_rewrite and result.fallback_reason:
                rag_query_rewrite_fallbacks_total.labels(reason=result.fallback_reason).inc()
            span.set_attribute("rag.query_rewrite.used", result.used_rewrite)
            if result.fallback_reason:
                span.set_attribute("rag.query_rewrite.fallback_reason", result.fallback_reason)
            return result

    async def _rewrite(
        self, *, question: str, history: list[ChatMessage]
    ) -> QueryRewriteResult:
        if not settings.QUERY_REWRITE_ENABLED:
            return QueryRewriteResult(
                retrieval_query=question, used_rewrite=False, fallback_reason="disabled"
            )
        if not history:
            # Nothing to resolve, and saves an LLM round-trip on every
            # first turn of a conversation.
            return QueryRewriteResult(
                retrieval_query=question, used_rewrite=False, fallback_reason="no_history"
            )

        messages = build_query_rewrite_messages(question=question, conversation_history=history)
        try:
            raw = await asyncio.wait_for(
                self.llm.generate(messages, max_tokens=self.max_tokens),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            logger.warning("query_rewrite_fallback", reason="timeout")
            return QueryRewriteResult(
                retrieval_query=question, used_rewrite=False, fallback_reason="timeout"
            )
        except (LLMProviderUnavailableError, LLMProviderResponseError) as exc:
            logger.warning("query_rewrite_fallback", reason="llm_error", error=str(exc))
            return QueryRewriteResult(
                retrieval_query=question, used_rewrite=False, fallback_reason="llm_error"
            )

        cleaned = _clean(raw)
        if not cleaned:
            logger.warning("query_rewrite_fallback", reason="empty_output")
            return QueryRewriteResult(
                retrieval_query=question, used_rewrite=False, fallback_reason="empty_output"
            )
        if len(cleaned) > _MAX_LENGTH_MULTIPLIER * max(len(question), 1):
            logger.warning("query_rewrite_fallback", reason="implausible_length")
            return QueryRewriteResult(
                retrieval_query=question, used_rewrite=False, fallback_reason="implausible_length"
            )

        return QueryRewriteResult(retrieval_query=cleaned, used_rewrite=True)


def _clean(text: str) -> str:
    stripped = text.strip()
    # Strip one layer of wrapping quotes a local model sometimes adds
    # despite QUERY_REWRITE_SYSTEM_PROMPT rule 4 ("no quotes").
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        stripped = stripped[1:-1].strip()
    return stripped
