"""File type validation via magic-byte sniffing.

Hand-rolled rather than via python-magic/libmagic: the four formats Retriva
accepts have simple, well-documented signatures, and avoiding the dependency
sidesteps python-magic-bin being unmaintained and unreliable to install on
newer Python/Windows combinations. Never trust the client-supplied filename
extension or Content-Type header alone - both are attacker-controlled.
"""

import codecs
import io
import struct
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


# A real DOCX has tens to a few hundred entries and a central directory of a few
# KiB. This runs in the API process on every upload, so it must be bounded:
# zipfile.ZipFile builds a ZipInfo object per entry, and a 25 MB archive of
# nothing but ~500k directory entries took the API from 113 to 374 MiB per
# upload (two at once OOM-killed a 512 MB instance).
_MAX_ZIP_ENTRIES = 10_000
_MAX_ZIP_DIRECTORY_BYTES = 2 * 1024 * 1024
_EOCD = b"PK\x05\x06"
_ZIP64_EOCD_LOCATOR = b"PK\x06\x07"
_ZIP64_EOCD = b"PK\x06\x06"
_CENTRAL_HEADER = b"PK\x01\x02"


def _central_directory(file: BinaryIO) -> bytes | None:
    """The archive's central directory, read with bounded memory, or None if it is
    malformed or larger than a document could need."""
    file.seek(0, 2)
    size = file.tell()
    tail_length = min(size, 22 + 0xFFFF)  # EOCD record + the longest possible comment
    file.seek(size - tail_length)
    tail = file.read(tail_length)
    at = tail.rfind(_EOCD)
    if at < 0 or len(tail) - at < 22:
        return None
    _, _, _, _, entries, directory_size, directory_offset, _ = struct.unpack(
        "<4sHHHHIIH", tail[at : at + 22]
    )
    if entries == 0xFFFF or directory_size == 0xFFFFFFFF or directory_offset == 0xFFFFFFFF:
        locator = at - 20
        if locator < 0 or tail[locator : locator + 4] != _ZIP64_EOCD_LOCATOR:
            return None
        zip64_offset = struct.unpack("<Q", tail[locator + 8 : locator + 16])[0]
        if zip64_offset + 56 > size:  # also keeps seek() clear of 64-bit overflow
            return None
        file.seek(zip64_offset)
        record = file.read(56)
        if len(record) < 56 or record[:4] != _ZIP64_EOCD:
            return None
        entries, directory_size, directory_offset = struct.unpack("<QQQ", record[32:56])
    if entries > _MAX_ZIP_ENTRIES or directory_size > _MAX_ZIP_DIRECTORY_BYTES:
        return None
    if directory_offset + directory_size > size:
        return None
    file.seek(directory_offset)
    directory = file.read(directory_size)
    return directory if len(directory) == directory_size else None


def _zip_has_entry(directory: bytes, wanted: bytes) -> bool:
    at = 0
    while at + 46 <= len(directory):
        if directory[at : at + 4] != _CENTRAL_HEADER:
            return False
        name_length, extra_length, comment_length = struct.unpack(
            "<HHH", directory[at + 28 : at + 34]
        )
        if directory[at + 46 : at + 46 + name_length] == wanted:
            return True
        at += 46 + name_length + extra_length + comment_length
    return False


def _is_docx(file: BinaryIO) -> bool:
    # DOCX is a ZIP archive with a specific internal layout - checking just
    # the "PK\x03\x04" local-file-header signature would also match plain
    # .zip, .xlsx, .pptx, etc., so confirm a docx-specific entry exists too.
    # The central directory is read directly (bounded; see above), never
    # through zipfile, which would materialise every entry.
    file.seek(0)
    if file.read(4) != b"PK\x03\x04":
        return False
    directory = _central_directory(file)
    return directory is not None and _zip_has_entry(directory, b"word/document.xml")


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
