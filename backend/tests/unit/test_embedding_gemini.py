"""Unit tests for GeminiEmbeddingProvider using httpx.MockTransport - no
real network call, no real Gemini API key required. An explicit opt-in
live smoke test against the real Gemini API (only run when a real key is
present locally) lives in tests/e2e/test_gemini_e2e.py - never in CI."""

import json
import math

import httpx
import pytest

from app.rag.embedding.base import (
    EmbeddingDimensionMismatchError,
    EmbeddingProviderUnavailableError,
)
from app.rag.embedding.gemini import GeminiEmbeddingProvider


def _patched_provider(monkeypatch, handler, **kwargs) -> GeminiEmbeddingProvider:
    kwargs.setdefault("api_key", "fake-gemini-key")
    provider = GeminiEmbeddingProvider(
        base_url="http://fake-gemini.test/v1beta/openai", dimensions=4, batch_size=2, **kwargs
    )

    real_async_client = httpx.AsyncClient

    def _factory(*args, **client_kwargs):
        client_kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **client_kwargs)

    monkeypatch.setattr("app.rag.embedding.gemini.httpx.AsyncClient", _factory)
    return provider


def _embeddings_response(dim: int, value: float = 3.0):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data = [{"index": i, "embedding": [value] * dim} for i, _ in enumerate(body["input"])]
        return httpx.Response(200, json={"data": data})

    return handler


def test_missing_api_key_raises_unavailable_at_construction(monkeypatch):
    # Patch the settings default rather than passing api_key="" - the
    # constructor's `or settings.EMBEDDING_API_KEY` fallback (same pattern
    # LMStudioEmbeddingProvider uses for every parameter) treats an
    # explicitly empty string the same as "not overridden", by design.
    monkeypatch.setattr("app.rag.embedding.gemini.settings.EMBEDDING_API_KEY", "")
    with pytest.raises(EmbeddingProviderUnavailableError, match="EMBEDDING_API_KEY"):
        GeminiEmbeddingProvider(base_url="http://fake-gemini.test/v1beta/openai", dimensions=768)


async def test_request_includes_standard_dimensions_field(monkeypatch):
    seen_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_payloads.append(body)
        data = [{"index": i, "embedding": [1.0] * 4} for i in range(len(body["input"]))]
        return httpx.Response(200, json={"data": data})

    provider = _patched_provider(monkeypatch, handler)
    await provider.embed_query("hello")
    assert seen_payloads[0]["dimensions"] == 4
    assert seen_payloads[0]["model"] == provider.model


async def test_authorization_header_carries_api_key(monkeypatch):
    seen_headers = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0] * 4}]})

    provider = _patched_provider(monkeypatch, handler, api_key="secret-key-123")
    await provider.embed_query("hello")
    assert seen_headers[0] == "Bearer secret-key-123"


async def test_result_is_l2_normalized(monkeypatch):
    # gemini-embedding-001 does not auto-normalize truncated output - the
    # provider must do it, since pgvector cosine_distance correctness
    # aside, Google's own docs treat this as a requirement at this width.
    provider = _patched_provider(monkeypatch, _embeddings_response(dim=4, value=3.0))
    vector = await provider.embed_query("hello")
    norm = math.sqrt(sum(v * v for v in vector))
    assert norm == pytest.approx(1.0)


async def test_embed_documents_returns_correctly_ordered_normalized_vectors(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # Return out of order to prove the provider sorts by `index`.
        data = [
            {"index": i, "embedding": [float(i) + 1.0, 0.0, 0.0, 0.0]}
            for i, _ in reversed(list(enumerate(body["input"])))
        ]
        return httpx.Response(200, json={"data": data})

    provider = _patched_provider(monkeypatch, handler)
    vectors = await provider.embed_documents(["a", "b"])
    assert vectors == [[1.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]]


async def test_response_without_index_field_trusts_response_order(monkeypatch):
    # Confirmed live against the real Gemini API: its /embeddings response
    # items carry only `object`/`embedding`, no `index` at all - unlike
    # standard OpenAI. Sorting by a missing key would KeyError; the
    # provider must fall back to trusting response order instead.
    def handler(request: httpx.Request) -> httpx.Response:
        # Each input gets a distinguishably-directioned vector, in request
        # order, so the assertion actually proves order was preserved
        # (not just that normalization happened).
        shapes = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
        body = json.loads(request.content)
        data = [
            {"object": "embedding", "embedding": shapes[i]} for i in range(len(body["input"]))
        ]
        return httpx.Response(200, json={"object": "list", "data": data, "model": "m"})

    provider = _patched_provider(monkeypatch, handler)
    vectors = await provider.embed_documents(["a", "b"])
    assert vectors == [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]


async def test_empty_input_returns_empty_list_without_a_request(monkeypatch):
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"data": []})

    provider = _patched_provider(monkeypatch, handler)
    assert await provider.embed_documents([]) == []
    assert called is False


async def test_dimension_mismatch_raises_clear_error(monkeypatch):
    provider = _patched_provider(monkeypatch, _embeddings_response(dim=8))  # configured dims=4
    with pytest.raises(EmbeddingDimensionMismatchError, match="EMBEDDING_DIMENSIONS"):
        await provider.embed_documents(["a"])


async def test_invalid_api_key_http_error_raises_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid API key"}})

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(EmbeddingProviderUnavailableError):
        await provider.embed_documents(["a"])


async def test_timeout_raises_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(EmbeddingProviderUnavailableError, match="Could not reach"):
        await provider.embed_documents(["a"])


async def test_malformed_response_shape_raises_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(EmbeddingProviderUnavailableError):
        await provider.embed_documents(["a"])
