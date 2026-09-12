"""DOCX parsing via python-docx.

One element per paragraph, tagged with the text of the nearest preceding
Heading-style paragraph (best-effort "section" metadata - DOCX has no page
concept at the file-format level, since pagination is computed by the
renderer, not stored in the document)."""

import io
import zipfile

from docx import Document as DocxDocument
from docx.opc.exceptions import PackageNotFoundError

from app.ingestion.errors import ParsingError
from app.ingestion.parsers.base import ParsedDocument, ParsedElement


class DOCXParser:
    def parse(self, data: bytes) -> ParsedDocument:
        try:
            doc = DocxDocument(io.BytesIO(data))
        except (PackageNotFoundError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            raise ParsingError(f"Could not read DOCX: {exc}") from exc

        elements: list[ParsedElement] = []
        current_section: str | None = None

        for paragraph in doc.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            style_name = (paragraph.style.name if paragraph.style else "") or ""
            if style_name.lower().startswith("heading") or style_name.lower() == "title":
                current_section = text
                continue
            elements.append(ParsedElement(text=text, section=current_section))

        return ParsedDocument(elements=elements)
