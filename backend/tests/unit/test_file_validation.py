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
