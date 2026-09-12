import uuid

from app.rag.citations import validate_citations
from app.rag.context_builder import build_context
from app.rag.retrieval.types import RetrievedChunk


def _chunk(content: str = "content", **kwargs) -> RetrievedChunk:
    defaults = dict(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_name="Handbook.pdf",
        content=content,
        page_number=14,
        section="Leave Policy",
        token_count=10,
        vector_score=0.9,
        keyword_score=None,
        fused_score=0.5,
    )
    defaults.update(kwargs)
    return RetrievedChunk(**defaults)


def test_valid_citation_is_extracted_and_kept_in_answer():
    context = build_context([_chunk("Employees get 24 days of leave per year.")])
    answer = "You get 24 days of annual leave. [SOURCE-1]"

    result = validate_citations(answer, context)

    assert "[SOURCE-1]" in result.answer
    assert len(result.citations) == 1
    assert result.citations[0].id == "SOURCE-1"
    assert result.citations[0].document_name == "Handbook.pdf"
    assert result.citations[0].page == 14
    assert result.citations[0].section == "Leave Policy"


def test_fabricated_citation_is_stripped_from_answer_and_not_in_list():
    context = build_context([_chunk("only one source")])
    answer = "According to the policy. [SOURCE-1] Also see [SOURCE-99]."

    result = validate_citations(answer, context)

    assert "[SOURCE-99]" not in result.answer
    assert "[SOURCE-1]" in result.answer
    assert [c.id for c in result.citations] == ["SOURCE-1"]


def test_answer_with_only_fabricated_citations_has_no_valid_citations():
    context = build_context([_chunk("real content")])
    answer = "Here is the answer. [SOURCE-5] [SOURCE-6]"

    result = validate_citations(answer, context)

    assert result.citations == []
    assert "[SOURCE-5]" not in result.answer
    assert "[SOURCE-6]" not in result.answer


def test_empty_context_means_every_citation_is_fabricated():
    context = build_context([])
    answer = "The policy says X. [SOURCE-1]"

    result = validate_citations(answer, context)

    assert result.citations == []
    assert "[SOURCE-1]" not in result.answer


def test_duplicate_valid_citation_tags_produce_one_citation_object():
    context = build_context([_chunk("content")])
    answer = "First claim [SOURCE-1]. Second claim also [SOURCE-1]."

    result = validate_citations(answer, context)

    assert len(result.citations) == 1
    assert result.answer.count("[SOURCE-1]") == 2  # kept in the text both times


def test_no_citations_at_all_returns_answer_unchanged():
    context = build_context([_chunk("content")])
    answer = "I couldn't find enough information in your organization's documents to answer that."

    result = validate_citations(answer, context)

    assert result.answer == answer
    assert result.citations == []


def test_excerpt_is_truncated_for_long_chunks():
    long_content = "x" * 1000
    context = build_context([_chunk(long_content)])
    answer = "Answer. [SOURCE-1]"

    result = validate_citations(answer, context)

    assert len(result.citations[0].excerpt) < len(long_content)
    assert result.citations[0].excerpt.endswith("...")


def test_citation_ids_correspond_only_to_actually_retrieved_chunks():
    real_chunk = _chunk("real")
    context = build_context([real_chunk])
    answer = "Claim. [SOURCE-1]"

    result = validate_citations(answer, context)

    assert result.citations[0].chunk_id == str(real_chunk.chunk_id)
    assert result.citations[0].document_id == str(real_chunk.document_id)
