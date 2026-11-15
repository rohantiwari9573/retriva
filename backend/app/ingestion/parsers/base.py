"""Parser interface and the shared result shape every parser produces.

An "element" is the smallest unit a parser can attach metadata to - a PDF
page, a paragraph between DOCX headings, a Markdown section. The chunker
consumes a flat list of these rather than a single text blob so it can carry
page/section metadata through into chunks without re-deriving it later
(which is only possible for PDF page numbers - DOCX/Markdown structure is
lost once flattened to plain text).
"""

import codecs
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import BinaryIO, Protocol

from app.core.config import settings
from app.ingestion.errors import ParsingError, TextLimitExceededError

# Read size for streamed text decoding.
TEXT_BLOCK_BYTES = 1024 * 1024


@dataclass
class ParsedElement:
    text: str
    page_number: int | None = None
    section: str | None = None


@dataclass
class ParsedDocument:
    elements: list[ParsedElement] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n\n".join(e.text for e in self.elements if e.text)


class DocumentParser(Protocol):
    def parse(self, data: bytes) -> ParsedDocument: ...

    def parse_file(self, path: str) -> ParsedDocument: ...


class TextBudget:
    """Running count of extracted characters for one document. Every parser
    charges what it extracts as it goes and stops as soon as the document passes
    MAX_DOCUMENT_TEXT_LENGTH, instead of extracting everything first and checking
    afterwards (which held e.g. 89 million characters of a 390 KB DOCX in memory
    before rejecting it)."""

    def __init__(self, limit: int | None = None) -> None:
        self.limit = settings.MAX_DOCUMENT_TEXT_LENGTH if limit is None else limit
        self.used = 0

    def charge(self, characters: int) -> None:
        self.used += characters
        if self.used > self.limit:
            raise TextLimitExceededError(
                f"Document contains more than {self.limit} characters of extracted "
                "text, exceeding the processing limit."
            )


def iter_utf8_text(file: BinaryIO, budget: TextBudget) -> Iterator[str]:
    """Decodes a UTF-8 file in 1 MiB blocks, charging each decoded block to the
    budget before yielding it, so at most one block beyond the limit is decoded."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        while True:
            block = file.read(TEXT_BLOCK_BYTES)
            if not block:
                tail = decoder.decode(b"", final=True)
                if tail:
                    budget.charge(len(tail))
                    yield tail
                return
            text = decoder.decode(block)
            budget.charge(len(text))
            yield text
    except UnicodeDecodeError as exc:
        raise ParsingError(f"File is not valid UTF-8 text: {exc}") from exc
