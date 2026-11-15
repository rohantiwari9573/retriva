"""Parsing in a memory- and time-limited child process (app.ingestion.isolated).

These run the real child (`python -m app.ingestion.isolated`). The memory-limit
tests need RLIMIT_DATA, so they are Linux-only (CI and the Docker image).
"""

import asyncio
import io
import sys
import time
import zipfile

import pytest

from app.core.config import settings
from app.ingestion import isolated
from app.ingestion.errors import (
    EmptyDocumentError,
    ParsingError,
    PermanentProcessingError,
    TextLimitExceededError,
)

linux_only = pytest.mark.skipif(sys.platform == "win32", reason="needs RLIMIT_DATA")
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _write(tmp_path, name: str, data: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def _docx(body: str, extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f"<w:document {W}><w:body>{body}</w:body></w:document>")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def _giant_table_docx(cells: int) -> bytes:
    """Reviewer finding: everything inside one table, so the streaming parser can
    only free it at the end of the table (hundreds of MiB of lxml tree)."""
    cell = "<w:tc><w:p><w:r><w:t>x</w:t></w:r></w:p></w:tc>"
    intro = "<w:p><w:r><w:t>Body text outside the table.</w:t></w:r></w:p>"
    return _docx(intro + "<w:tbl><w:tr>" + cell * cells + "</w:tr></w:tbl>")


@pytest.mark.parametrize(
    ("name", "mime", "data"),
    [
        ("a.txt", "text/plain", b"First paragraph.\n\nSecond paragraph.\n\n" * 200),
        ("a.md", "text/markdown", b"# Title\nbody\n## Sub\r\nmore text\n" * 200),
        (
            "a.docx",
            DOCX,
            _docx("<w:p><w:r><w:t>Hello world, a paragraph.</w:t></w:r></w:p>" * 300),
        ),
    ],
)
async def test_child_produces_exactly_the_in_process_chunks(tmp_path, name, mime, data):
    path = _write(tmp_path, name, data)
    got = await isolated.parse_and_chunk(path, mime, str(tmp_path))
    assert got == isolated._parse_and_chunk(path, mime)
    assert got  # non-trivial


async def test_runtime_settings_reach_the_child(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MAX_CHUNKS_PER_DOCUMENT", 2)
    monkeypatch.setattr(settings, "CHUNK_SIZE_TOKENS", 20)
    monkeypatch.setattr(settings, "CHUNK_OVERLAP_TOKENS", 0)
    path = _write(tmp_path, "a.txt", b"A paragraph with several words in it.\n\n" * 50)
    with pytest.raises(PermanentProcessingError, match="more than 2 chunks"):
        await isolated.parse_and_chunk(path, "text/plain", str(tmp_path))


@pytest.mark.parametrize(
    ("data", "mime", "error"),
    [
        (b"   \n\n  ", "text/plain", EmptyDocumentError),
        (b"x" * 5000, "text/plain", TextLimitExceededError),
        (b"%PDF-1.7 not really a pdf", "application/pdf", ParsingError),
    ],
)
async def test_child_errors_keep_their_permanent_type(tmp_path, monkeypatch, data, mime, error):
    monkeypatch.setattr(settings, "MAX_DOCUMENT_TEXT_LENGTH", 1000)
    path = _write(tmp_path, "f", data)
    with pytest.raises(error):
        await isolated.parse_and_chunk(path, mime, str(tmp_path))


@linux_only
async def test_memory_overrun_fails_the_document_and_spares_the_parent(tmp_path, monkeypatch):
    """The giant-table DOCX exhausts the child's limit; the parent's peak RSS
    (ru_maxrss, which counts lxml's C allocations, unlike tracemalloc) must not
    move. (The child's own peak, measured directly, is about the limit + 17 MiB;
    RUSAGE_CHILDREN can't show it here, as it reflects the forked pytest image.)"""
    import resource

    monkeypatch.setattr(settings, "DOCUMENT_PARSE_MEMORY_LIMIT_MB", 96)
    path = _write(tmp_path, "t.docx", _giant_table_docx(600_000))
    parent_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    with pytest.raises(ParsingError) as info:
        await isolated.parse_and_chunk(path, DOCX, str(tmp_path))

    assert str(info.value) == isolated.MEMORY_LIMIT_REASON
    parent_growth_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - parent_before
    assert parent_growth_kib < 20 * 1024, f"parent grew {parent_growth_kib // 1024} MiB"


@linux_only
async def test_the_same_document_parses_fine_in_process_memory_permitting(tmp_path):
    """Control for the test above: the file is valid, just too costly for 96 MiB.
    Under the default limit a smaller table still parses."""
    path = _write(tmp_path, "t.docx", _giant_table_docx(20_000))
    chunks = await isolated.parse_and_chunk(path, DOCX, str(tmp_path))
    assert chunks


@linux_only
async def test_oversized_relationships_part_is_rejected(tmp_path):
    """Reviewer finding: a 164 KB DOCX whose _rels/.rels inflates to ~90 MB."""
    rels = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + b"<Relationship/>" * 2_000_000
        + b"</Relationships>"
    )
    data = _docx("<w:p><w:r><w:t>hi</w:t></w:r></w:p>", {"_rels/.rels": rels})
    path = _write(tmp_path, "r.docx", data)
    with pytest.raises(ParsingError, match="relationships part is unreasonably large"):
        await isolated.parse_and_chunk(path, DOCX, str(tmp_path))


@linux_only
async def test_many_tiny_elements_stop_at_the_chunk_limit(tmp_path, monkeypatch):
    """Reviewer finding: millions of tiny paragraphs. The chunker stops at
    MAX_CHUNKS_PER_DOCUMENT instead of chunking everything first."""
    monkeypatch.setattr(settings, "MAX_CHUNKS_PER_DOCUMENT", 50)
    monkeypatch.setattr(settings, "CHUNK_SIZE_TOKENS", 8)
    monkeypatch.setattr(settings, "CHUNK_OVERLAP_TOKENS", 0)
    path = _write(tmp_path, "p.docx", _docx("<w:p><w:r><w:t>ab cd</w:t></w:r></w:p>" * 200_000))
    started = time.monotonic()
    with pytest.raises(PermanentProcessingError, match="more than 50 chunks"):
        await isolated.parse_and_chunk(path, DOCX, str(tmp_path))
    assert time.monotonic() - started < 60


def _fake_child(monkeypatch, code: str):
    """Replaces the child command with `python -c code` (same plumbing)."""
    real = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        return await real(sys.executable, "-c", code, **kwargs)

    monkeypatch.setattr(isolated.asyncio, "create_subprocess_exec", spawn)


async def test_a_hung_parse_is_killed_at_the_time_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DOCUMENT_PARSE_TIMEOUT_SECONDS", 1)
    _fake_child(monkeypatch, "import time; time.sleep(120)")
    path = _write(tmp_path, "a.txt", b"x")
    started = time.monotonic()
    with pytest.raises(ParsingError) as info:
        await isolated.parse_and_chunk(path, "text/plain", str(tmp_path))
    assert str(info.value) == isolated.TIME_LIMIT_REASON
    assert time.monotonic() - started < 10


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_a_child_killed_by_the_kernel_is_reported_as_a_memory_overrun(tmp_path, monkeypatch):
    _fake_child(monkeypatch, "import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
    path = _write(tmp_path, "a.txt", b"x")
    with pytest.raises(ParsingError) as info:
        await isolated.parse_and_chunk(path, "text/plain", str(tmp_path))
    assert str(info.value) == isolated.MEMORY_LIMIT_REASON


async def test_a_crashed_child_fails_with_its_exit_code(tmp_path, monkeypatch):
    _fake_child(monkeypatch, "import os; os._exit(3)")
    path = _write(tmp_path, "a.txt", b"x")
    with pytest.raises(ParsingError, match=r"exit 3"):
        await isolated.parse_and_chunk(path, "text/plain", str(tmp_path))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process check")
async def test_cancelling_the_parse_kills_the_child(tmp_path, monkeypatch):
    """Shutdown cancels in-flight work: the child must not be left running."""
    import os

    pid_file = tmp_path / "pid"
    _fake_child(
        monkeypatch,
        f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(120)",
    )
    path = _write(tmp_path, "a.txt", b"x")
    task = asyncio.create_task(isolated.parse_and_chunk(path, "text/plain", str(tmp_path)))
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    with pytest.raises(OSError):  # gone (killed and reaped)
        os.kill(pid, 0)


async def test_isolation_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DOCUMENT_PARSE_ISOLATION", False)
    _fake_child(monkeypatch, "raise SystemExit('the child must not be used')")
    path = _write(tmp_path, "a.txt", b"Some text.\n\nMore text.")
    assert await isolated.parse_and_chunk(path, "text/plain", str(tmp_path))


@linux_only
async def test_an_allocation_failure_inside_a_c_parser_reports_the_memory_limit(
    tmp_path, monkeypatch
):
    """Reviewer finding: one giant paragraph. libxml2 reports its failed
    allocation as an XML syntax error ("unknown error"); the child must still say
    the document exceeded the memory limit, not that it is malformed."""
    monkeypatch.setattr(settings, "DOCUMENT_PARSE_MEMORY_LIMIT_MB", 96)
    run = "<w:r><w:t>word </w:t></w:r>"
    path = _write(tmp_path, "p.docx", _docx("<w:p>" + run * 1_500_000 + "</w:p>"))
    with pytest.raises(ParsingError) as info:
        await isolated.parse_and_chunk(path, DOCX, str(tmp_path))
    assert str(info.value) == isolated.MEMORY_LIMIT_REASON
