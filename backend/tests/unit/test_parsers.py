import io

import pytest
from docx import Document as DocxDocument
from pypdf import PdfWriter

from app.core.config import settings
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


def _pdf_with_text(pages: list[str]) -> bytes:
    """A real PDF with one line of extractable text per page."""
    objs: list[bytes | None] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    kids = []
    for text in pages:
        content = f"BT /F1 12 Tf 20 150 Td ({text}) Tj ET".encode()
        objs.append(
            b"<< /Length "
            + str(len(content)).encode()
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        )
        cid = len(objs)
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents {cid} 0 R "
            "/Resources << /Font << /F1 3 0 R >> >> >>".encode()
        )
        kids.append(len(objs))
    refs = " ".join(f"{k} 0 R" for k in kids)
    objs[1] = f"<< /Type /Pages /Kids [{refs}] /Count {len(kids)} >>".encode()
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + (body or b"") + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


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


# ---- Streaming parsers: equivalence with python-docx, early text limits, memory


def _python_docx_reference(data: bytes):
    """The pre-streaming DOCXParser (python-docx), kept here as the oracle."""
    from docx import Document as DocxDocument

    doc = DocxDocument(io.BytesIO(data))
    out, section = [], None
    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style_name = (paragraph.style.name if paragraph.style else "") or ""
        if style_name.lower().startswith("heading") or style_name.lower() == "title":
            section = text
            continue
        out.append((text, section))
    return out


def _rich_docx() -> bytes:
    from docx import Document as DocxDocument
    from docx.enum.text import WD_BREAK
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    doc = DocxDocument()
    doc.add_heading("Report title", level=0)
    doc.add_paragraph("Intro before any heading.")
    doc.add_heading("Section A", level=1)
    p = doc.add_paragraph("Line one")
    p.add_run().add_break()
    p.add_run("line two\tafter tab")
    p.add_run().add_break(WD_BREAK.PAGE)
    p.add_run("after page break")
    p.add_run().add_break(WD_BREAK.COLUMN)
    hyphen = p.add_run("x")._r
    hyphen.append(OxmlElement("w:noBreakHyphen"))
    cr = p.add_run("y")._r
    cr.append(OxmlElement("w:cr"))
    # A hyperlink wrapping a run (python-docx counts its text).
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), "rId99")
    run = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "linked words"
    run.append(t)
    link.append(run)
    p._p.append(link)
    doc.add_paragraph("   ")  # whitespace only: skipped
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "table cell text is not a body paragraph"
    doc.add_heading("Section B", level=2)
    doc.add_paragraph("Quote style paragraph", style="Quote")
    doc.add_paragraph("List paragraph", style="List Bullet")
    for i in range(50):
        doc.add_paragraph(f"Body paragraph number {i} with some words.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_streaming_docx_parser_matches_python_docx_exactly():
    data = _rich_docx()
    expected = _python_docx_reference(data)
    got = [(e.text, e.section) for e in DOCXParser().parse(data).elements]

    assert got == expected
    assert ("linked words" in got[1][0]) and got[0] == ("Intro before any heading.", "Report title")


def test_docx_parse_file_matches_parse(tmp_path):
    data = _rich_docx()
    path = tmp_path / "doc.docx"
    path.write_bytes(data)
    assert DOCXParser().parse_file(str(path)) == DOCXParser().parse(data)


def _big_docx(paragraphs: int, words: int = 30) -> bytes:
    import zipfile

    para = "<w:p><w:r><w:t>" + " ".join(["lorem"] * words) + "</w:t></w:r></w:p>"
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + para * paragraphs + "</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


@pytest.mark.parametrize(
    ("parser", "data"),
    [
        (TXTParser(), b"0123456789" * 1000),
        (MarkdownParser(), b"a\n" * 5000),
        (DOCXParser(), None),  # filled below
    ],
)
def test_text_limit_stops_parsing_early(monkeypatch, parser, data):
    from app.ingestion.errors import TextLimitExceededError

    monkeypatch.setattr(settings, "MAX_DOCUMENT_TEXT_LENGTH", 1000)
    data = data if data is not None else _big_docx(2000)
    with pytest.raises(TextLimitExceededError, match="more than 1000 characters"):
        parser.parse(data)


def test_pdf_text_limit_stops_parsing_early(monkeypatch):
    from app.ingestion.errors import TextLimitExceededError

    monkeypatch.setattr(settings, "MAX_DOCUMENT_TEXT_LENGTH", 20)
    with pytest.raises(TextLimitExceededError):
        PDFParser().parse(_pdf_with_text(["first page has plenty of words", "second"]))


def test_large_docx_xml_is_parsed_in_bounded_memory(monkeypatch):
    """~40 MB of XML: python-docx built the whole tree (hundreds of MB). The
    streaming parser frees each paragraph as it goes."""
    import tracemalloc

    monkeypatch.setattr(settings, "MAX_DOCUMENT_TEXT_LENGTH", 100_000_000)
    data = _big_docx(200_000)
    tracemalloc.start()
    try:
        result = DOCXParser().parse(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(result.elements) == 200_000
    # The elements themselves (~200k short strings) dominate; the XML tree must not.
    assert peak < 120 * 1024 * 1024, f"peak {peak / 1048576:.0f} MiB"


def test_markdown_one_character_lines_are_bounded(monkeypatch):
    """Millions of tiny lines used to become one list of str objects before the
    length check (~420 MiB for 25 MB). Elements are now capped in size."""
    import tracemalloc

    monkeypatch.setattr(settings, "MAX_DOCUMENT_TEXT_LENGTH", 5_000_000)
    data = b"a\n" * 2_000_000  # 4M characters, under the limit
    tracemalloc.start()
    try:
        result = MarkdownParser().parse(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert sum(len(e.text) for e in result.elements) == 2_000_000 * 2 - 1 - (
        len(result.elements) - 1
    )
    assert peak < 60 * 1024 * 1024, f"peak {peak / 1048576:.0f} MiB"


@pytest.mark.parametrize(
    "text",
    ["", "one line", "a\nb\r\nc\rd", "x\u2028y\x0cz\x85w", "# H1\nbody\n## H2\r\nmore\r\n"],
)
def test_markdown_streaming_matches_whole_text_parsing(text):
    expected = MarkdownParser().parse(text.encode())
    got = MarkdownParser().parse_stream(io.BytesIO(text.encode()))
    assert got == expected
