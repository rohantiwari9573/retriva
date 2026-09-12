"""Test-only embedding provider - never imported from application code.

Integration tests need the full pipeline (parse -> chunk -> embed -> persist)
to run in CI, where no LM Studio instance exists. Mocking the provider at the
unit level proves the pipeline *calls* an embedding provider correctly; it
doesn't prove the persisted vectors are consistent/usable. This class fills
that gap: deterministic (same text -> same vector, so tests can assert on
similarity/equality), the right width for whatever EMBEDDING_DIMENSIONS is
set to, and requires no network access at all.

It is deliberately NOT wired into app/rag/embedding/dependency.py's
production factory - only tests import it directly.
"""

import hashlib
import struct


class DeterministicTestEmbeddingProvider:
    def __init__(self, dimensions: int = 768) -> None:
        self.dimensions = dimensions
        self.model = "deterministic-test-provider"

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector_for(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector_for(text)

    def _vector_for(self, text: str) -> list[float]:
        # Expand a SHA-256 digest with a counter-based hash chain until we
        # have enough bytes for `dimensions` floats, then normalize each
        # 4-byte group into a small deterministic float in [-1, 1].
        needed_bytes = self.dimensions * 4
        blocks: list[bytes] = []
        counter = 0
        total = 0
        while total < needed_bytes:
            block = hashlib.sha256(f"{text}:{counter}".encode()).digest()
            blocks.append(block)
            total += len(block)
            counter += 1
        raw = b"".join(blocks)[:needed_bytes]

        values = struct.unpack(f"{self.dimensions}I", raw)
        return [(v / 0xFFFFFFFF) * 2 - 1 for v in values]
