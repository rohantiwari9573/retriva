from app.rag.embedding.testing import DeterministicTestEmbeddingProvider


async def test_same_text_produces_same_vector():
    provider = DeterministicTestEmbeddingProvider(dimensions=16)
    v1 = await provider.embed_query("hello world")
    v2 = await provider.embed_query("hello world")
    assert v1 == v2


async def test_different_text_produces_different_vector():
    provider = DeterministicTestEmbeddingProvider(dimensions=16)
    v1 = await provider.embed_query("hello world")
    v2 = await provider.embed_query("goodbye world")
    assert v1 != v2


async def test_vector_has_configured_dimensions():
    provider = DeterministicTestEmbeddingProvider(dimensions=768)
    vector = await provider.embed_query("some text")
    assert len(vector) == 768


async def test_values_are_bounded_in_expected_range():
    provider = DeterministicTestEmbeddingProvider(dimensions=32)
    vector = await provider.embed_query("bounded")
    assert all(-1.0 <= v <= 1.0 for v in vector)


async def test_embed_documents_matches_embed_query_per_text():
    provider = DeterministicTestEmbeddingProvider(dimensions=16)
    batch = await provider.embed_documents(["a", "b"])
    single_a = await provider.embed_query("a")
    single_b = await provider.embed_query("b")
    assert batch == [single_a, single_b]
