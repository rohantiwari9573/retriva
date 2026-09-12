"""EmbeddingProvider interface.

Kept to two methods - embed_documents (batch, for ingestion) and embed_query
(single, for retrieval in a later phase) - mirroring the shape every
embedding API (OpenAI-compatible, Cohere, local) actually exposes, so a new
provider is a new class, never an if/elif in calling code.
"""

from typing import Protocol


class EmbeddingDimensionMismatchError(Exception):
    """Raised when a provider returns vectors of a different width than
    settings.EMBEDDING_DIMENSIONS declares. This must never be silently
    truncated or padded - it means the configured model and the configured
    dimension have drifted apart, and any vector written to pgvector under
    the wrong assumption would silently corrupt retrieval later."""


class EmbeddingProviderUnavailableError(Exception):
    """The embedding backend could not be reached or returned an error.
    Treated as transient by callers (worth a Celery retry) unless the
    subclass says otherwise."""


class EmbeddingProvider(Protocol):
    """dimensions must be the actual width of vectors this instance returns,
    read from configuration - never hardcoded, since it varies by model."""

    dimensions: int
    model: str

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...
