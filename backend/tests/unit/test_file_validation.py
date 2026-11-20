import io
import zipfile

import pytest

from app.core.exceptions import UnsupportedFileTypeError
from app.services.file_validation import detect_and_validate_file_type


def _make_docx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document></w:document>")
    return buffer.getvalue()


def test_valid_pdf():
    result = detect_and_validate_file_type("report.pdf", b"%PDF-1.4\n...")
    assert result.extension == ".pdf"
    assert result.mime_type == "application/pdf"


def test_valid_docx():
    result = detect_and_validate_file_type("policy.docx", _make_docx_bytes())
    assert result.extension == ".docx"


def test_valid_txt():
    result = detect_and_validate_file_type("notes.txt", b"Hello, world.")
    assert result.extension == ".txt"


def test_valid_md():
    result = detect_and_validate_file_type("readme.md", b"# Heading")
    assert result.extension == ".md"


def test_rejects_unsupported_extension():
    with pytest.raises(UnsupportedFileTypeError):
        detect_and_validate_file_type("virus.exe", b"MZ\x90\x00")


def test_rejects_extension_content_mismatch_pdf_claim_but_zip_content():
    # A renamed .docx (or plain zip) claiming to be a PDF.
    with pytest.raises(UnsupportedFileTypeError):
        detect_and_validate_file_type("fake.pdf", _make_docx_bytes())


def test_rejects_zip_without_docx_marker_entry():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "just a plain zip")
    with pytest.raises(UnsupportedFileTypeError):
        detect_and_validate_file_type("notadocx.docx", buffer.getvalue())


def test_rejects_binary_content_claiming_to_be_text():
    binary_data = bytes(range(256))
    with pytest.raises(UnsupportedFileTypeError):
        detect_and_validate_file_type("data.txt", binary_data)


def test_extension_is_case_insensitive():
    result = detect_and_validate_file_type("REPORT.PDF", b"%PDF-1.4")
    assert result.extension == ".pdf"


def test_path_traversal_filename_still_validates_on_basename():
    # The extension check tolerates a traversal-shaped filename because the
    # storage key never incorporates the filename at all (see DocumentService)
    # - this only proves detection doesn't choke on hostile input, not that
    # path traversal is "allowed."
    result = detect_and_validate_file_type("../../etc/passwd.txt", b"hello")
    assert result.extension == ".txt"


def _central_directory_bomb(entries: int) -> bytes:
    """Final review's file: a zip that is almost all central directory (an entry
    per name, no data), plus word/document.xml so it looks like a DOCX."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("word/document.xml", "<w:document></w:document>")
        for i in range(entries):
            archive.writestr(f"d/{i}", b"")
    return buffer.getvalue()


def test_a_central_directory_bomb_is_rejected_with_bounded_memory():
    """zipfile.ZipFile built a ZipInfo per entry: ~230 MiB per 25 MB upload, in the
    API process. The bounded reader refuses archives with too many entries."""
    import time
    import tracemalloc

    data = _central_directory_bomb(60_000)
    tracemalloc.start()
    started = time.monotonic()
    try:
        with pytest.raises(UnsupportedFileTypeError):
            detect_and_validate_file_type("bomb.docx", data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024, f"peak {peak / 1048576:.1f} MiB"
    assert time.monotonic() - started < 2


def test_a_docx_with_many_but_plausible_entries_is_accepted():
    assert (
        detect_and_validate_file_type("big.docx", _central_directory_bomb(2_000)).extension
        == ".docx"
    )


def test_real_word_documents_are_accepted():
    from docx import Document

    for paragraphs in (1, 200):
        buffer = io.BytesIO()
        document = Document()
        for i in range(paragraphs):
            document.add_paragraph(f"Paragraph {i}")
        document.save(buffer)
        assert detect_and_validate_file_type("real.docx", buffer.getvalue()).extension == ".docx"


def test_a_zip64_docx_is_accepted():
    """The same archive with zip64 end records (as written for very large
    archives): sizes and counts come from the zip64 record."""
    import struct

    data = _make_docx_bytes()
    at = data.rfind(b"PK\x05\x06")
    _, _, _, _, entries, size, offset, _ = struct.unpack("<4sHHHHIIH", data[at : at + 22])
    zip64_record = struct.pack(
        "<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, entries, entries, size, offset
    )
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, at, 1)
    end = struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    zip64 = data[:at] + zip64_record + locator + end
    assert zipfile.ZipFile(io.BytesIO(zip64)).namelist() == ["word/document.xml"]
    assert detect_and_validate_file_type("z64.docx", zip64).extension == ".docx"


@pytest.mark.parametrize(
    "data",
    [
        b"PK\x03\x04" + b"\x00" * 100,  # no end-of-central-directory record
        _make_docx_bytes()[:-10],  # truncated
        b"PK\x03\x04" + b"\x00" * 30 + b"PK\x05\x06" + b"\x00" * 8 + b"\xff" * 12,  # bogus sizes
    ],
)
def test_malformed_archives_are_rejected(data):
    with pytest.raises(UnsupportedFileTypeError):
        detect_and_validate_file_type("bad.docx", data)
