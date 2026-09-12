"""ContextBuilder: turns ranked, hydrated chunks into the exact text block
that goes into the LLM prompt, plus the source-id mapping the citation
validator needs afterward.

Responsibilities kept deliberately narrow:
- assign stable [SOURCE-N] identifiers, in rank order (rank 1 = SOURCE-1)
- enforce MAX_CONTEXT_CHUNKS and MAX_CONTEXT_TOKENS so a large retrieval
  never turns into an unbounded prompt
- deduplicate by chunk_id (defensive - HybridRetriever's fused list is
  already deduplicated by construction, but this stays a hard guarantee
  rather than an assumption borrowed from a caller)
- format deterministically, so the same chunks always produce the same
  context string (useful for tests and for debugging a bad answer)

Token counting reuses DocumentChunk.token_count (already computed at
chunking time - see app/ingestion/chunking.py) rather than re-estimating,
so context budgeting and stored chunk sizes never disagree.
"""

import uuid
from dataclasses import dataclass

from app.core.config import settings
from app.rag.retrieval.types import RetrievedChunk


@dataclass(frozen=True)
class ContextBlock:
    source_id: str  # "SOURCE-1", "SOURCE-2", ...
    chunk: RetrievedChunk


@dataclass(frozen=True)
class BuiltContext:
    blocks: list[ContextBlock]
    text: str

    def source_ids(self) -> set[str]:
        return {b.source_id for b in self.blocks}

    def block_for(self, source_id: str) -> ContextBlock | None:
        return next((b for b in self.blocks if b.source_id == source_id), None)


def build_context(
    chunks: list[RetrievedChunk],
    *,
    max_chunks: int | None = None,
    max_tokens: int | None = None,
) -> BuiltContext:
    max_chunks = max_chunks or settings.MAX_CONTEXT_CHUNKS
    max_tokens = max_tokens or settings.MAX_CONTEXT_TOKENS

    seen: set[uuid.UUID] = set()
    selected: list[RetrievedChunk] = []
    total_tokens = 0
    for chunk in chunks:
        if chunk.chunk_id in seen:
            continue
        if len(selected) >= max_chunks:
            break
        # Always include at least one chunk even if it alone exceeds the
        # token budget - an empty context is worse than a slightly
        # over-budget one, and MAX_CONTEXT_TOKENS is a soft guard against
        # unbounded growth, not a hard per-chunk cap.
        if selected and total_tokens + chunk.token_count > max_tokens:
            break
        seen.add(chunk.chunk_id)
        selected.append(chunk)
        total_tokens += chunk.token_count

    blocks = [ContextBlock(source_id=f"SOURCE-{i}", chunk=c) for i, c in enumerate(selected, 1)]
    text = "\n\n".join(_format_block(b) for b in blocks)
    return BuiltContext(blocks=blocks, text=text)


def _format_block(block: ContextBlock) -> str:
    chunk = block.chunk
    location_parts = [f"Document: {chunk.document_name}"]
    if chunk.page_number is not None:
        location_parts.append(f"Page {chunk.page_number}")
    if chunk.section:
        location_parts.append(f"Section: {chunk.section}")
    location = ", ".join(location_parts)
    return f"[{block.source_id}] ({location})\n{chunk.content}"
