from app.ingestion.chunking import chunk_document, estimate_token_count
from app.ingestion.parsers.base import ParsedDocument, ParsedElement


def _doc(*elements: ParsedElement) -> ParsedDocument:
    return ParsedDocument(elements=list(elements))


def test_small_document_produces_single_chunk():
    doc = _doc(ParsedElement(text="A short paragraph.", page_number=1))
    chunks = chunk_document(doc, chunk_size_tokens=500, chunk_overlap_tokens=50)
    assert len(chunks) == 1
    assert chunks[0].chunk_index == 0
    assert chunks[0].page_number == 1
    assert chunks[0].content == "A short paragraph."


def test_chunking_is_deterministic():
    doc = _doc(
        ParsedElement(text="Paragraph one.\n\nParagraph two.\n\nParagraph three.", page_number=1)
    )
    first = chunk_document(doc, chunk_size_tokens=5, chunk_overlap_tokens=1)
    second = chunk_document(doc, chunk_size_tokens=5, chunk_overlap_tokens=1)
    assert [c.content for c in first] == [c.content for c in second]
    assert [c.content_hash for c in first] == [c.content_hash for c in second]


def test_large_document_splits_into_multiple_chunks_respecting_size():
    paragraphs = "\n\n".join(
        f"Paragraph number {i} with some extra words in it." for i in range(30)
    )
    doc = _doc(ParsedElement(text=paragraphs, page_number=2))
    chunks = chunk_document(doc, chunk_size_tokens=20, chunk_overlap_tokens=5)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.token_count <= 20 + 5  # oversized-paragraph splitting can slightly exceed
        assert chunk.page_number == 2


def test_chunk_indices_are_sequential():
    paragraphs = "\n\n".join(f"Paragraph {i}." for i in range(10))
    doc = _doc(ParsedElement(text=paragraphs))
    chunks = chunk_document(doc, chunk_size_tokens=5, chunk_overlap_tokens=1)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_overlap_carries_trailing_content_into_next_chunk():
    paragraphs = "\n\n".join(f"Paragraph {i} has some words." for i in range(10))
    doc = _doc(ParsedElement(text=paragraphs))
    no_overlap = chunk_document(doc, chunk_size_tokens=10, chunk_overlap_tokens=0)
    with_overlap = chunk_document(doc, chunk_size_tokens=10, chunk_overlap_tokens=5)
    # Overlap means later chunks repeat some content, so total characters
    # across all chunks should be >= the no-overlap total.
    assert sum(c.char_count for c in with_overlap) >= sum(c.char_count for c in no_overlap)


def test_oversized_single_paragraph_is_split_by_sentence():
    long_paragraph = " ".join(f"Sentence number {i} is reasonably long." for i in range(40))
    doc = _doc(ParsedElement(text=long_paragraph))
    chunks = chunk_document(doc, chunk_size_tokens=15, chunk_overlap_tokens=0)
    assert len(chunks) > 1


def test_metadata_preserved_per_chunk_from_first_paragraph():
    doc = _doc(
        ParsedElement(text="Intro text.", section="Introduction"),
        ParsedElement(text="Body text that is different.", section="Body"),
    )
    chunks = chunk_document(doc, chunk_size_tokens=2, chunk_overlap_tokens=0)
    sections = {c.section for c in chunks}
    assert "Introduction" in sections
    assert "Body" in sections


def test_content_hash_is_sha256_hex_of_content():
    import hashlib

    doc = _doc(ParsedElement(text="Deterministic content."))
    chunks = chunk_document(doc, chunk_size_tokens=500, chunk_overlap_tokens=50)
    expected = hashlib.sha256(chunks[0].content.encode("utf-8")).hexdigest()
    assert chunks[0].content_hash == expected


def test_empty_document_produces_no_chunks():
    doc = _doc()
    assert chunk_document(doc, chunk_size_tokens=500, chunk_overlap_tokens=50) == []


def test_estimate_token_count_is_positive_for_nonempty_text():
    assert estimate_token_count("hello world") > 0
    assert estimate_token_count("x") == 1
