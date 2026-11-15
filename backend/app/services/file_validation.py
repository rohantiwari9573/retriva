"""File type validation via magic-byte sniffing.

Hand-rolled rather than via python-magic/libmagic: the four formats Retriva
accepts have simple, well-documented signatures, and avoiding the dependency
sidesteps python-magic-bin being unmaintained and unreliable to install on
newer Python/Windows combinations. Never trust the client-supplied filename
extension or Content-Type header alone - both are attacker-controlled.
"""

import codecs
import io
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO

from app.core.exceptions import UnsupportedFileTypeError


@dataclass(frozen=True)
class DetectedFileType:
    extension: str
    mime_type: str


_TEXT_BLOCK_BYTES = 1024 * 1024


def _is_pdf(file: BinaryIO) -> bool:
    file.seek(0)
    return file.read(5) == b"%PDF-"


def _is_docx(file: BinaryIO) -> bool:
    # DOCX is a ZIP archive with a specific internal layout - checking just
    # the "PK\x03\x04" local-file-header signature would also match plain
    # .zip, .xlsx, .pptx, etc., so confirm a docx-specific entry exists too.
    # zipfile reads only the central directory from the (seekable) file.
    file.seek(0)
    if file.read(4) != b"PK\x03\x04":
        return False
    file.seek(0)
    try:
        with zipfile.ZipFile(file) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile:
        return False
    return "word/document.xml" in names


def _is_text(file: BinaryIO) -> bool:
    # Binary files almost never decode cleanly as UTF-8 and null bytes are a
    # strong binary signal - good enough to distinguish "this is prose" from
    # "this is a renamed .exe" without a full content-sniffing library.
    # Decoded incrementally in 1 MiB blocks, so memory stays flat.
    file.seek(0)
    decoder = codecs.getincrementaldecoder("utf-8")()
    while True:
        block = file.read(_TEXT_BLOCK_BYTES)
        if not block:
            break
        if b"\x00" in block:
            return False
        try:
            decoder.decode(block)
        except UnicodeDecodeError:
            return False
    try:
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


# Order matters: PDF and DOCX signatures are checked first since they're
# unambiguous; TXT/MD share one "is it valid UTF-8 text" check and are
# distinguished only by the extension the user chose.
_VALIDATORS: dict[str, tuple[str, Callable[[BinaryIO], bool]]] = {
    ".pdf": ("application/pdf", _is_pdf),
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        _is_docx,
    ),
    ".txt": ("text/plain", _is_text),
    ".md": ("text/markdown", _is_text),
}


def detect_and_validate_file_type(filename: str, data: bytes | BinaryIO) -> DetectedFileType:
    """Validates that the full file content actually matches the format
    implied by `filename`'s extension. Needs the complete file (not just a
    head snippet) because DOCX's ZIP central directory lives at the end of
    the file, so it takes a seekable file object (or bytes, wrapped) and reads
    it in bounded pieces rather than as one buffer. Raises
    UnsupportedFileTypeError on any mismatch or unrecognized extension."""
    extension = _extract_extension(filename)
    entry = _VALIDATORS.get(extension)
    if entry is None:
        allowed = ", ".join(sorted(_VALIDATORS))
        raise UnsupportedFileTypeError(
            f"Unsupported file type '{extension or filename}'. Allowed types: {allowed}."
        )

    mime_type, validator = entry
    file = io.BytesIO(data) if isinstance(data, bytes | bytearray) else data
    try:
        valid = validator(file)
    finally:
        file.seek(0)
    if not valid:
        raise UnsupportedFileTypeError(
            f"File content does not match its '{extension}' extension. "
            "The file may be corrupted or mislabeled."
        )
    return DetectedFileType(extension=extension, mime_type=mime_type)


def _extract_extension(filename: str) -> str:
    # basename only - a filename like "../../etc/passwd.pdf" should validate
    # on its extension like any other; path traversal is prevented downstream
    # by never using client-supplied text in the storage key at all.
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()
