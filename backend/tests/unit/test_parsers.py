import io

import pytest
from docx import Document as DocxDocument
from pypdf import PdfWriter

from app.ingestion.errors import ParsingError
from app.ingestion.parsers.docx import DOCXParser
from app.ingestion.parsers.markdown import MarkdownParser
from app.ingestion.parsers.pdf import PDFParser
from app.ingestion.parsers.registry import get_parser_for_mime_type
from app.ingestion.parsers.txt import TXTParser


def _make_pdf_bytes(pages: list[str]) -> bytes:
    writer = PdfWriter()
    for _ in pages:
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_docx_bytes(paragraphs: list[tuple[str, str | None]]) -> bytes:
    doc = DocxDocument()
    for text, style in paragraphs:
        doc.add_paragraph(text, style=style)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class TestPDFParser:
    def test_parses_blank_pages_without_error(self):
        data = _make_pdf_bytes(["", ""])
        result = PDFParser().parse(data)
        # Blank pages extract no text, so elements should be empty - not an error.
        assert result.elements == []

    def test_malformed_pdf_raises_parsing_error(self):
        with pytest.raises(ParsingError):
            PDFParser().parse(b"not a real pdf")

    def test_encrypted_pdf_raises_parsing_error(self):
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.encrypt("secret-password")
        buf = io.BytesIO()
        writer.write(buf)
        with pytest.raises(ParsingError):
            PDFParser().parse(buf.getvalue())

    def test_page_count_over_limit_rejected(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "MAX_DOCUMENT_PAGES", 2)
        data = _make_pdf_bytes(["", "", ""])
        with pytest.raises(ParsingError):
            PDFParser().parse(data)

    def test_page_count_at_limit_accepted(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "MAX_DOCUMENT_PAGES", 3)
        data = _make_pdf_bytes(["", "", ""])
        result = PDFParser().parse(data)
        assert result.elements == []


class TestDOCXParser:
    def test_paragraphs_become_elements(self):
        data = _make_docx_bytes([("Hello world.", None), ("Second paragraph.", None)])
        result = DOCXParser().parse(data)
        assert [e.text for e in result.elements] == ["Hello world.", "Second paragraph."]

    def test_heading_style_tags_following_paragraphs_with_section(self):
        data = _make_docx_bytes(
            [
                ("Chapter One", "Heading 1"),
                ("Body text under chapter one.", None),
                ("Chapter Two", "Heading 1"),
                ("Body text under chapter two.", None),
            ]
        )
        result = DOCXParser().parse(data)
        sections = {e.text: e.section for e in result.elements}
        assert sections["Body text under chapter one."] == "Chapter One"
        assert sections["Body text under chapter two."] == "Chapter Two"

    def test_malformed_docx_raises_parsing_error(self):
        with pytest.raises(ParsingError):
            DOCXParser().parse(b"not a real docx")

    def test_zip_bomb_over_uncompressed_limit_rejected(self, monkeypatch):
        from app.core.config import settings

        data = _make_docx_bytes([("Hello world.", None)])
        # The real document's total uncompressed size is trivially small;
        # setting the limit below it proves the guard actually fires without
        # needing to construct a real multi-gigabyte zip bomb in a unit test.
        monkeypatch.setattr(settings, "MAX_DOCX_UNCOMPRESSED_SIZE_BYTES", 10)
        with pytest.raises(ParsingError):
            DOCXParser().parse(data)

    def test_normal_docx_under_uncompressed_limit_accepted(self):
        data = _make_docx_bytes([("Hello world.", None)])
        result = DOCXParser().parse(data)
        assert [e.text for e in result.elements] == ["Hello world."]


class TestTXTParser:
    def test_parses_plain_text(self):
        result = TXTParser().parse(b"Hello, this is plain text.")
        assert len(result.elements) == 1
        assert result.elements[0].text == "Hello, this is plain text."

    def test_invalid_utf8_raises_parsing_error(self):
        with pytest.raises(ParsingError):
            TXTParser().parse(b"\xff\xfe\x00\x01invalid")

    def test_empty_file_produces_no_elements(self):
        result = TXTParser().parse(b"   \n\n  ")
        assert result.elements == []


class TestMarkdownParser:
    def test_heading_hierarchy_tracked_across_levels(self):
        md = (
            b"# Title\n\nIntro paragraph.\n\n"
            b"## Section A\n\nContent A.\n\n"
            b"### Subsection A.1\n\nDeep content.\n\n"
            b"## Section B\n\nContent B.\n"
        )
        result = MarkdownParser().parse(md)
        sections = {e.text: e.section for e in result.elements}
        assert sections["Intro paragraph."] == "Title"
        assert sections["Content A."] == "Title > Section A"
        assert sections["Deep content."] == "Title > Section A > Subsection A.1"
        assert sections["Content B."] == "Title > Section B"

    def test_document_with_no_headings_has_no_section(self):
        result = MarkdownParser().parse(b"Just a plain paragraph, no headings at all.")
        assert result.elements[0].section is None


def test_registry_returns_correct_parser_by_mime_type():
    assert isinstance(get_parser_for_mime_type("application/pdf"), PDFParser)
    assert isinstance(
        get_parser_for_mime_type(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        DOCXParser,
    )
    assert isinstance(get_parser_for_mime_type("text/plain"), TXTParser)
    assert isinstance(get_parser_for_mime_type("text/markdown"), MarkdownParser)


def test_registry_raises_for_unknown_mime_type():
    from app.ingestion.errors import PermanentProcessingError

    with pytest.raises(PermanentProcessingError):
        get_parser_for_mime_type("application/octet-stream")
