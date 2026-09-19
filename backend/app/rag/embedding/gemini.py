"""Embedding provider for Google's Gemini API, via its official
OpenAI-compatible endpoint (https://ai.google.dev/gemini-api/docs/openai) -
same request/response shape as LMStudioEmbeddingProvider, so this exists as
a separate class only for the two things that are genuinely different, not
as a rewrite of the same logic:

1. Gemini's embedding models (gemini-embedding-001) default to 3072
   dimensions - getting the 768 width this project's pgvector column
   requires means sending the standard OpenAI `dimensions` field, which
   Gemini's OpenAI-compatible layer maps internally to its own
   `outputDimensionality` parameter (confirmed against Gemini's own docs -
   this is not a Gemini-specific wire field, just the standard OpenAI
   embeddings request shape already used for e.g. text-embedding-3-*).
2. Unlike gemini-embedding-2, gemini-embedding-001 does NOT automatically
   re-normalize a truncated embedding to unit length - Google's own docs
   call this out explicitly and recommend manual L2 normalization when
   requesting fewer than the model's native 3072 dimensions. Deliberately
   NOT using gemini-embedding-2 here: its embedding space is incompatible
   with gemini-embedding-001's, so switching would silently invalidate
   every vector already stored under vector(768) - see
   docs/evaluation-baseline.md and the Gemini provider integration doc for
   why 001 is the migration-free choice.

pgvector's cosine_distance() already divides by each vector's own norm, so
retrieval would work even without this normalization step - it's done
anyway because Google's docs call it a requirement for this model, and
skipping a one-line correctness step to save a division has no upside.
"""

import math
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


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]


class GeminiEmbeddingProvider:
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
        if not self.api_key:
            raise EmbeddingProviderUnavailableError(
                "EMBEDDING_API_KEY is required for the Gemini embedding provider - "
                "get a free key at https://aistudio.google.com/apikey and set it "
                "server-side only (never in frontend code, never committed)."
            )

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
        """Instrumentation wrapper, matching LMStudioEmbeddingProvider's own
        split between metrics/tracing and the actual network call."""
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
        # `dimensions` is the standard OpenAI embeddings request field
        # (also used for e.g. text-embedding-3-*) - Gemini's OpenAI-
        # compatible layer maps it to its own outputDimensionality
        # parameter internally, so this is not a Gemini-specific field.
        payload = {"model": self.model, "input": batch, "dimensions": self.dimensions}

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
                f"Gemini embedding backend returned HTTP {exc.response.status_code}."
            ) from exc
        except httpx.HTTPError as exc:
            logger.error("embedding_request_unreachable", base_url=self.base_url, exc_info=exc)
            raise EmbeddingProviderUnavailableError(
                f"Could not reach Gemini embedding backend at {self.base_url}. "
                "Check EMBEDDING_API_KEY and network connectivity."
            ) from exc

        try:
            raw_items = body["data"]
            # Unlike standard OpenAI (and LM Studio), Gemini's real
            # OpenAI-compatible /embeddings response omits the `index`
            # field on each item entirely (confirmed live: response items
            # only carry `object`/`embedding`) - sorting by an index that
            # doesn't exist would KeyError. Sort only when every item
            # actually has one (still correct for any endpoint that does
            # include it); otherwise trust response order, which for
            # Gemini's batch embeddings matches request order.
            if all("index" in item for item in raw_items):
                raw_items = sorted(raw_items, key=lambda item: item["index"])
            vectors = [item["embedding"] for item in raw_items]
        except (KeyError, TypeError) as exc:
            raise EmbeddingProviderUnavailableError(
                "Gemini embedding backend returned an unexpected response shape."
            ) from exc

        for vector in vectors:
            if len(vector) != self.dimensions:
                raise EmbeddingDimensionMismatchError(
                    f"Gemini model '{self.model}' returned a {len(vector)}-dimensional "
                    f"vector, but EMBEDDING_DIMENSIONS is configured as {self.dimensions}. "
                    "gemini-embedding-001 supports configurable output width via the "
                    "`dimensions` request field - check EMBEDDING_DIMENSIONS matches what "
                    "was actually requested."
                )

        # gemini-embedding-001 does not auto-normalize a truncated output
        # (unlike gemini-embedding-2, deliberately not used here - see this
        # module's docstring) - Google's docs call manual L2 normalization
        # a requirement at non-native dimensions.
        return [_l2_normalize(vector) for vector in vectors]
