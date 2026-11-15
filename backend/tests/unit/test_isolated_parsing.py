"""Parsing in a memory- and time-limited child process (app.ingestion.isolated).

These run the real child (`python -m app.ingestion.isolated`). The memory-limit
tests need RLIMIT_DATA, so they are Linux-only (CI and the Docker image).
"""

import asyncio
import contextlib
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
    TransientProcessingError,
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


async def test_a_stalled_child_is_killed_at_the_wall_clock_limit_and_retried(tmp_path, monkeypatch):
    """Wall time is a backstop (starved instance, blocked child): transient."""
    monkeypatch.setattr(settings, "DOCUMENT_PARSE_WALL_TIMEOUT_SECONDS", 1)
    _fake_child(monkeypatch, "import time; time.sleep(120)")
    path = _write(tmp_path, "a.txt", b"x")
    started = time.monotonic()
    with pytest.raises(TransientProcessingError, match="busy instance"):
        await isolated.parse_and_chunk(path, "text/plain", str(tmp_path))
    assert time.monotonic() - started < 10


@linux_only
async def test_a_document_that_burns_its_cpu_budget_fails_permanently(tmp_path, monkeypatch):
    """CPU time is the document's own cost: RLIMIT_CPU -> SIGXCPU -> permanent."""
    monkeypatch.setattr(settings, "DOCUMENT_PARSE_TIMEOUT_SECONDS", 1)
    _fake_child(
        monkeypatch,
        "from app.ingestion.isolated import _apply_child_limits\n"
        "_apply_child_limits()\n"
        "while True: pass",
    )
    path = _write(tmp_path, "a.txt", b"x")
    started = time.monotonic()
    with pytest.raises(ParsingError) as info:
        await isolated.parse_and_chunk(path, "text/plain", str(tmp_path))
    assert str(info.value) == isolated.TIME_LIMIT_REASON
    assert time.monotonic() - started < 30


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_a_child_killed_from_outside_is_retried_not_failed(tmp_path, monkeypatch):
    """Reviewer M2: the kernel may kill the child because the whole instance is
    short of memory - not the document's fault, so not a permanent failure."""
    _fake_child(monkeypatch, "import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
    path = _write(tmp_path, "a.txt", b"x")
    with pytest.raises(TransientProcessingError, match="exit -9"):
        await isolated.parse_and_chunk(path, "text/plain", str(tmp_path))


async def test_a_crashed_child_is_retried_with_its_exit_code(tmp_path, monkeypatch):
    _fake_child(monkeypatch, "import os; os._exit(3)")
    path = _write(tmp_path, "a.txt", b"x")
    with pytest.raises(TransientProcessingError, match=r"exit 3"):
        await isolated.parse_and_chunk(path, "text/plain", str(tmp_path))


def test_a_child_that_cannot_apply_its_limits_reports_a_transient_failure():
    status = isolated._status_for(isolated._SetupError("setrlimit failed"))
    assert status["error"]["type"] == "TransientProcessingError"


@linux_only
async def test_child_stderr_never_accumulates_in_the_parent(tmp_path, monkeypatch):
    """Reviewer C1: stderr was a pipe the parent buffered whole (a 58 KB PDF's
    warnings OOM-killed the API). It is now a file; only its tail is read."""
    import resource

    _fake_child(
        monkeypatch,
        "import sys\nfor _ in range(300): sys.stderr.buffer.write(b'w' * 1048576)\n"
        "raise SystemExit(1)",
    )
    path = _write(tmp_path, "a.txt", b"x")
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    with pytest.raises(TransientProcessingError):
        await isolated.parse_and_chunk(path, "text/plain", str(tmp_path))
    growth_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
    assert growth_kib < 20 * 1024, f"parent grew {growth_kib // 1024} MiB for 300 MiB of stderr"


def _warning_flood_pdf(lines: int, pages: int) -> bytes:
    """Reviewer C1's file: every page's font has a ToUnicode CMap of broken
    bfrange lines, each of which makes pypdf log a warning."""
    import zlib

    cmap = (
        b"/CIDInit /ProcSet findresource begin\nbegincmap\n1 begincodespacerange\n"
        b"<00> <FF>\nendcodespacerange\nbeginbfrange\n"
        + b"<41>\n" * lines
        + b"endbfrange\nendcmap\n"
    )
    packed = zlib.compress(cmap, 9)
    content = b"BT /F1 12 Tf 72 720 Td (AB) Tj ET"
    objs = [
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(packed) + packed + b"\nendstream",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    kids = []
    pages_id = 3 + 2 * pages
    for _ in range(pages):
        objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 1 0 R >>")
        font = len(objs)
        objs.append(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents 2 0 R >>" % (pages_id, font)
        )
        kids.append(len(objs))
    objs.append(
        b"<< /Type /Pages /Kids ["
        + b" ".join(b"%d 0 R" % k for k in kids)
        + b"] /Count %d >>" % pages
    )
    objs.append(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        len(objs),
        xref,
    )
    return bytes(out)


@linux_only
async def test_parser_warnings_are_not_written_by_the_child(tmp_path):
    path = _write(tmp_path, "w.pdf", _warning_flood_pdf(lines=20_000, pages=5))
    # "AB" may or may not extract; only the stderr volume matters here.
    with contextlib.suppress(ParsingError):
        await isolated.parse_and_chunk(path, "application/pdf", str(tmp_path))
    assert (tmp_path / "parser-stderr.log").stat().st_size < 64 * 1024


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG")
def test_the_child_dies_with_its_parent(tmp_path):
    """Reviewer M1: a child must not outlive a killed API/worker."""
    import os
    import signal
    import subprocess

    pid_file = tmp_path / "child.pid"
    child_code = (
        "import os, time\n"
        "from app.ingestion.isolated import _apply_child_limits\n"
        "_apply_child_limits()\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(120)\n"
    )
    parent_code = (
        "import os, subprocess, sys, time\n"
        "env = dict(os.environ, RETRIVA_PARSER_PARENT_PID=str(os.getpid()))\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], env=env)\n"
        "time.sleep(120)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code])
    try:
        for _ in range(200):
            if pid_file.exists() and pid_file.read_text():
                break
            time.sleep(0.05)
        child_pid = int(pid_file.read_text())
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait()
        for _ in range(100):
            try:
                os.kill(child_pid, 0)
            except OSError:
                break  # gone (killed by the kernel, reaped by init)
            if _is_zombie(child_pid):
                break
            time.sleep(0.05)
        else:
            pytest.fail("the parse child kept running after its parent was killed")
    finally:
        if parent.poll() is None:
            parent.kill()


def _is_zombie(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().split(") ")[1].startswith("Z")
    except OSError:
        return True


async def test_the_child_gets_no_credentials(tmp_path, monkeypatch):
    """Reviewer L2: a process parsing hostile input needs no secrets."""
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "do-not-leak-this")
    env_file = tmp_path / "env.json"
    _fake_child(
        monkeypatch,
        f"import json, os; json.dump(dict(os.environ), open({str(env_file)!r}, 'w'))",
    )
    path = _write(tmp_path, "a.txt", b"x")
    with pytest.raises(TransientProcessingError):
        await isolated.parse_and_chunk(path, "text/plain", str(tmp_path))
    env = __import__("json").loads(env_file.read_text())
    assert "S3_SECRET_ACCESS_KEY" not in env
    assert env["JWT_SECRET"] != settings.JWT_SECRET
    assert env["DATABASE_URL"] != settings.DATABASE_URL


async def test_non_ascii_chunks_survive_the_round_trip(tmp_path):
    text = "Ünïcödé — 日本語 😀 text.\n\nSecond ¶ paragraph.".encode()
    path = _write(tmp_path, "u.txt", text)
    assert await isolated.parse_and_chunk(
        path, "text/plain", str(tmp_path)
    ) == isolated._parse_and_chunk(path, "text/plain")


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


@linux_only
async def test_a_wall_timeout_after_most_of_the_cpu_budget_fails_permanently(tmp_path, monkeypatch):
    """Review M-A: on a fractional-CPU instance the wall-clock backstop fires
    before the CPU limit. A child that had used most of its CPU budget is the
    document's cost, not starvation: fail it rather than hold the slot again."""
    monkeypatch.setattr(settings, "DOCUMENT_PARSE_TIMEOUT_SECONDS", 1)  # threshold 0.5 CPU-s
    monkeypatch.setattr(settings, "DOCUMENT_PARSE_WALL_TIMEOUT_SECONDS", 2)
    _fake_child(monkeypatch, "while True: pass")  # no rlimit: only the wall clock stops it
    path = _write(tmp_path, "a.txt", b"x")
    with pytest.raises(ParsingError) as info:
        await isolated.parse_and_chunk(path, "text/plain", str(tmp_path))
    assert str(info.value) == isolated.TIME_LIMIT_REASON


@linux_only
async def test_output_over_the_file_size_limit_fails_permanently(tmp_path, monkeypatch):
    """Review L-a: Python ignores SIGXFSZ, so RLIMIT_FSIZE surfaces as EFBIG."""
    real = asyncio.create_subprocess_exec
    code = (
        "import sys\n"
        "from app.ingestion import isolated\n"
        "isolated._OUTPUT_LIMIT_BYTES = 4096\n"
        "raise SystemExit(isolated._child_main(sys.argv[1:]))\n"
    )

    async def spawn(*args, **kwargs):  # the real child, with a tiny output limit
        return await real(sys.executable, "-c", code, *args[3:], **kwargs)

    monkeypatch.setattr(isolated.asyncio, "create_subprocess_exec", spawn)
    path = _write(tmp_path, "a.txt", b"A sentence of some length here.\n\n" * 2000)
    with pytest.raises(ParsingError) as info:
        await isolated.parse_and_chunk(path, "text/plain", str(tmp_path))
    assert str(info.value) == isolated.OUTPUT_LIMIT_REASON
