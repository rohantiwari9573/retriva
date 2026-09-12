"""PDF parsing via pypdf, one element per page (preserves page numbers for
future citations - see docs/document-ingestion.md)."""

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.ingestion.errors import ParsingError
from app.ingestion.parsers.base import ParsedDocument, ParsedElement


class PDFParser:
    def parse(self, data: bytes) -> ParsedDocument:
        try:
            reader = PdfReader(io.BytesIO(data))
        except (PdfReadError, ValueError) as exc:
            raise ParsingError(f"Could not read PDF: {exc}") from exc

        if reader.is_encrypted:
            raise ParsingError("PDF is password-protected and cannot be parsed.")

        elements: list[ParsedElement] = []
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # pypdf can raise a variety of internal errors
                raise ParsingError(
                    f"Could not extract text from PDF page {page_number}: {exc}"
                ) from exc
            if text.strip():
                elements.append(ParsedElement(text=text, page_number=page_number))

        return ParsedDocument(elements=elements)
