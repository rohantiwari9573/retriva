"""Unit tests for the pure helpers in app.storage.s3 - the S3StorageProvider
class itself needs a live MinIO/S3 and is only exercised manually against
Docker Compose (see README), but _escape_content_disposition_filename has no
I/O and is worth testing directly."""

from app.storage.s3 import _escape_content_disposition_filename


def test_plain_filename_is_unchanged():
    assert _escape_content_disposition_filename("handbook.pdf") == "handbook.pdf"


def test_quote_is_backslash_escaped():
    # Without escaping, this would close the quoted parameter early and let
    # anything after it be interpreted as additional header content.
    result = _escape_content_disposition_filename('evil".pdf')
    assert result == 'evil\\".pdf'
    assert '"; ' not in result


def test_backslash_is_escaped_before_quote_escaping():
    result = _escape_content_disposition_filename("weird\\name.pdf")
    assert result == "weird\\\\name.pdf"


def test_control_characters_are_stripped():
    result = _escape_content_disposition_filename("evil\r\nX-Injected: 1.pdf")
    assert "\r" not in result
    assert "\n" not in result
