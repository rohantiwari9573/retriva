"""Test-only QueryRewriter doubles - never imported from application code."""

from app.rag.llm.base import ChatMessage
from app.rag.query_rewrite.base import QueryRewriteResult


class StubQueryRewriter:
    """Deterministic: prefixes the question when history is non-empty,
    passes it through unchanged when history is empty - enough to assert
    that a caller actually threads the rewritten query into retrieval
    without depending on real language understanding."""

    def __init__(self, *, prefix: str = "REWRITTEN: ") -> None:
        self.prefix = prefix
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def rewrite(
        self, *, question: str, history: list[ChatMessage]
    ) -> QueryRewriteResult:
        self.calls.append((question, history))
        if not history:
            return QueryRewriteResult(
                retrieval_query=question, used_rewrite=False, fallback_reason="no_history"
            )
        return QueryRewriteResult(retrieval_query=f"{self.prefix}{question}", used_rewrite=True)


class FailingQueryRewriter:
    """Always falls back to the original question - for testing that a
    rewrite failure never breaks the chat turn, only degrades retrieval."""

    async def rewrite(
        self, *, question: str, history: list[ChatMessage]
    ) -> QueryRewriteResult:
        return QueryRewriteResult(
            retrieval_query=question, used_rewrite=False, fallback_reason="stub_failure"
        )
