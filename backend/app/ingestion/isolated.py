"""Parsing and chunking in a separate, memory- and time-limited process.

Parsers are third-party code (pypdf, lxml) fed attacker-controlled files, and
memory blow-ups were found that no single per-format check bounded: a DOCX whose
content sits in one table or one giant paragraph, an oversized package part, a
PDF whose 500 pages each sit just under the stream cap, millions of tiny
elements. Rather than chase each one in-process (where an overrun kills the API
or worker, and with it every other request), each document is parsed and chunked
in a child process that:

- limits its own data segment (RLIMIT_DATA, DOCUMENT_PARSE_MEMORY_LIMIT_MB) as its
  first action, so an overrun is a MemoryError inside the child;
- is killed after DOCUMENT_PARSE_TIMEOUT_SECONDS (quadratic-time inputs);
- returns only the chunks (bounded by MAX_CHUNKS_PER_DOCUMENT and
  MAX_DOCUMENT_TEXT_LENGTH) through a file in the document's private work dir.

A document that exceeds either limit fails permanently with a clear reason: it
would fail the same way on every retry, so it is never retried (no repeated
crash cycles). The parent's own memory is unaffected either way.

Protocol: `python -m app.ingestion.isolated <in> <mime> <out.json> <limits.json>`
writes {"chunks": [...]} or {"error": {"type": ..., "message": ...}} to <out>.
"""

import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from app.core.config import settings
from app.ingestion.chunking import Chunk
from app.ingestion.errors import (
    EmptyDocumentError,
    ParsingError,
    PermanentProcessingError,
    TextLimitExceededError,
)

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_ERRORS = {
    cls.__name__: cls
    for cls in (ParsingError, TextLimitExceededError, EmptyDocumentError, PermanentProcessingError)
}
# Settings the child must see (tests and operators change them at runtime).
_LIMIT_SETTINGS = (
    "MAX_DOCUMENT_TEXT_LENGTH",
    "MAX_DOCUMENT_PAGES",
    "MAX_DOCX_UNCOMPRESSED_SIZE_BYTES",
    "PDF_MAX_DECOMPRESSED_STREAM_BYTES",
    "CHUNK_SIZE_TOKENS",
    "CHUNK_OVERLAP_TOKENS",
    "MAX_CHUNKS_PER_DOCUMENT",
)
MEMORY_LIMIT_REASON = "This document is too large or complex to process within the memory limit."
TIME_LIMIT_REASON = "This document took too long to process and was stopped."
_SIGKILLED = -9  # asyncio returncode of a child killed by SIGKILL (e.g. the OOM killer)


async def parse_and_chunk(path: str, mime_type: str, workdir: str) -> list[Chunk]:
    """Parses and chunks `path` in a limited child process (or in-process when
    DOCUMENT_PARSE_ISOLATION is off). Raises the same permanent errors the
    in-process parsers do, plus ParsingError for a memory or time overrun."""
    if not settings.DOCUMENT_PARSE_ISOLATION:
        return await asyncio.to_thread(_parse_and_chunk, path, mime_type)

    out_path = os.path.join(workdir, "chunks.json")
    limits_path = os.path.join(workdir, "limits.json")
    with open(limits_path, "w", encoding="utf-8") as f:
        json.dump({name: getattr(settings, name) for name in _LIMIT_SETTINGS}, f)

    env = dict(os.environ)
    env["MALLOC_ARENA_MAX"] = "2"  # one parsing thread; don't reserve arenas
    env["DOCUMENT_PARSE_MEMORY_LIMIT_MB"] = str(settings.DOCUMENT_PARSE_MEMORY_LIMIT_MB)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.ingestion.isolated",
        path,
        mime_type,
        out_path,
        limits_path,
        cwd=str(_BACKEND_ROOT),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(), timeout=settings.DOCUMENT_PARSE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ParsingError(TIME_LIMIT_REASON) from None
    except asyncio.CancelledError:
        process.kill()  # shutdown: never leave a parser running unsupervised
        await asyncio.shield(process.wait())  # reap it (no zombie)
        raise

    try:
        with open(out_path, encoding="utf-8") as f:
            result = json.load(f)
    except (OSError, ValueError):
        result = None
    if result is None:
        # No result written: the child died (MemoryError while writing, killed by
        # the kernel, crashed). Treat as a resource overrun, never retried.
        tail = (stderr or b"")[-300:].decode("utf-8", "replace")
        if process.returncode == _SIGKILLED or "MemoryError" in tail:
            raise ParsingError(MEMORY_LIMIT_REASON)  # kernel OOM kill, or rlimit hit
        raise ParsingError(f"Could not process this document (exit {process.returncode}).")
    if "error" in result:
        error_class = _ERRORS.get(result["error"]["type"], ParsingError)
        raise error_class(result["error"]["message"])
    return [Chunk(**c) for c in result["chunks"]]


def _parse_and_chunk(path: str, mime_type: str) -> list[Chunk]:
    from app.ingestion.chunking import chunk_document
    from app.ingestion.parsers.registry import get_parser_for_mime_type

    parsed = get_parser_for_mime_type(mime_type).parse_file(path)
    if not parsed.elements:
        raise EmptyDocumentError("Document contains no extractable text.")
    total = sum(len(e.text) for e in parsed.elements)
    if total > settings.MAX_DOCUMENT_TEXT_LENGTH:
        raise TextLimitExceededError(
            f"Document contains more than {settings.MAX_DOCUMENT_TEXT_LENGTH} characters "
            "of extracted text, exceeding the processing limit."
        )
    chunks = chunk_document(
        parsed,
        chunk_size_tokens=settings.CHUNK_SIZE_TOKENS,
        chunk_overlap_tokens=settings.CHUNK_OVERLAP_TOKENS,
        max_chunks=settings.MAX_CHUNKS_PER_DOCUMENT,
    )
    if not chunks:
        raise EmptyDocumentError("Document contains no extractable text.")
    return chunks


def _limit_own_memory() -> None:
    if sys.platform == "win32":  # local development on Windows: no rlimit support
        return
    import resource

    limit_mb = int(os.environ.get("DOCUMENT_PARSE_MEMORY_LIMIT_MB", "224"))
    limit = limit_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))


def _near_memory_limit() -> bool:
    """True if this process's peak RSS reached 90% of its memory limit (an
    exhausted limit's peak RSS is measured at about the limit + 17 MiB).

    VmHWM, not getrusage's ru_maxrss: ru_maxrss survives exec, so it would
    report the (large) forking API process's peak as the child's."""
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            line = next(x for x in status if x.startswith("VmHWM:"))
    except (OSError, StopIteration):
        return False
    limit_mb = int(os.environ.get("DOCUMENT_PARSE_MEMORY_LIMIT_MB", "224"))
    return int(line.split()[1]) * 1024 >= 0.9 * limit_mb * 1024 * 1024


def _child_main(argv: list[str]) -> int:
    _limit_own_memory()  # first, before anything large is allocated
    in_path, mime_type, out_path, limits_path = argv
    with open(limits_path, encoding="utf-8") as f:
        for name, value in json.load(f).items():
            setattr(settings, name, value)
    try:
        chunks = _parse_and_chunk(in_path, mime_type)
        payload: dict = {"chunks": [asdict(c) for c in chunks]}
    except MemoryError:
        payload = {"error": {"type": "ParsingError", "message": MEMORY_LIMIT_REASON}}
    except Exception as exc:
        if _near_memory_limit():
            # C libraries report a failed allocation in their own words (libxml2:
            # "unknown error"; zlib: "Unable to allocate output buffer"), which
            # parsers then wrap as a parse error. Report what actually happened.
            payload = {"error": {"type": "ParsingError", "message": MEMORY_LIMIT_REASON}}
        elif isinstance(exc, PermanentProcessingError):
            payload = {"error": {"type": type(exc).__name__, "message": str(exc)}}
        else:  # parser bug: permanent, but never leak internals
            payload = {
                "error": {
                    "type": "ParsingError",
                    "message": f"Could not parse this document ({type(exc).__name__}).",
                }
            }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return 0


if __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv[1:]))
