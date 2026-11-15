"""Plain-text parsing. The whole file is one element - there's no structure
to preserve beyond what normalization/chunking will impose.

Decoded in 1 MiB blocks against the document's text budget, so a 25 MB file is
rejected after reading just past MAX_DOCUMENT_TEXT_LENGTH characters instead of
after decoding all of it."""

import io
from typing import BinaryIO

from app.ingestion.parsers.base import ParsedDocument, ParsedElement, TextBudget, iter_utf8_text


class TXTParser:
    def parse(self, data: bytes) -> ParsedDocument:
        return self.parse_stream(io.BytesIO(data))

    def parse_file(self, path: str) -> ParsedDocument:
        with open(path, "rb") as file:
            return self.parse_stream(file)

    def parse_stream(self, file: BinaryIO) -> ParsedDocument:
        text = "".join(iter_utf8_text(file, TextBudget()))
        return ParsedDocument(elements=[ParsedElement(text=text)] if text.strip() else [])
