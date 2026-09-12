"""Parser interface and the shared result shape every parser produces.

An "element" is the smallest unit a parser can attach metadata to - a PDF
page, a paragraph between DOCX headings, a Markdown section. The chunker
consumes a flat list of these rather than a single text blob so it can carry
page/section metadata through into chunks without re-deriving it later
(which is only possible for PDF page numbers - DOCX/Markdown structure is
lost once flattened to plain text).
"""

from dataclasses import dataclass, field
from typing import Protocol


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
