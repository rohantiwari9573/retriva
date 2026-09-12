"""Unit tests for LMStudioEmbeddingProvider using httpx.MockTransport - no
real network call, no real LM Studio required. Real-LM-Studio verification
is a separate, explicitly-marked E2E test (tests/e2e/test_lmstudio_e2e.py)."""

import httpx
import pytest

from app.rag.embedding.base import (
    EmbeddingDimensionMismatchError,
    EmbeddingProviderUnavailableError,
)
from app.rag.embedding.lmstudio import LMStudioEmbeddingProvider


def _patched_provider(monkeypatch, handler, **kwargs) -> LMStudioEmbeddingProvider:
    provider = LMStudioEmbeddingProvider(
        base_url="http://fake-lmstudio.test/v1", dimensions=4, batch_size=2, **kwargs
    )

    real_async_client = httpx.AsyncClient

    def _factory(*args, **client_kwargs):
        client_kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **client_kwargs)

    monkeypatch.setattr("app.rag.embedding.lmstudio.httpx.AsyncClient", _factory)
    return provider


def _embeddings_response(dim: int):
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        data = [
            {"index": i, "embedding": [0.1] * dim} for i, _ in enumerate(body["input"])
        ]
        return httpx.Response(200, json={"data": data})

    return handler


async def test_embed_documents_returns_correctly_ordered_vectors(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        # Return out of order to prove the provider sorts by `index`.
        data = [
            {"index": i, "embedding": [float(i)] * 4}
            for i, _ in reversed(list(enumerate(body["input"])))
        ]
        return httpx.Response(200, json={"data": data})

    provider = _patched_provider(monkeypatch, handler)
    vectors = await provider.embed_documents(["a", "b"])
    assert vectors == [[0.0] * 4, [1.0] * 4]


async def test_embed_documents_batches_according_to_batch_size(monkeypatch):
    seen_batch_sizes = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        seen_batch_sizes.append(len(body["input"]))
        data = [{"index": i, "embedding": [0.0] * 4} for i in range(len(body["input"]))]
        return httpx.Response(200, json={"data": data})

    provider = _patched_provider(monkeypatch, handler)
    await provider.embed_documents(["a", "b", "c", "d", "e"])
    assert seen_batch_sizes == [2, 2, 1]  # batch_size=2 over 5 texts


async def test_embed_query_returns_single_vector(monkeypatch):
    provider = _patched_provider(monkeypatch, _embeddings_response(4))
    vector = await provider.embed_query("hello")
    assert vector == [0.1, 0.1, 0.1, 0.1]


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
    with pytest.raises(EmbeddingDimensionMismatchError):
        await provider.embed_documents(["a"])


async def test_http_error_status_raises_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(EmbeddingProviderUnavailableError):
        await provider.embed_documents(["a"])


async def test_connection_error_raises_unavailable_with_helpful_message(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(EmbeddingProviderUnavailableError, match="host.docker.internal"):
        await provider.embed_documents(["a"])


async def test_malformed_response_shape_raises_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    provider = _patched_provider(monkeypatch, handler)
    with pytest.raises(EmbeddingProviderUnavailableError):
        await provider.embed_documents(["a"])
