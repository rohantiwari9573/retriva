import pytest

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


def _run_with_timeout(doc: ParsedDocument, seconds: float = 5.0, **kwargs):
    """chunk_document used to loop forever on some inputs; never hang the suite."""
    import threading

    result: dict = {}
    thread = threading.Thread(
        target=lambda: result.setdefault("chunks", chunk_document(doc, **kwargs)), daemon=True
    )
    thread.start()
    thread.join(seconds)
    assert "chunks" in result, "chunk_document did not terminate"
    return result["chunks"]


def test_short_paragraph_before_one_that_cannot_share_a_chunk_terminates():
    """Regression: a <= overlap-sized paragraph followed by a paragraph too big to
    fit beside it made chunk_document emit overlap-only chunks forever (duplicate
    chunks until the process ran out of memory; a 3.8 MB PDF OOM-killed the API)."""
    doc = _doc(ParsedElement(text="Short intro line here.\n\n" + ("word " * 396).strip()))
    chunks = _run_with_timeout(doc, chunk_size_tokens=500, chunk_overlap_tokens=50)

    assert [c.content for c in chunks] == ["Short intro line here.", ("word " * 396).strip()]


def test_every_chunk_adds_new_text_and_chunking_always_terminates():
    import random

    rng = random.Random(1234)
    for _ in range(300):
        paragraphs = []
        for _ in range(rng.randint(1, 25)):
            words = rng.choice([1, 3, 8, 40, 120, 300, 390, 420, 499, 650])
            paragraphs.append(" ".join(f"w{rng.randint(0, 9999)}" for _ in range(words)))
        doc = _doc(ParsedElement(text="\n\n".join(paragraphs), page_number=1))
        chunks = _run_with_timeout(doc, chunk_size_tokens=500, chunk_overlap_tokens=50)

        assert chunks
        # No chunk is a copy of the previous one's tail: each contributes new text.
        for previous, chunk in zip(chunks, chunks[1:], strict=False):
            assert not previous.content.endswith(chunk.content)
        # Bounded output: overlap can't make the chunk count explode.
        total_tokens = sum(estimate_token_count(p) for p in paragraphs)
        assert len(chunks) <= 2 * (total_tokens // 450 + len(paragraphs)) + 1


def test_stale_ingestion_workdirs_are_removed_but_active_and_young_ones_kept(tmp_path, monkeypatch):
    import os
    import tempfile
    import time

    from app.ingestion import pipeline

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    stale = tmp_path / f"{pipeline.WORKDIR_PREFIX}stale"
    young = tmp_path / f"{pipeline.WORKDIR_PREFIX}young"
    active = tmp_path / f"{pipeline.WORKDIR_PREFIX}active"
    unrelated = tmp_path / "something-else"
    for d in (stale, young, active, unrelated):
        d.mkdir()
        (d / "source").write_bytes(b"x")
    old = time.time() - 2 * pipeline.STALE_WORKDIR_SECONDS
    for d in (stale, active, unrelated):
        os.utime(d, (old, old))
    monkeypatch.setattr(pipeline, "_active_workdirs", {str(active)})

    assert pipeline.cleanup_stale_workdirs() == 1
    assert not stale.exists()
    assert young.exists() and active.exists() and unrelated.exists()


def test_a_sentence_larger_than_the_chunk_size_is_window_split():
    """Reviewer M3: a multi-sentence paragraph whose one sentence was 20 MB became
    one 20 MB chunk (and a ~60 MB JSON line, embedding request and row)."""
    from app.ingestion.parsers.base import ParsedDocument, ParsedElement

    text = "Start. " + "x" * 50_000 + " end."
    chunks = chunk_document(
        ParsedDocument(elements=[ParsedElement(text=text)]),
        chunk_size_tokens=500,
        chunk_overlap_tokens=50,
    )
    assert max(c.char_count for c in chunks) <= 500 * 4 + 10
    assert "".join(c.content for c in chunks).replace("\n\n", "").count("x") >= 50_000


def _check_invariants(chunks, document, size):
    # Never above the target size (every unit is split to fit), never empty, and
    # no text lost: every word of every paragraph appears in some chunk.
    assert all(0 < c.token_count <= size for c in chunks)
    # A word longer than the window (size * 4 characters, the token estimate) is
    # window-split, so check it piece by piece; the last piece can straddle the
    # next word, so only full-window pieces are checked for those.
    joined = "\n\n".join(c.content for c in chunks)
    window = size * 4
    for element in document.elements:
        for word in element.text.split():
            if len(word) <= window:
                assert word in joined
            else:
                assert all(
                    word[i : i + window] in joined for i in range(0, len(word) - window + 1, window)
                )
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


@pytest.mark.parametrize(
    ("name", "paragraphs", "size", "overlap"),
    [
        ("exactly the limit", ["a" * 400, "b" * 400], 100, 20),
        ("one char above the limit", ["a" * 401, "b" * 10], 100, 20),
        ("overlap larger than the next paragraph", ["x" * 60, "y" * 360, "z" * 8], 100, 90),
        ("overlap equal to the size", ["p" * 40, "q" * 400, "r" * 40], 100, 100),
        ("empty and blank paragraphs", ["", "   ", "real text here.", "\n\n", "more."], 50, 10),
        ("multi-byte text", ["日本語のテキスト。" * 60, "😀 émoji ünïcode. " * 40], 50, 10),
        ("many tiny paragraphs", ["w"] * 5000, 10, 3),
    ],
)
def test_chunking_edge_cases_terminate_and_keep_their_invariants(name, paragraphs, size, overlap):
    document = ParsedDocument(elements=[ParsedElement(text="\n\n".join(paragraphs))])
    chunks = chunk_document(document, chunk_size_tokens=size, chunk_overlap_tokens=overlap)
    _check_invariants(chunks, document, size)
    # Progress: every chunk carries at least one paragraph-unit of new text, so a
    # document can never produce more chunks than it has units.
    units = sum(1 for p in "\n\n".join(paragraphs).split("\n\n") if p.strip())
    assert len(chunks) <= max(units, 1) * (1 + 4 * max(len(p) for p in paragraphs) // size + 1)


def test_the_chunk_limit_stops_early_with_a_clear_error():
    from app.ingestion.errors import PermanentProcessingError

    document = ParsedDocument(elements=[ParsedElement(text="\n\n".join(["word"] * 1000))])
    with pytest.raises(PermanentProcessingError, match="more than 10 chunks"):
        chunk_document(document, chunk_size_tokens=1, chunk_overlap_tokens=0, max_chunks=10)
    exact = chunk_document(document, chunk_size_tokens=1, chunk_overlap_tokens=0, max_chunks=1000)
    assert len(exact) == 1000


def test_a_section_longer_than_the_column_is_truncated():
    """document_chunks.section is VARCHAR(500): a longer heading path used to
    fail the whole document at insert time."""
    from app.ingestion.chunking import MAX_SECTION_CHARS

    heading = "H" * 2000
    document = ParsedDocument(elements=[ParsedElement(text="Body text.", section=heading)])
    chunks = chunk_document(document, chunk_size_tokens=100, chunk_overlap_tokens=10)
    assert chunks[0].section == heading[:MAX_SECTION_CHARS]
