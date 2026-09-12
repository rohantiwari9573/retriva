"""Plain-text parsing. The whole file is one element - there's no structure
to preserve beyond what normalization/chunking will impose."""

from app.ingestion.errors import ParsingError
from app.ingestion.parsers.base import ParsedDocument, ParsedElement


class TXTParser:
    def parse(self, data: bytes) -> ParsedDocument:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ParsingError(f"File is not valid UTF-8 text: {exc}") from exc

        return ParsedDocument(elements=[ParsedElement(text=text)] if text.strip() else [])
