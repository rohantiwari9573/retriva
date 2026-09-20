"""Unit tests for LMStudioLLMProvider using httpx.MockTransport - no real
network call, no real LM Studio required. Real-LM-Studio verification is a
separate, explicitly-marked E2E test."""

import json

import httpx
import pytest

from app.rag.llm.base import (
    ChatMessage,
    LLMProviderResponseError,
    LLMProviderStreamInterruptedError,
    LLMProviderTimeoutError,
    LLMProviderUnavailableError,
)
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


async def test_generate_timeout_raises_timeout_error_not_unavailable(monkeypatch):
    # A slow-but-reachable backend must be distinguishable from a broken
    # one - see LLMProviderTimeoutError's docstring for why these are
    # separate exception types rather than both mapping to "unavailable".
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(LLMProviderTimeoutError):
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


def _sse_response(*deltas: str) -> httpx.Response:
    lines = []
    for delta in deltas:
        chunk = {"choices": [{"delta": {"content": delta}}]}
        lines.append(f"data: {json.dumps(chunk)}")
    lines.append("data: [DONE]")
    body = "\n\n".join(lines) + "\n\n"
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


async def test_stream_yields_incremental_deltas(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response("Hello", ", ", "world.")

    provider = _patched_provider(monkeypatch, handler)
    deltas = [d async for d in provider.stream([ChatMessage(role="user", content="hi")])]
    assert deltas == ["Hello", ", ", "world."]


async def test_stream_sends_stream_true_and_model(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _sse_response("ok")

    provider = _patched_provider(monkeypatch, handler, model="qwen2.5-7b-instruct")
    async for _ in provider.stream([ChatMessage(role="user", content="hi")]):
        pass

    assert captured["body"]["model"] == "qwen2.5-7b-instruct"
    assert captured["body"]["stream"] is True


async def test_stream_http_error_status_before_any_output_raises_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(LLMProviderUnavailableError):
        async for _ in provider.stream([ChatMessage(role="user", content="hi")]):
            pass


async def test_stream_connection_error_before_any_output_raises_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(LLMProviderUnavailableError, match="host.docker.internal"):
        async for _ in provider.stream([ChatMessage(role="user", content="hi")]):
            pass


async def test_stream_malformed_chunk_raises_interrupted(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = 'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: not-json\n\n'
        return httpx.Response(200, content=body.encode())

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(LLMProviderStreamInterruptedError):
        async for _ in provider.stream([ChatMessage(role="user", content="hi")]):
            pass


async def test_stream_ignores_role_only_delta_with_no_content(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body.encode())

    provider = _patched_provider(monkeypatch, handler)
    deltas = [d async for d in provider.stream([ChatMessage(role="user", content="hi")])]
    assert deltas == ["Hi"]


async def test_stream_timeout_before_any_output_raises_timeout_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(LLMProviderTimeoutError):
        async for _ in provider.stream([ChatMessage(role="user", content="hi")]):
            pass


async def test_stream_timeout_after_some_output_raises_interrupted_not_timeout(monkeypatch):
    # Once tokens have already been yielded, a timeout is a stream
    # interruption (there's partial output the caller must discard), not a
    # plain "never responded" timeout - see stream()'s comment on this.
    class _FlakyStream(httpx.AsyncByteStream):
        def __init__(self, request: httpx.Request) -> None:
            self._request = request

        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
            raise httpx.ReadTimeout("timed out", request=self._request)

        async def aclose(self) -> None:
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_FlakyStream(request))

    provider = _patched_provider(monkeypatch, handler)
    deltas = []
    with pytest.raises(LLMProviderStreamInterruptedError):
        async for delta in provider.stream([ChatMessage(role="user", content="hi")]):
            deltas.append(delta)
    assert deltas == ["Hello"]


async def test_stream_closes_without_done_after_output_raises_interrupted(monkeypatch):
    # Regression test: this reproduces the real production bug where a
    # quota/capacity-degraded Gemini connection closed after emitting some
    # genuine content, but *without* the `data: [DONE]` sentinel and
    # without raising any httpx exception - aiter_lines() simply stopped
    # yielding and the async-for ended normally. Before the done_received
    # tracking was added, this looked exactly like a real, complete answer
    # to the caller (RAGService), which then fell through to its
    # insufficient-evidence response - a provider failure silently
    # misreported as "no relevant documents found".
    def handler(request: httpx.Request) -> httpx.Response:
        body = 'data: {"choices":[{"delta":{"content":"Candidate appears to have"}}]}\n\n'
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    provider = _patched_provider(monkeypatch, handler)
    deltas = []
    with pytest.raises(LLMProviderStreamInterruptedError):
        async for delta in provider.stream([ChatMessage(role="user", content="hi")]):
            deltas.append(delta)
    assert deltas == ["Candidate appears to have"]


async def test_stream_closes_without_done_and_no_output_raises_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"", headers={"content-type": "text/event-stream"})

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(LLMProviderUnavailableError):
        async for _ in provider.stream([ChatMessage(role="user", content="hi")]):
            pass
