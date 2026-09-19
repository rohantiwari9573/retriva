"""Proves LMStudioLLMProvider - already a generic OpenAI-compatible client
despite its name (see its own module docstring) - works correctly when
configured for Google Gemini's OpenAI-compatible endpoint
(https://generativelanguage.googleapis.com/v1beta/openai), with no code
change: only base_url/api_key/model differ from the local LM Studio case.

This is deliberate: Gemini's chat/completions and SSE streaming wire
format is standard OpenAI-compatible (confirmed against Gemini's own
docs), so a second, duplicate provider class would just be the same
~200 lines of httpx/error-handling logic under a different name. The
tests below exist because "provider selection is configuration-driven"
is a real, testable claim, not because the underlying code differs.

No real network call, no real Gemini API key required - see
tests/e2e/test_gemini_e2e.py for the explicit opt-in live smoke test.
"""

import json

import httpx
import pytest

from app.rag.llm.base import ChatMessage, LLMProviderTimeoutError, LLMProviderUnavailableError
from app.rag.llm.lmstudio import LMStudioLLMProvider

GEMINI_BASE_URL = "http://fake-gemini.test/v1beta/openai"
GEMINI_MODEL = "gemini-3.6-flash"


def _gemini_configured_provider(monkeypatch, handler, **kwargs) -> LMStudioLLMProvider:
    provider = LMStudioLLMProvider(
        base_url=GEMINI_BASE_URL,
        api_key=kwargs.pop("api_key", "fake-gemini-api-key"),
        model=kwargs.pop("model", GEMINI_MODEL),
        **kwargs,
    )
    real_async_client = httpx.AsyncClient

    def _factory(*args, **client_kwargs):
        client_kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **client_kwargs)

    monkeypatch.setattr("app.rag.llm.lmstudio.httpx.AsyncClient", _factory)
    return provider


def test_provider_initializes_with_gemini_configuration():
    provider = LMStudioLLMProvider(
        base_url=GEMINI_BASE_URL, api_key="fake-key", model=GEMINI_MODEL
    )
    assert provider.base_url == GEMINI_BASE_URL
    assert provider.model == GEMINI_MODEL
    assert provider.api_key == "fake-key"


async def test_generate_against_gemini_endpoint_returns_content(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == f"{GEMINI_BASE_URL}/chat/completions"
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "Paris."}}]}
        )

    provider = _gemini_configured_provider(monkeypatch, handler)
    answer = await provider.generate([ChatMessage(role="user", content="Capital of France?")])
    assert answer == "Paris."


async def test_generate_sends_bearer_token_and_configured_model(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = _gemini_configured_provider(monkeypatch, handler, api_key="real-gemini-key")
    await provider.generate([ChatMessage(role="user", content="hi")])

    assert captured["auth"] == "Bearer real-gemini-key"
    assert captured["body"]["model"] == GEMINI_MODEL


async def test_missing_or_invalid_api_key_raises_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "API key not valid"}})

    provider = _gemini_configured_provider(monkeypatch, handler, api_key="")
    with pytest.raises(LLMProviderUnavailableError):
        await provider.generate([ChatMessage(role="user", content="hi")])


async def test_provider_failure_raises_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "model overloaded"}})

    provider = _gemini_configured_provider(monkeypatch, handler)
    with pytest.raises(LLMProviderUnavailableError):
        await provider.generate([ChatMessage(role="user", content="hi")])


async def test_provider_timeout_raises_timeout_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = _gemini_configured_provider(monkeypatch, handler)
    with pytest.raises(LLMProviderTimeoutError):
        await provider.generate([ChatMessage(role="user", content="hi")])


def _sse_response(*deltas: str) -> httpx.Response:
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': d}}]})}" for d in deltas]
    lines.append("data: [DONE]")
    body = "\n\n".join(lines) + "\n\n"
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


async def test_streaming_generation_against_gemini_endpoint(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return _sse_response("The ", "capital ", "is Paris.")

    provider = _gemini_configured_provider(monkeypatch, handler)
    deltas = [d async for d in provider.stream([ChatMessage(role="user", content="hi")])]
    assert deltas == ["The ", "capital ", "is Paris."]
