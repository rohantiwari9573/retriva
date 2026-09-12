"""Structure-aware, deterministic chunking.

Not naive fixed-N-character splitting: the unit of work is the paragraph
(text between blank lines within a parser-produced element), not a raw
character window, so a chunk boundary never lands mid-sentence unless a
single paragraph itself exceeds the target size. Paragraphs carry their
source element's page_number/section, which becomes the chunk's metadata
(the first paragraph's, since a chunk always starts a new paragraph run).

Token counting is an approximation (~4 characters/token, a widely-used rule
of thumb for English prose) rather than a real BPE tokenizer such as
tiktoken. That's a deliberate trade-off: tiktoken's encodings are fetched
over the network on first use and cached, which would make ingestion depend
on outbound internet access purely to size chunks - a project whose whole
point is running at zero cost and fully offline (LM Studio, no cloud APIs)
shouldn't gain a hidden network dependency for something CHUNK_SIZE_TOKENS
already only needs approximately. token_count is stored per chunk so a more
exact tokenizer could be swapped in later without a schema change.

Determinism: chunk_document is a pure function of (parsed document content,
chunk_size_tokens, chunk_overlap_tokens) - no randomness, no wall-clock
input - so re-running it (e.g. on a Celery retry) always reproduces the same
chunk boundaries and content_hash values.

Overlap is implemented at paragraph granularity: after closing a chunk, the
trailing paragraphs whose combined token count fits within
chunk_overlap_tokens are carried over as the start of the next chunk. This
avoids the alternative (slicing the last N characters/tokens off raw text),
which can cut a sentence in half and hand the embedding model a fragment
that doesn't mean anything on its own.
"""

import hashlib
import re
from dataclasses import dataclass

from app.ingestion.normalization import normalize_text
from app.ingestion.parsers.base import ParsedDocument

_CHARS_PER_TOKEN = 4
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def estimate_token_count(text: str) -> int:
    return max(1, -(-len(text) // _CHARS_PER_TOKEN))  # ceil division


@dataclass
class Chunk:
    chunk_index: int
    content: str
    page_number: int | None
    section: str | None
    char_count: int
    token_count: int
    content_hash: str


@dataclass
class _Unit:
    text: str
    page_number: int | None
    section: str | None
    token_count: int


def _paragraphs(document: ParsedDocument) -> list[_Unit]:
    units: list[_Unit] = []
    for element in document.elements:
        normalized = normalize_text(element.text)
        if not normalized:
            continue
        for para in normalized.split("\n\n"):
            para = para.strip()
            if not para:
                continue
            units.append(
                _Unit(
                    text=para,
                    page_number=element.page_number,
                    section=element.section,
                    token_count=estimate_token_count(para),
                )
            )
    return units


def _split_oversized(unit: _Unit, max_tokens: int) -> list[_Unit]:
    """A single paragraph larger than the whole target chunk size - split on
    sentence boundaries first (keeps sentences intact); if it's one giant
    sentence with no punctuation, fall back to a fixed character window
    sized to approximately max_tokens."""
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(unit.text) if s]
    if len(sentences) <= 1:
        window_chars = max_tokens * _CHARS_PER_TOKEN
        return [
            _Unit(
                text=unit.text[i : i + window_chars],
                page_number=unit.page_number,
                section=unit.section,
                token_count=estimate_token_count(unit.text[i : i + window_chars]),
            )
            for i in range(0, len(unit.text), window_chars)
        ]

    result: list[_Unit] = []
    current: list[str] = []
    current_tokens = 0
    for sentence in sentences:
        tokens = estimate_token_count(sentence)
        if current and current_tokens + tokens > max_tokens:
            text = " ".join(current)
            result.append(_Unit(text, unit.page_number, unit.section, current_tokens))
            current, current_tokens = [], 0
        current.append(sentence)
        current_tokens += tokens
    if current:
        text = " ".join(current)
        result.append(_Unit(text, unit.page_number, unit.section, current_tokens))
    return result


def chunk_document(
    document: ParsedDocument,
    *,
    chunk_size_tokens: int,
    chunk_overlap_tokens: int,
) -> list[Chunk]:
    units: list[_Unit] = []
    for paragraph in _paragraphs(document):
        if paragraph.token_count > chunk_size_tokens:
            units.extend(_split_oversized(paragraph, chunk_size_tokens))
        else:
            units.append(paragraph)

    chunks: list[Chunk] = []
    current: list[_Unit] = []
    current_tokens = 0

    def flush() -> None:
        if not current:
            return
        content = "\n\n".join(u.text for u in current)
        first = current[0]
        chunks.append(
            Chunk(
                chunk_index=len(chunks),
                content=content,
                page_number=first.page_number,
                section=first.section,
                char_count=len(content),
                token_count=sum(u.token_count for u in current),
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        )

    index = 0
    while index < len(units):
        unit = units[index]
        if current and current_tokens + unit.token_count > chunk_size_tokens:
            flush()
            overlap: list[_Unit] = []
            overlap_tokens = 0
            for prev in reversed(current):
                if overlap_tokens + prev.token_count > chunk_overlap_tokens:
                    break
                overlap.insert(0, prev)
                overlap_tokens += prev.token_count
            current, current_tokens = overlap, overlap_tokens
            continue
        current.append(unit)
        current_tokens += unit.token_count
        index += 1
    flush()

    return chunks
