import uuid

from app.rag.context_builder import build_context
from app.rag.retrieval.types import RetrievedChunk


def _chunk(content: str = "some content", token_count: int = 10, **kwargs) -> RetrievedChunk:
    defaults = dict(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_name="Handbook.pdf",
        content=content,
        page_number=None,
        section=None,
        token_count=token_count,
        vector_score=0.9,
        keyword_score=None,
        fused_score=0.5,
    )
    defaults.update(kwargs)
    return RetrievedChunk(**defaults)


def test_assigns_sequential_source_ids_in_rank_order():
    chunks = [_chunk("first"), _chunk("second"), _chunk("third")]
    context = build_context(chunks, max_chunks=10, max_tokens=10_000)
    assert [b.source_id for b in context.blocks] == ["SOURCE-1", "SOURCE-2", "SOURCE-3"]
    assert [b.chunk.content for b in context.blocks] == ["first", "second", "third"]


def test_respects_max_chunks():
    chunks = [_chunk(f"chunk {i}") for i in range(10)]
    context = build_context(chunks, max_chunks=3, max_tokens=10_000)
    assert len(context.blocks) == 3


def test_respects_max_tokens_budget():
    chunks = [_chunk(f"chunk {i}", token_count=100) for i in range(10)]
    context = build_context(chunks, max_chunks=10, max_tokens=250)
    # 100 + 100 = 200 <= 250, +100 = 300 > 250 -> stop at 2
    assert len(context.blocks) == 2


def test_always_includes_at_least_one_chunk_even_if_it_exceeds_budget():
    chunks = [_chunk("huge chunk", token_count=99_999)]
    context = build_context(chunks, max_chunks=10, max_tokens=10)
    assert len(context.blocks) == 1


def test_deduplicates_by_chunk_id():
    shared_id = uuid.uuid4()
    chunks = [_chunk("dup", chunk_id=shared_id), _chunk("dup again", chunk_id=shared_id)]
    context = build_context(chunks, max_chunks=10, max_tokens=10_000)
    assert len(context.blocks) == 1


def test_formats_page_and_section_metadata_when_present():
    chunk = _chunk("leave policy text", page_number=14, section="Leave Policy")
    context = build_context([chunk], max_chunks=10, max_tokens=10_000)
    assert "Page 14" in context.text
    assert "Section: Leave Policy" in context.text
    assert "Handbook.pdf" in context.text


def test_omits_page_and_section_when_absent():
    chunk = _chunk("no metadata", page_number=None, section=None)
    context = build_context([chunk], max_chunks=10, max_tokens=10_000)
    assert "Page" not in context.text
    assert "Section" not in context.text


def test_empty_input_produces_empty_context():
    context = build_context([], max_chunks=10, max_tokens=10_000)
    assert context.blocks == []
    assert context.text == ""


def test_source_ids_and_block_for_lookup():
    chunks = [_chunk("a"), _chunk("b")]
    context = build_context(chunks, max_chunks=10, max_tokens=10_000)
    assert context.source_ids() == {"SOURCE-1", "SOURCE-2"}
    assert context.block_for("SOURCE-1").chunk.content == "a"
    assert context.block_for("SOURCE-99") is None


def test_deterministic_output_for_same_input():
    chunks = [_chunk("a"), _chunk("b"), _chunk("c")]
    first = build_context(chunks, max_chunks=10, max_tokens=10_000)
    second = build_context(chunks, max_chunks=10, max_tokens=10_000)
    assert first.text == second.text
