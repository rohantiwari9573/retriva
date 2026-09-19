"""Real Gemini API end-to-end test - talks to the actual, live Gemini
OpenAI-compatible endpoint using a real API key and consumes real free-tier
quota. Skipped by default, always - unlike test_lmstudio_e2e.py's
"reachable" check (LM Studio is only reachable if it's actually running
locally), Gemini's endpoint is always reachable from anywhere with
internet access, so mere reachability says nothing about whether the
caller actually wants to spend real quota running this. Requires
explicit opt-in via the RUN_GEMINI_LIVE_TEST=1 environment variable AND a
real EMBEDDING_API_KEY/LLM_API_KEY - never runs in CI, never runs by
accident locally.

To run for real:
    export RUN_GEMINI_LIVE_TEST=1
    export EMBEDDING_PROVIDER=gemini
    export EMBEDDING_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
    export EMBEDDING_API_KEY=<your real key, never committed>
    export EMBEDDING_MODEL=gemini-embedding-001
    export LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
    export LLM_API_KEY=<your real key, never committed>
    export LLM_MODEL=gemini-3.6-flash
    pytest tests/e2e/test_gemini_e2e.py -v
"""

import os

import pytest

from app.core.config import settings
from app.rag.embedding.gemini import GeminiEmbeddingProvider
from app.rag.llm.base import ChatMessage
from app.rag.llm.lmstudio import LMStudioLLMProvider

_OPTED_IN = os.environ.get("RUN_GEMINI_LIVE_TEST") == "1"

pytestmark = pytest.mark.skipif(
    not _OPTED_IN,
    reason=(
        "Gemini live test skipped by default (never runs in CI or by accident) - "
        "set RUN_GEMINI_LIVE_TEST=1 with a real EMBEDDING_API_KEY/LLM_API_KEY to run it. "
        "See this file's module docstring for the full opt-in procedure."
    ),
)


async def test_real_gemini_embedding_returns_768_dimensional_vector():
    provider = GeminiEmbeddingProvider()
    vector = await provider.embed_query("What is the leave policy?")
    assert len(vector) == settings.EMBEDDING_DIMENSIONS == 768
    assert all(isinstance(v, float) for v in vector)


async def test_real_gemini_generation_returns_real_answer():
    provider = LMStudioLLMProvider()  # generic OpenAI-compatible client - see its docstring
    answer = await provider.generate(
        [ChatMessage(role="user", content="Reply with exactly the word: OK")]
    )
    assert "OK" in answer
