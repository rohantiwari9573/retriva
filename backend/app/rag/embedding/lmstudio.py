"""Embedding provider backed by an OpenAI-compatible /v1/embeddings endpoint.

Works with LM Studio's local server (the zero-cost target for this project)
and, unchanged, with any other OpenAI-compatible embedding endpoint someone
points EMBEDDING_BASE_URL at - the class is named for the primary target,
not because it speaks a proprietary protocol.

A fresh httpx.AsyncClient is created per call rather than cached on the
instance: this provider is used both from FastAPI (one event loop for the
process lifetime) and from Celery tasks (a new event loop per task, via
asyncio.run()). Caching a client - like caching the Redis client that broke
rate limiting in Phase 2 - would bind its connection pool to whichever loop
first touched it and break on the next one.
"""

import time

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import (
    embedding_failures_total,
    embedding_items_total,
    embedding_request_duration_seconds,
    embedding_requests_total,
)
from app.core.telemetry import get_tracer
from app.rag.embedding.base import (
    EmbeddingDimensionMismatchError,
    EmbeddingProviderUnavailableError,
)

logger = get_logger(__name__)
tracer = get_tracer(__name__)


class LMStudioEmbeddingProvider:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        dimensions: int | None = None,
        batch_size: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.EMBEDDING_BASE_URL).rstrip("/")
        self.api_key = api_key or settings.EMBEDDING_API_KEY
        self.model = model or settings.EMBEDDING_MODEL
        self.dimensions = dimensions or settings.EMBEDDING_DIMENSIONS
        self.batch_size = batch_size or settings.EMBEDDING_BATCH_SIZE
        self.timeout_seconds = timeout_seconds or settings.EMBEDDING_REQUEST_TIMEOUT_SECONDS

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        num_batches = -(-len(texts) // self.batch_size)
        for batch_index, start in enumerate(range(0, len(texts), self.batch_size), start=1):
            batch = texts[start : start + self.batch_size]
            logger.info(
                "embedding_batch_started",
                batch_index=batch_index,
                batch_count=num_batches,
                batch_size=len(batch),
                model=self.model,
            )
            vectors.extend(await self._embed_batch(batch))
            logger.info(
                "embedding_batch_completed",
                batch_index=batch_index,
                batch_count=num_batches,
                model=self.model,
            )
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        result = await self._embed_batch([text])
        return result[0]

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """Instrumentation wrapper (Phase 8) around _embed_batch_impl - the
        actual network call and its error handling are unchanged below."""
        provider = settings.EMBEDDING_PROVIDER
        start = time.perf_counter()
        with tracer.start_as_current_span("embedding.request") as span:
            span.set_attribute("llm.provider", provider)
            try:
                result = await self._embed_batch_impl(batch)
            except Exception:
                embedding_requests_total.labels(provider=provider, status="failure").inc()
                embedding_failures_total.labels(provider=provider).inc()
                raise
            embedding_requests_total.labels(provider=provider, status="success").inc()
            embedding_request_duration_seconds.labels(provider=provider).observe(
                time.perf_counter() - start
            )
            embedding_items_total.labels(provider=provider).inc(len(batch))
            return result

    async def _embed_batch_impl(self, batch: list[str]) -> list[list[float]]:
        url = f"{self.base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {"model": self.model, "input": batch}

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "embedding_request_failed",
                status_code=exc.response.status_code,
                base_url=self.base_url,
            )
            raise EmbeddingProviderUnavailableError(
                f"Embedding backend returned HTTP {exc.response.status_code}."
            ) from exc
        except httpx.HTTPError as exc:
            logger.error("embedding_request_unreachable", base_url=self.base_url, exc_info=exc)
            raise EmbeddingProviderUnavailableError(
                f"Could not reach embedding backend at {self.base_url}. "
                "Is LM Studio running with its local server started, and is "
                "the endpoint reachable from this process (use "
                "host.docker.internal, not localhost, when running in Docker)?"
            ) from exc

        try:
            data = sorted(body["data"], key=lambda item: item["index"])
            vectors = [item["embedding"] for item in data]
        except (KeyError, TypeError) as exc:
            raise EmbeddingProviderUnavailableError(
                "Embedding backend returned an unexpected response shape."
            ) from exc

        for vector in vectors:
            if len(vector) != self.dimensions:
                raise EmbeddingDimensionMismatchError(
                    f"Embedding model '{self.model}' returned a "
                    f"{len(vector)}-dimensional vector, but EMBEDDING_DIMENSIONS "
                    f"is configured as {self.dimensions}. Update EMBEDDING_DIMENSIONS "
                    "to match the model actually loaded in LM Studio (and re-run "
                    "the migration if the pgvector column width needs to change)."
                )

        return vectors
