"""Retriever roles in the hybrid pipeline.

VectorRetriever and KeywordRetriever intentionally do NOT share one exact
method signature - vector search needs a precomputed query embedding
(HybridRetriever owns calling EmbeddingProvider.embed_query once and handing
the vector to VectorRetriever), keyword search needs the raw query text.
Both return list[RankedHit] (see types.py), which is the actual interface
boundary HybridRetriever and fusion.py depend on - a retriever is
interchangeable with any other implementation that returns the same shape,
not one that takes the same input.

Every concrete search method here takes organization_id and enforces it in
the SQL WHERE clause (joined through `documents`, since document_chunks
carries no organization_id of its own - see DocumentChunk's docstring).
There is no code path in this package that queries document_chunks without
that join. This is the mandatory tenant-isolation boundary for retrieval.
"""
