"""Test-only LLM provider - never imported from application code.

Real LM Studio generation is nondeterministic and slow; integration tests
need the RAG pipeline (retrieve -> build context -> prompt -> generate ->
validate citations) to run deterministically in CI. This stub echoes back
the SOURCE tags it was given in the prompt, which is exactly what's needed
to exercise citation validation without a real model - it does NOT simulate
language understanding, and tests built on it must not pretend otherwise.
"""

import re

from app.rag.llm.base import ChatMessage, LLMProviderUnavailableError

_SOURCE_TAG_RE = re.compile(r"\[SOURCE-\d+\]")


class StubLLMProvider:
    """Cites every SOURCE tag it finds in the user message, in order, once
    each - deterministic and good enough to test the citation-validation
    path. `fixed_response`, if set, is returned verbatim instead (used to
    test citation-stripping behavior with specific/fabricated tags)."""

    def __init__(self, *, fixed_response: str | None = None) -> None:
        self.model = "stub-test-provider"
        self.fixed_response = fixed_response
        self.calls: list[list[ChatMessage]] = []

    async def generate(self, messages: list[ChatMessage], *, max_tokens: int | None = None) -> str:
        self.calls.append(messages)
        if self.fixed_response is not None:
            return self.fixed_response

        user_content = next((m.content for m in reversed(messages) if m.role == "user"), "")
        tags = _SOURCE_TAG_RE.findall(user_content)
        if not tags:
            return (
                "I couldn't find enough information in your organization's "
                "documents to answer that."
            )
        citations = " ".join(dict.fromkeys(tags))  # de-duplicate, preserve order
        return f"Based on the provided documents, here is the answer. {citations}"


class UnavailableLLMProvider:
    """Always raises, for testing the LM-Studio-unreachable error path
    without touching real error classes from a different module."""

    model = "unavailable-test-provider"

    async def generate(self, messages: list[ChatMessage], *, max_tokens: int | None = None) -> str:
        raise LLMProviderUnavailableError("Stub: LLM backend unavailable.")
