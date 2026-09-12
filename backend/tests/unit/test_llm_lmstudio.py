"""Unit tests for LMStudioLLMProvider using httpx.MockTransport - no real
network call, no real LM Studio required. Real-LM-Studio verification is a
separate, explicitly-marked E2E test."""

import httpx
import pytest

from app.rag.llm.base import ChatMessage, LLMProviderResponseError, LLMProviderUnavailableError
from app.rag.llm.lmstudio import LMStudioLLMProvider


def _patched_provider(monkeypatch, handler, **kwargs) -> LMStudioLLMProvider:
    provider = LMStudioLLMProvider(base_url="http://fake-lmstudio.test/v1", **kwargs)
    real_async_client = httpx.AsyncClient

    def _factory(*args, **client_kwargs):
        client_kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **client_kwargs)

    monkeypatch.setattr("app.rag.llm.lmstudio.httpx.AsyncClient", _factory)
    return provider


def _chat_response(content: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
        )

    return handler


async def test_generate_returns_message_content(monkeypatch):
    provider = _patched_provider(monkeypatch, _chat_response("The leave allowance is 24 days."))
    answer = await provider.generate([ChatMessage(role="user", content="What is the policy?")])
    assert answer == "The leave allowance is 24 days."


async def test_generate_sends_all_messages_and_model(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    provider = _patched_provider(monkeypatch, handler, model="qwen2.5-7b-instruct")
    messages = [
        ChatMessage(role="system", content="system rules"),
        ChatMessage(role="user", content="question"),
    ]
    await provider.generate(messages)

    assert captured["body"]["model"] == "qwen2.5-7b-instruct"
    assert captured["body"]["messages"] == [
        {"role": "system", "content": "system rules"},
        {"role": "user", "content": "question"},
    ]


async def test_http_error_status_raises_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(LLMProviderUnavailableError):
        await provider.generate([ChatMessage(role="user", content="hi")])


async def test_connection_error_raises_unavailable_with_helpful_message(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(LLMProviderUnavailableError, match="host.docker.internal"):
        await provider.generate([ChatMessage(role="user", content="hi")])


async def test_timeout_raises_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(LLMProviderUnavailableError):
        await provider.generate([ChatMessage(role="user", content="hi")])


async def test_malformed_response_shape_raises_response_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(LLMProviderResponseError):
        await provider.generate([ChatMessage(role="user", content="hi")])


async def test_empty_choices_raises_response_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(LLMProviderResponseError):
        await provider.generate([ChatMessage(role="user", content="hi")])
