"""Unit tests for LMStudioQueryRewriter - built on a fake LLMProvider, no
real network/LM Studio. Query rewriting is an optimization, never a source
of truth for retrieval - every test here checks either that a real rewrite
happens, or that a failure/edge case falls back to the original question
(and reports why) rather than breaking the chat turn."""

import asyncio

import pytest

from app.core.config import settings
from app.rag.llm.base import (
    ChatMessage,
    LLMProviderResponseError,
    LLMProviderUnavailableError,
)
from app.rag.query_rewrite.lmstudio import LMStudioQueryRewriter

HISTORY = [
    ChatMessage(role="user", content="How is authentication handled in Nexus?"),
    ChatMessage(role="assistant", content="Nexus uses JWT access tokens. [SOURCE-1]"),
]


class _FixedLLMProvider:
    model = "fixed-test"

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[list[ChatMessage]] = []

    async def generate(self, messages, *, max_tokens=None) -> str:
        self.calls.append(messages)
        return self.response


class _RaisingLLMProvider:
    model = "raising-test"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def generate(self, messages, *, max_tokens=None) -> str:
        raise self.exc


class _SlowLLMProvider:
    model = "slow-test"

    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds

    async def generate(self, messages, *, max_tokens=None) -> str:
        await asyncio.sleep(self.delay_seconds)
        return "irrelevant"


async def test_no_history_skips_rewrite_without_calling_llm():
    llm = _FixedLLMProvider("should never be returned")
    rewriter = LMStudioQueryRewriter(llm)

    result = await rewriter.rewrite(question="What is PostgreSQL?", history=[])

    assert result.retrieval_query == "What is PostgreSQL?"
    assert result.used_rewrite is False
    assert result.fallback_reason == "no_history"
    assert llm.calls == []


async def test_contextual_followup_with_history_is_rewritten():
    llm = _FixedLLMProvider("How does Nexus validate authentication JWT tokens?")
    rewriter = LMStudioQueryRewriter(llm)

    result = await rewriter.rewrite(question="How is the token validated?", history=HISTORY)

    assert result.used_rewrite is True
    assert result.retrieval_query == "How does Nexus validate authentication JWT tokens?"
    assert result.fallback_reason is None
    assert len(llm.calls) == 1


async def test_rewrite_disabled_falls_back_without_calling_llm(monkeypatch):
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", False)
    llm = _FixedLLMProvider("should never be returned")
    rewriter = LMStudioQueryRewriter(llm)

    result = await rewriter.rewrite(question="Follow-up question", history=HISTORY)

    assert result.used_rewrite is False
    assert result.fallback_reason == "disabled"
    assert llm.calls == []


async def test_wrapping_quotes_are_stripped():
    llm = _FixedLLMProvider('"How does Nexus validate JWT tokens?"')
    rewriter = LMStudioQueryRewriter(llm)

    result = await rewriter.rewrite(question="How is it validated?", history=HISTORY)

    assert result.retrieval_query == "How does Nexus validate JWT tokens?"
    assert result.used_rewrite is True


async def test_empty_output_falls_back():
    llm = _FixedLLMProvider("   ")
    rewriter = LMStudioQueryRewriter(llm)

    result = await rewriter.rewrite(question="How is it validated?", history=HISTORY)

    assert result.used_rewrite is False
    assert result.retrieval_query == "How is it validated?"
    assert result.fallback_reason == "empty_output"


async def test_implausibly_long_output_falls_back():
    # A model that answered instead of rewriting - far longer than 4x the
    # original question.
    llm = _FixedLLMProvider("A" * 500)
    rewriter = LMStudioQueryRewriter(llm)

    result = await rewriter.rewrite(question="How is it validated?", history=HISTORY)

    assert result.used_rewrite is False
    assert result.fallback_reason == "implausible_length"


async def test_llm_unavailable_falls_back():
    llm = _RaisingLLMProvider(LLMProviderUnavailableError("down"))
    rewriter = LMStudioQueryRewriter(llm)

    result = await rewriter.rewrite(question="How is it validated?", history=HISTORY)

    assert result.used_rewrite is False
    assert result.retrieval_query == "How is it validated?"
    assert result.fallback_reason == "llm_error"


async def test_malformed_response_falls_back():
    llm = _RaisingLLMProvider(LLMProviderResponseError("bad shape"))
    rewriter = LMStudioQueryRewriter(llm)

    result = await rewriter.rewrite(question="How is it validated?", history=HISTORY)

    assert result.used_rewrite is False
    assert result.fallback_reason == "llm_error"


async def test_timeout_falls_back():
    llm = _SlowLLMProvider(delay_seconds=0.2)
    rewriter = LMStudioQueryRewriter(llm, timeout_seconds=0.01)

    result = await rewriter.rewrite(question="How is it validated?", history=HISTORY)

    assert result.used_rewrite is False
    assert result.retrieval_query == "How is it validated?"
    assert result.fallback_reason == "timeout"


async def test_standalone_question_with_history_is_still_sent_for_rewrite():
    # Rule 5 of the prompt says the model should echo standalone questions
    # unchanged - this test just verifies the wiring passes it through
    # correctly, not that a real model would behave, that's an eval concern.
    llm = _FixedLLMProvider("What is PostgreSQL?")
    rewriter = LMStudioQueryRewriter(llm)

    result = await rewriter.rewrite(question="What is PostgreSQL?", history=HISTORY)

    assert result.used_rewrite is True
    assert result.retrieval_query == "What is PostgreSQL?"


@pytest.fixture(autouse=True)
def _restore_query_rewrite_enabled():
    original = settings.QUERY_REWRITE_ENABLED
    yield
    settings.QUERY_REWRITE_ENABLED = original
