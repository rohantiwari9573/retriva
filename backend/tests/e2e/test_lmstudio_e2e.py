"""Real LM Studio end-to-end test - the ONLY test in this suite that talks
to an actual embedding model. Skipped automatically if LM Studio isn't
reachable at EMBEDDING_BASE_URL, since CI and most local dev machines won't
have it running. This is deliberately separate from:

- tests/unit/test_embedding_lmstudio.py: mocks the HTTP layer, tests
  LMStudioEmbeddingProvider's own logic with no network at all.
- tests/integration/test_document_pipeline.py and
  test_document_processing_task.py: run the real pipeline/task end to end
  against DeterministicTestEmbeddingProvider (no live model needed).

Per the Phase 4 spec: do not claim LM Studio E2E verification unless this
test actually ran (not skipped) against a real, reachable LM Studio
instance - check the pytest output for "skipped" vs "passed" on this file
specifically before citing it as verification.
"""

import httpx
import pytest

from app.core.config import settings
from app.ingestion.chunking import chunk_document
from app.ingestion.parsers.txt import TXTParser
from app.rag.embedding.lmstudio import LMStudioEmbeddingProvider


def _lmstudio_reachable() -> bool:
    try:
        response = httpx.get(f"{settings.EMBEDDING_BASE_URL}/models", timeout=2.0)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    not _lmstudio_reachable(),
    reason=(
        f"LM Studio not reachable at {settings.EMBEDDING_BASE_URL} - "
        "start LM Studio's local server with an embedding model loaded to run this test."
    ),
)


async def test_real_lmstudio_embeds_a_document_end_to_end():
    provider = LMStudioEmbeddingProvider()
    document = TXTParser().parse(b"The quick brown fox jumps over the lazy dog.")
    chunks = chunk_document(document, chunk_size_tokens=500, chunk_overlap_tokens=50)
    assert len(chunks) == 1

    vectors = await provider.embed_documents([c.content for c in chunks])

    assert len(vectors) == 1
    assert len(vectors[0]) == settings.EMBEDDING_DIMENSIONS
    assert all(isinstance(v, float) for v in vectors[0])


async def test_real_lmstudio_embed_query_matches_dimensions():
    provider = LMStudioEmbeddingProvider()
    vector = await provider.embed_query("What is the leave policy?")
    assert len(vector) == settings.EMBEDDING_DIMENSIONS
