"""Test-only LLM provider - never imported from application code.

Real LM Studio generation is nondeterministic and slow; integration tests
need the RAG pipeline (retrieve -> build context -> prompt -> generate ->
validate citations) to run deterministically in CI. This stub echoes back
the SOURCE tags it was given in the prompt, which is exactly what's needed
to exercise citation validation without a real model - it does NOT simulate
language understanding, and tests built on it must not pretend otherwise.
"""

import asyncio
import re
from collections.abc import AsyncGenerator

from app.rag.llm.base import (
    ChatMessage,
    LLMProviderStreamInterruptedError,
    LLMProviderUnavailableError,
)

_SOURCE_TAG_RE = re.compile(r"\[SOURCE-\d+\]")


def _answer_for(messages: list[ChatMessage], fixed_response: str | None) -> str:
    if fixed_response is not None:
        return fixed_response
    user_content = next((m.content for m in reversed(messages) if m.role == "user"), "")
    tags = _SOURCE_TAG_RE.findall(user_content)
    if not tags:
        return (
            "I couldn't find enough information in your organization's "
            "documents to answer that."
        )
    citations = " ".join(dict.fromkeys(tags))  # de-duplicate, preserve order
    return f"Based on the provided documents, here is the answer. {citations}"


class StubLLMProvider:
    """Cites every SOURCE tag it finds in the user message, in order, once
    each - deterministic and good enough to test the citation-validation
    path. `fixed_response`, if set, is returned verbatim instead (used to
    test citation-stripping behavior with specific/fabricated tags).

    stream() yields the same answer split into word-sized deltas, so
    streaming tests exercise real incremental accumulation instead of a
    single chunk containing the whole answer."""

    def __init__(self, *, fixed_response: str | None = None) -> None:
        self.model = "stub-test-provider"
        self.fixed_response = fixed_response
        self.calls: list[list[ChatMessage]] = []

    async def generate(self, messages: list[ChatMessage], *, max_tokens: int | None = None) -> str:
        self.calls.append(messages)
        return _answer_for(messages, self.fixed_response)

    async def stream(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> AsyncGenerator[str, None]:
        self.calls.append(messages)
        answer = _answer_for(messages, self.fixed_response)
        words = answer.split(" ")
        for i, word in enumerate(words):
            await asyncio.sleep(0)  # yield control, mimicking real incremental delivery
            yield word if i == len(words) - 1 else word + " "


class UnavailableLLMProvider:
    """Always raises, for testing the LM-Studio-unreachable error path
    without touching real error classes from a different module."""

    model = "unavailable-test-provider"

    async def generate(self, messages: list[ChatMessage], *, max_tokens: int | None = None) -> str:
        raise LLMProviderUnavailableError("Stub: LLM backend unavailable.")

    async def stream(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> AsyncGenerator[str, None]:
        raise LLMProviderUnavailableError("Stub: LLM backend unavailable.")
        yield ""  # pragma: no cover - makes this an async generator; unreachable


class InterruptingLLMProvider:
    """Streams a few real tokens, then breaks - for testing
    LLMProviderStreamInterruptedError handling (a partial answer that must
    never be persisted or shown to the user as if it were complete)."""

    model = "interrupting-test-provider"

    def __init__(self, *, tokens_before_failure: int = 2) -> None:
        self.tokens_before_failure = tokens_before_failure

    async def generate(self, messages: list[ChatMessage], *, max_tokens: int | None = None) -> str:
        raise LLMProviderStreamInterruptedError("Stub: connection lost mid-generation.")

    async def stream(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> AsyncGenerator[str, None]:
        for i in range(self.tokens_before_failure):
            await asyncio.sleep(0)
            yield f"partial-{i} "
        raise LLMProviderStreamInterruptedError("Stub: connection lost mid-generation.")


class SlowStreamingLLMProvider:
    """Yields tokens with a real delay between them, for testing
    client-disconnect/cancellation handling - a fast fake would complete
    before the test has a chance to cancel it."""

    model = "slow-streaming-test-provider"

    def __init__(self, *, delay_seconds: float = 0.05, token_count: int = 50) -> None:
        self.delay_seconds = delay_seconds
        self.token_count = token_count
        self.cancelled = False

    async def generate(self, messages: list[ChatMessage], *, max_tokens: int | None = None) -> str:
        return " ".join(f"token-{i}" for i in range(self.token_count))

    async def stream(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> AsyncGenerator[str, None]:
        try:
            for i in range(self.token_count):
                await asyncio.sleep(self.delay_seconds)
                yield f"token-{i} "
        except (asyncio.CancelledError, GeneratorExit):
            # GeneratorExit is what `aclose()` throws in here when a caller
            # tears this generator down early (e.g. RAGService.ask_stream()
            # closing it via contextlib.aclosing() on a client disconnect) -
            # CancelledError is what a cancelled asyncio.Task throws into
            # whatever it's awaiting. Both mean the same thing for this
            # fake: generation was stopped before completion.
            self.cancelled = True
            raise
