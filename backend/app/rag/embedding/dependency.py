"""Embedding provider factory.

Plain function, not a cached singleton: unlike the boto3-backed
StorageProvider, LMStudioEmbeddingProvider holds no event-loop-affine state
(each call opens its own httpx.AsyncClient), so there's nothing to gain from
caching and no correctness reason to avoid it either way. Kept as a function
so both FastAPI (via Depends) and Celery tasks (direct call, no DI container)
use the same construction path.
"""

from app.core.config import settings
from app.rag.embedding.base import EmbeddingProvider
from app.rag.embedding.gemini import GeminiEmbeddingProvider
from app.rag.embedding.lmstudio import LMStudioEmbeddingProvider


def get_embedding_provider() -> EmbeddingProvider:
    if settings.EMBEDDING_PROVIDER == "openai_compatible":
        return LMStudioEmbeddingProvider()
    if settings.EMBEDDING_PROVIDER == "gemini":
        return GeminiEmbeddingProvider()
    raise ValueError(f"Unknown EMBEDDING_PROVIDER: {settings.EMBEDDING_PROVIDER}")
