"""DOCX parsing, streamed.

One element per body paragraph, tagged with the text of the nearest preceding
Heading/Title-style paragraph (best-effort "section" metadata - DOCX has no
page concept at the file-format level, since pagination is computed by the
renderer, not stored in the document).

word/document.xml is read with lxml.iterparse and every finished body-level
element is freed as soon as it has been read, so memory stays flat however large
the XML is. python-docx built the whole XML tree first: a 390 KB DOCX holding
95 MB of XML took the API to ~430 MiB before anything was checked. The text
rules below reproduce python-docx 1.2's Document.paragraphs/Paragraph.text and
style-name lookup exactly (tests/unit/test_parsers.py compares the two).
"""

import io
import zipfile
from typing import IO, BinaryIO

from lxml import etree

from app.core.config import settings
from app.ingestion.errors import ParsingError, TextLimitExceededError
from app.ingestion.parsers.base import ParsedDocument, ParsedElement, TextBudget

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_OFFICE_DOCUMENT_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)
_MAX_STYLES_XML_BYTES = 10 * 1024 * 1024


def _run_text(run: etree._Element) -> str:
    """python-docx CT_R.text: br/cr/noBreakHyphen/ptab/t/tab children, in order."""
    parts: list[str] = []
    for child in run:
        tag = child.tag
        if tag == f"{_W}t":
            parts.append(child.text or "")
        elif tag in (f"{_W}tab", f"{_W}ptab"):
            parts.append("\t")
        elif tag == f"{_W}cr":
            parts.append("\n")
        elif tag == f"{_W}br":
            # Only a text-wrapping break is a newline; page/column breaks are "".
            if child.get(f"{_W}type", "textWrapping") == "textWrapping":
                parts.append("\n")
        elif tag == f"{_W}noBreakHyphen":
            parts.append("-")
    return "".join(parts)


def _paragraph_text(paragraph: etree._Element) -> str:
    """python-docx CT_P.text: direct w:r and w:hyperlink children (a hyperlink's
    text is that of its own direct w:r children)."""
    parts: list[str] = []
    for child in paragraph:
        if child.tag == f"{_W}r":
            parts.append(_run_text(child))
        elif child.tag == f"{_W}hyperlink":
            parts.extend(_run_text(r) for r in child if r.tag == f"{_W}r")
    return "".join(parts)


def _paragraph_style_id(paragraph: etree._Element) -> str | None:
    ppr = paragraph.find(f"{_W}pPr")
    if ppr is None:
        return None
    pstyle = ppr.find(f"{_W}pStyle")
    return None if pstyle is None else pstyle.get(f"{_W}val")


def _paragraph_styles(archive: zipfile.ZipFile) -> tuple[dict[str, str], str]:
    """styleId -> name for paragraph styles, and the default paragraph style's name
    (what python-docx's Paragraph.style falls back to)."""
    names: dict[str, str] = {}
    default = ""
    try:
        info = archive.getinfo("word/styles.xml")
    except KeyError:
        return names, default
    if info.file_size > _MAX_STYLES_XML_BYTES:
        raise ParsingError("DOCX styles part is unreasonably large.")
    root = etree.fromstring(archive.read(info), parser=etree.XMLParser(resolve_entities=False))
    for style in root.iter(f"{_W}style"):
        if style.get(f"{_W}type") != "paragraph":
            continue
        name_el = style.find(f"{_W}name")
        name = name_el.get(f"{_W}val", "") if name_el is not None else ""
        style_id = style.get(f"{_W}styleId")
        if style_id is not None:
            names[style_id] = name
        if style.get(f"{_W}default") in ("1", "true", "on"):
            default = name
    return names, default


def _main_part_name(archive: zipfile.ZipFile) -> str:
    try:
        rels = etree.fromstring(
            archive.read("_rels/.rels"), parser=etree.XMLParser(resolve_entities=False)
        )
        for rel in rels.iter(f"{_REL_NS}Relationship"):
            if rel.get("Type") == _OFFICE_DOCUMENT_REL and rel.get("TargetMode") != "External":
                return rel.get("Target", "word/document.xml").lstrip("/")
    except (KeyError, etree.XMLSyntaxError):
        pass
    return "word/document.xml"


class DOCXParser:
    def parse(self, data: bytes) -> ParsedDocument:
        return self.parse_stream(io.BytesIO(data))

    def parse_file(self, path: str) -> ParsedDocument:
        with open(path, "rb") as file:
            return self.parse_stream(file)

    def parse_stream(self, file: BinaryIO) -> ParsedDocument:
        try:
            with zipfile.ZipFile(file) as archive:
                # DOCX is a ZIP archive: refuse a zip bomb from the declared sizes
                # before decompressing anything.
                total_uncompressed = sum(info.file_size for info in archive.infolist())
                if total_uncompressed > settings.MAX_DOCX_UNCOMPRESSED_SIZE_BYTES:
                    raise ParsingError(
                        f"DOCX decompresses to {total_uncompressed} bytes, exceeding the "
                        f"limit of {settings.MAX_DOCX_UNCOMPRESSED_SIZE_BYTES}."
                    )
                styles, default_style = _paragraph_styles(archive)
                with archive.open(_main_part_name(archive)) as document_xml:
                    return self._parse_document_xml(document_xml, styles, default_style)
        except (zipfile.BadZipFile, KeyError, etree.XMLSyntaxError, ValueError) as exc:
            raise ParsingError(f"Could not read DOCX: {exc}") from exc

    def _parse_document_xml(
        self, document_xml: IO[bytes], styles: dict[str, str], default_style: str
    ) -> ParsedDocument:
        budget = TextBudget()
        elements: list[ParsedElement] = []
        current_section: str | None = None
        body: etree._Element | None = None

        events = etree.iterparse(
            document_xml, events=("start", "end"), resolve_entities=False, no_network=True
        )
        for event, element in events:
            if event == "start":
                if element.tag == f"{_W}body" and body is None:
                    body = element
                continue
            if body is None or element.getparent() is not body:
                continue
            # A finished direct child of w:body: read it if it's a paragraph, then
            # free it and everything before it.
            if element.tag == f"{_W}p":
                text = _paragraph_text(element).strip()
                if text:
                    style_id = _paragraph_style_id(element)
                    style_name = styles.get(style_id, default_style) if style_id else default_style
                    lowered = (style_name or "").lower()
                    if lowered.startswith("heading") or lowered == "title":
                        current_section = text
                    else:
                        budget.charge(len(text))
                        elements.append(ParsedElement(text=text, section=current_section))
            element.clear(keep_tail=False)
            while element.getprevious() is not None:
                del body[0]

        return ParsedDocument(elements=elements)


__all__ = ["DOCXParser", "TextLimitExceededError"]
