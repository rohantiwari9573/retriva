"""PDF parsing via pypdf, one element per page (preserves page numbers for
future citations - see docs/document-ingestion.md).

Memory: parse_file() gives pypdf the file path, so objects are read from disk on
demand instead of from an in-memory copy of the whole file. Each page's text is
charged to the document's text budget as it is extracted, so an oversized
document stops early. pypdf's per-stream decompression limits (75 MB by default)
are lowered to PDF_MAX_DECOMPRESSED_STREAM_BYTES for the parse: a 0.8 MB PDF whose content
stream inflated to tens of MB took the API to ~260 MiB before failing.
"""

import io
from typing import BinaryIO

from pypdf import PdfReader, apply_configuration
from pypdf.errors import PdfReadError

from app.core.config import settings
from app.ingestion.errors import ParsingError, TextLimitExceededError
from app.ingestion.parsers.base import ParsedDocument, ParsedElement, TextBudget


def _limits() -> dict[str, int]:
    """pypdf 7 resource limits, scoped to one parse via apply_configuration (a
    ContextVar, so concurrent parses in other threads are unaffected). Every value
    is pypdf's default (75 MB) lowered to PDF_MAX_DECOMPRESSED_STREAM_BYTES."""
    limit = settings.PDF_MAX_DECOMPRESSED_STREAM_BYTES
    return {
        "maximum_declared_stream_length": limit,
        "zlib_maximum_output_length": limit,
        "lzw_maximum_output_length": limit,
        "run_length_maximum_output_length": limit,
        "array_based_stream_maximum_output_length": limit,
        "image_maximum_buffer_size": limit,
        "stream_decoding_work_maximum_length": 4 * limit,
    }


class PDFParser:
    def parse(self, data: bytes) -> ParsedDocument:
        return self.parse_stream(io.BytesIO(data))

    def parse_file(self, path: str) -> ParsedDocument:
        with open(path, "rb") as file:
            return self.parse_stream(file)

    def parse_stream(self, file: BinaryIO) -> ParsedDocument:
        with apply_configuration(None, **_limits()):
            return self._parse(file)

    def _parse(self, file: BinaryIO) -> ParsedDocument:
        try:
            reader = PdfReader(file)
        except (PdfReadError, ValueError) as exc:
            raise ParsingError(f"Could not read PDF: {exc}") from exc

        if reader.is_encrypted:
            raise ParsingError("PDF is password-protected and cannot be parsed.")

        # The MAX_DOCUMENT_SIZE_MB byte cap only indirectly bounds page
        # count - a small PDF can still legitimately (or maliciously) contain
        # tens of thousands of near-empty pages, each cheap on disk but
        # costing a full extract_text() call. Check before extracting any
        # text so a pathological page count fails fast.
        try:
            page_count = len(reader.pages)
        except (PdfReadError, ValueError) as exc:
            raise ParsingError(f"Could not read PDF: {exc}") from exc
        if page_count > settings.MAX_DOCUMENT_PAGES:
            raise ParsingError(
                f"PDF has {page_count} pages, exceeding the limit of {settings.MAX_DOCUMENT_PAGES}."
            )

        budget = TextBudget()
        elements: list[ParsedElement] = []
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # pypdf can raise a variety of internal errors
                raise ParsingError(
                    f"Could not extract text from PDF page {page_number}: {exc}"
                ) from exc
            if text.strip():
                budget.charge(len(text))
                elements.append(ParsedElement(text=text, page_number=page_number))

        return ParsedDocument(elements=elements)


__all__ = ["PDFParser", "TextLimitExceededError"]
