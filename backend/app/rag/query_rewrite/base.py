"""QueryRewriter interface.

Same shape as the other RAG provider Protocols (EmbeddingProvider,
LLMProvider): the application layer calls this abstraction, never a
concrete implementation directly. Unlike those two, a QueryRewriter does
NOT own its own network client - it's built on top of an existing
LLMProvider (see lmstudio.py), since rewriting is just another chat
completion, not a distinct external dependency.
"""

from dataclasses import dataclass
from typing import Protocol

from app.rag.llm.base import ChatMessage


@dataclass(frozen=True)
class QueryRewriteResult:
    """`retrieval_query` is what HybridRetriever should search with;
    `original_query` (kept alongside it by the caller, not duplicated here)
    remains the actual question the LLM answers - the two must never be
    conflated. `used_rewrite=False` means retrieval_query == the original
    question, either because rewriting was skipped (no history) or because
    it failed and fell back - `fallback_reason` distinguishes the two so
    the caller can log/measure honestly (never silently hide a failure)."""

    retrieval_query: str
    used_rewrite: bool
    fallback_reason: str | None = None


class QueryRewriter(Protocol):
    async def rewrite(
        self, *, question: str, history: list[ChatMessage]
    ) -> QueryRewriteResult: ...
