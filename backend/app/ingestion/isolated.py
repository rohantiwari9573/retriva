"""Parsing and chunking in a separate, resource-limited process.

Parsers are third-party code (pypdf, lxml) fed attacker-controlled files, and
memory blow-ups were found that no single per-format check bounded: a DOCX whose
content sits in one table or one giant paragraph, an oversized package part, a
PDF whose 500 pages each sit just under the stream cap, millions of tiny
elements. Rather than chase each one in-process (where an overrun kills the API
or worker, and with it every other request), each document is parsed and chunked
in a child process that, as its first actions:

- limits its data segment (RLIMIT_DATA, DOCUMENT_PARSE_MEMORY_LIMIT_MB), so an
  overrun is a MemoryError inside the child;
- limits its CPU time (RLIMIT_CPU, DOCUMENT_PARSE_TIMEOUT_SECONDS) - CPU time, not
  wall time, so a busy instance doesn't fail a valid document;
- limits the size of any file it writes (RLIMIT_FSIZE);
- asks the kernel to kill it if the parent dies (PR_SET_PDEATHSIG), so a parse is
  never left running unsupervised;
- silences warning logs (pypdf can log tens of MB per page for crafted files).

Its stderr goes to a file in the document's private work dir, of which only the
tail is read, never to a pipe the parent buffers; the chunks come back as JSON
lines through a file in the same dir. The child gets a minimal environment (no
secrets) and runs in the work dir, so no .env file is read either.

Outcomes:
- the document's own fault (parse error, too much text, memory or CPU limit
  reached) -> permanent failure with a clear reason, never retried;
- anything else (killed from outside, e.g. by the kernel when the whole instance
  was short of memory; a wall-clock stall; a crash; disk full) -> transient, so
  the attempt limit bounds the retries and a valid document isn't failed for an
  instance-wide problem.

Protocol: `python -m app.ingestion.isolated <in> <mime> <out.jsonl> <limits.json>`
writes a status line ({"ok": true} or {"error": {"type", "message"}}) followed,
on success, by one chunk per line.
"""

import asyncio
import errno
import json
import os
import re
import signal
import sys
from dataclasses import asdict
from pathlib import Path

import structlog

from app.core.config import settings
from app.ingestion.chunking import Chunk
from app.ingestion.errors import (
    EmptyDocumentError,
    ParsingError,
    PermanentProcessingError,
    TextLimitExceededError,
    TransientProcessingError,
)

logger = structlog.get_logger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_ERRORS = {
    cls.__name__: cls
    for cls in (
        ParsingError,
        TextLimitExceededError,
        EmptyDocumentError,
        PermanentProcessingError,
        TransientProcessingError,
    )
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
# Environment the child keeps; everything else (credentials) is dropped.
_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",  # Windows: needed to start Python at all
    "VIRTUAL_ENV",
    "PYTHONHOME",
    "ENVIRONMENT",
)
MEMORY_LIMIT_REASON = "This document is too large or complex to process within the memory limit."
TIME_LIMIT_REASON = "This document took too long to process and was stopped."
OUTPUT_LIMIT_REASON = "This document produced too much output to process."
_OUTPUT_LIMIT_BYTES = 64 * 1024 * 1024  # chunks (<= 5M chars of text) stay far below
_STDERR_TAIL_BYTES = 2000


def _signal_exit(name: str) -> int | None:
    """asyncio's returncode for a child killed by this signal (None on Windows)."""
    sig = getattr(signal, name, None)
    return -int(sig) if sig is not None else None


async def parse_and_chunk(path: str, mime_type: str, workdir: str) -> list[Chunk]:
    """Parses and chunks `path` in a limited child process (or in-process when
    DOCUMENT_PARSE_ISOLATION is off). Raises the same permanent errors the
    in-process parsers do, plus ParsingError for a memory or CPU overrun and
    TransientProcessingError when the child was stopped for reasons outside it."""
    if not settings.DOCUMENT_PARSE_ISOLATION:
        return await asyncio.to_thread(_parse_and_chunk, path, mime_type)

    out_path = os.path.join(workdir, "chunks.jsonl")
    limits_path = os.path.join(workdir, "limits.json")
    stderr_path = os.path.join(workdir, "parser-stderr.log")
    with open(limits_path, "w", encoding="utf-8") as f:
        json.dump({name: getattr(settings, name) for name in _LIMIT_SETTINGS}, f)

    with open(stderr_path, "wb") as stderr_file:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.ingestion.isolated",
            os.path.abspath(path),
            mime_type,
            out_path,
            limits_path,
            cwd=workdir,
            env=_child_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=stderr_file,
        )
    try:
        await asyncio.wait_for(process.wait(), timeout=settings.DOCUMENT_PARSE_WALL_TIMEOUT_SECONDS)
    except TimeoutError:
        cpu_used = _cpu_seconds(process.pid)  # read before the kill reaps it
        process.kill()
        await process.wait()
        # On a fractional-CPU instance the wall-clock backstop can fire before
        # the CPU limit. If the child had used most of its CPU budget anyway, the
        # document is the cost: fail it (retrying would hold the processing slot
        # for another full timeout each time). Otherwise the instance was
        # starved or the child blocked: retry.
        if cpu_used is not None and cpu_used >= 0.5 * settings.DOCUMENT_PARSE_TIMEOUT_SECONDS:
            raise ParsingError(TIME_LIMIT_REASON) from None
        raise TransientProcessingError(
            "Parsing did not finish in time on a busy instance; it will be retried."
        ) from None
    except asyncio.CancelledError:
        process.kill()  # shutdown: never leave a parser running unsupervised
        await asyncio.shield(process.wait())  # reap it (no zombie)
        raise

    status, chunks = _read_result(out_path)
    if status is not None:
        if "error" in status:
            error_class = _ERRORS.get(status["error"]["type"], ParsingError)
            raise error_class(status["error"]["message"])
        return chunks

    # No result: the child died before writing it.
    code = process.returncode
    tail = _tail(stderr_path)
    if code == _signal_exit("SIGXCPU"):  # CPU time spent on this document
        raise ParsingError(TIME_LIMIT_REASON)
    if "MemoryError" in tail:
        raise ParsingError(MEMORY_LIMIT_REASON)
    logger.warning(
        "document_parser_exited_without_result", returncode=code, error=_error_type(tail)
    )
    raise TransientProcessingError(
        f"The document parser stopped unexpectedly (exit {code}); it will be retried."
    )


def _error_type(stderr_tail: str) -> str | None:
    """The exception class from a traceback's last line, without its message,
    which can quote document content."""
    for line in reversed(stderr_tail.strip().splitlines()):
        match = re.match(r"([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt))\b", line.strip())
        if match:
            return match.group(1)
    return None


def _cpu_seconds(pid: int) -> float | None:
    """User + system CPU time of a running process (Linux /proc), else None."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as stat:
            fields = stat.read().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST}
    env.update(
        PYTHONPATH=str(_BACKEND_ROOT),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONSAFEPATH="1",  # never import from the work dir (cwd)
        MALLOC_ARENA_MAX="2",  # one parsing thread; don't reserve arenas
        # Settings requires these; the child never connects or signs anything.
        JWT_SECRET="isolated-document-parser-has-no-secrets-0000",
        DATABASE_URL="postgresql+asyncpg://parser@localhost.invalid/none",
        OTEL_ENABLED="false",
        RETRIVA_PARSER_MEMORY_MB=str(settings.DOCUMENT_PARSE_MEMORY_LIMIT_MB),
        RETRIVA_PARSER_CPU_SECONDS=str(settings.DOCUMENT_PARSE_TIMEOUT_SECONDS),
        RETRIVA_PARSER_PARENT_PID=str(os.getpid()),
    )
    return env


def _read_result(out_path: str) -> tuple[dict | None, list[Chunk]]:
    """(status, chunks), or (None, []) if no complete result was written."""
    try:
        with open(out_path, encoding="utf-8") as f:
            status = json.loads(f.readline())
            chunks = [Chunk(**json.loads(line)) for line in f] if "ok" in status else []
            if "ok" in status and len(chunks) != status.get("chunks"):
                return None, []  # truncated
            return status, chunks
    except (OSError, ValueError, TypeError):
        return None, []


def _tail(path: str) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - _STDERR_TAIL_BYTES))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


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


# ---------------------------------------------------------------- child side


class _SetupError(Exception):
    pass


def _apply_child_limits() -> None:
    """Called first in the child. Raises _SetupError if a limit can't be set:
    parsing without the limits would put the API's memory at risk."""
    if sys.platform == "win32":  # local development on Windows: no rlimits
        return
    import resource

    try:
        memory = int(os.environ.get("RETRIVA_PARSER_MEMORY_MB", "224")) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_DATA, (memory, memory))
        cpu = int(os.environ.get("RETRIVA_PARSER_CPU_SECONDS", "120"))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))  # SIGXCPU, then SIGKILL
        # Python ignores SIGXFSZ, so an oversized write raises OSError(EFBIG).
        resource.setrlimit(resource.RLIMIT_FSIZE, (_OUTPUT_LIMIT_BYTES, _OUTPUT_LIMIT_BYTES))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # SIGXCPU would dump core
    except (ValueError, OSError) as exc:
        raise _SetupError(f"setrlimit failed: {exc}") from exc

    if sys.platform.startswith("linux"):
        # PDEATHSIG follows the *thread* that forked us: the attempt's event-loop
        # thread (the InlineRunner executor thread, or Celery's main thread),
        # which is blocked awaiting this child, so it can't exit first. Don't
        # spawn the child from a short-lived thread (e.g. asyncio.to_thread).
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        pr_set_pdeathsig = 1
        if libc.prctl(pr_set_pdeathsig, int(signal.SIGKILL), 0, 0, 0) != 0:
            raise _SetupError("prctl(PR_SET_PDEATHSIG) failed")
    expected_parent = os.environ.get("RETRIVA_PARSER_PARENT_PID")
    if expected_parent and os.getppid() != int(expected_parent):
        os._exit(0)  # the parent died before the death signal was armed


def _is_memory_failure(exc: BaseException) -> bool:
    """A failed allocation, however the library reported it: MemoryError anywhere
    in the cause chain (parsers wrap errors), C libraries' own wording (zlib:
    "Unable to allocate output buffer"; libxml2: "unknown error"), or this
    process's peak RSS at the limit (an exhausted limit peaks at ~limit + 17 MiB)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, MemoryError) or "allocate" in str(current).lower():
            return True
        current = current.__cause__ or current.__context__
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            line = next(x for x in status if x.startswith("VmHWM:"))
    except (OSError, StopIteration):
        return False
    # VmHWM, not getrusage's ru_maxrss: ru_maxrss survives exec, so it would
    # report the (large) forking API process's peak as the child's.
    limit = int(os.environ.get("RETRIVA_PARSER_MEMORY_MB", "224")) * 1024 * 1024
    return int(line.split()[1]) * 1024 >= 0.9 * limit


def _status_for(exc: BaseException) -> dict:
    if isinstance(exc, _SetupError):
        message = "The document parser could not start safely; it will be retried."
        return {"error": {"type": "TransientProcessingError", "message": message}}
    if _is_memory_failure(exc):
        return {"error": {"type": "ParsingError", "message": MEMORY_LIMIT_REASON}}
    if isinstance(exc, PermanentProcessingError):
        return {"error": {"type": type(exc).__name__, "message": str(exc)}}
    # A parser bug: permanent, but never leak internals.
    message = f"Could not parse this document ({type(exc).__name__})."
    return {"error": {"type": "ParsingError", "message": message}}


def _child_main(argv: list[str]) -> int:
    try:
        _apply_child_limits()  # first, before anything large is allocated
    except _SetupError as exc:
        print(f"document parser setup failed: {exc}", file=sys.stderr)
        status: dict = _status_for(exc)
        chunks: list[Chunk] = []
    else:
        import logging
        import warnings

        logging.disable(logging.WARNING)  # pypdf warnings can flood (crafted CMaps)
        warnings.simplefilter("ignore")
        in_path, mime_type, _, limits_path = argv
        with open(limits_path, encoding="utf-8") as f:
            for name, value in json.load(f).items():
                setattr(settings, name, value)
        try:
            chunks = _parse_and_chunk(in_path, mime_type)
            status = {"ok": True, "chunks": len(chunks)}
        except Exception as exc:  # MemoryError included
            chunks = []
            status = _status_for(exc)

    try:
        _write_result(argv[2], status, chunks)
    except OSError as exc:
        if exc.errno != errno.EFBIG:
            raise  # e.g. disk full: no result -> transient in the parent
        error = {"error": {"type": "ParsingError", "message": OUTPUT_LIMIT_REASON}}
        _write_result(argv[2], error, [])
    return 0


def _write_result(path: str, status: dict, chunks: list[Chunk]) -> None:
    with open(path, "w", encoding="utf-8") as out:
        out.write(json.dumps(status) + "\n")
        for chunk in chunks:
            out.write(json.dumps(asdict(chunk), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv[1:]))
