"""PDF parsing via pypdf, one element per page (preserves page numbers for
future citations - see docs/document-ingestion.md)."""

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.core.config import settings
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

        # The MAX_DOCUMENT_SIZE_MB byte cap only indirectly bounds page
        # count - a small PDF can still legitimately (or maliciously) contain
        # tens of thousands of near-empty pages, each cheap on disk but
        # costing a full extract_text() call. Check before extracting any
        # text so a pathological page count fails fast.
        page_count = len(reader.pages)
        if page_count > settings.MAX_DOCUMENT_PAGES:
            raise ParsingError(
                f"PDF has {page_count} pages, exceeding the limit of "
                f"{settings.MAX_DOCUMENT_PAGES}."
            )

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
