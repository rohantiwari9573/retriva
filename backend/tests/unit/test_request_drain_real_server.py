"""Refused upload bodies against a real uvicorn server, over real sockets.

The ASGI-level tests in test_request_size_limit.py check the middleware's decisions;
these check what a client actually gets on the wire, including the parts uvicorn
owns: it answers `100 Continue` itself on the first body read, and a connection
closed with unread data can reset. They also check the cost of refused requests:
the app is never called, nothing is kept in memory or on disk, and concurrent
refusals stay bounded.
"""

import contextlib
import socket
import tempfile
import threading
import time
import tracemalloc
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
import uvicorn
from fastapi import FastAPI, File, UploadFile

from app.core.request_size import RequestBodyLimitMiddleware, max_request_body_bytes

MiB = 1024 * 1024
UPLOAD_PATH = "/api/v1/organizations/00000000-0000-0000-0000-000000000000/documents"
CAP = max_request_body_bytes()  # 25 MiB document + 1 MiB multipart overhead


class _Server:
    def __init__(self) -> None:
        self.calls = 0
        app = FastAPI()

        @app.post(UPLOAD_PATH)
        async def upload(file: UploadFile = File(...)) -> dict[str, int]:
            self.calls += 1
            return {"bytes": len(await file.read())}

        app.add_middleware(RequestBodyLimitMiddleware)
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "_Server":
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        assert self.server.started, "uvicorn did not start"
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def server() -> Iterator[_Server]:
    with _Server() as s:
        yield s


def _request_head(declared: int | None, expect: bool, boundary: str = "B") -> bytes:
    lines = [
        f"POST {UPLOAD_PATH} HTTP/1.1",
        "Host: test",
        f"Content-Type: multipart/form-data; boundary={boundary}",
    ]
    lines.append(
        f"Content-Length: {declared}" if declared is not None else "Transfer-Encoding: chunked"
    )
    if expect:
        lines.append("Expect: 100-continue")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def _read_status(sock: socket.socket, timeout: float = 30.0) -> int | None:
    """The final status code, skipping a 100 Continue; None if the connection reset."""
    sock.settimeout(timeout)
    data = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            while data.startswith(b"HTTP/1.1 100"):
                end = data.find(b"\r\n\r\n")
                if end < 0:
                    break
                data = data[end + 4 :]
            if b"\r\n\r\n" in data and not data.startswith(b"HTTP/1.1 100"):
                break
    except (ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
        pass
    if not data.startswith(b"HTTP/1.1 "):
        return None
    return int(data[9:12])


def _send(
    port: int,
    *,
    declared: int | None,
    send_bytes: int,
    expect: bool,
    chunk: int = 256 * 1024,
    delay: float = 0.0,
    then_wait: float = 0.0,
    close_early: bool = False,
) -> dict:
    """Plays a client (or a truncating proxy): sends `send_bytes` of body, then reads."""
    started = time.monotonic()
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    got_100 = False
    sent = 0
    try:
        sock.sendall(_request_head(declared, expect))
        if expect:  # like curl: wait up to 1 s for 100 Continue or a final answer
            sock.settimeout(1.0)
            try:
                first = sock.recv(64, socket.MSG_PEEK)
                got_100 = first.startswith(b"HTTP/1.1 100")
            except TimeoutError:
                pass
        piece = b"0" * chunk
        while sent < send_bytes:
            n = min(chunk, send_bytes - sent)
            try:
                if declared is None:
                    sock.sendall(f"{n:x}\r\n".encode() + piece[:n] + b"\r\n")
                else:
                    sock.sendall(piece[:n])
            except OSError:
                break  # the server closed: read whatever it answered
            sent += n
            if delay:
                time.sleep(delay)
        if close_early:
            sock.close()
            return {
                "status": None,
                "sent": sent,
                "got_100": got_100,
                "seconds": time.monotonic() - started,
            }
        if declared is None and sent >= send_bytes:
            with contextlib.suppress(OSError):
                sock.sendall(b"0\r\n\r\n")
        if then_wait:
            time.sleep(then_wait)
        status = _read_status(sock)
    finally:
        sock.close()
    return {
        "status": status,
        "sent": sent,
        "got_100": got_100,
        "seconds": time.monotonic() - started,
    }


def _multipart(size: int) -> bytes:
    head = (
        b'--B\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
    )
    return head + b"a" * size + b"\r\n--B--\r\n"


def _ok_upload(port: int, size: int = 1024) -> int | None:
    body = _multipart(size)
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    try:
        sock.sendall(_request_head(len(body), expect=False) + body)
        return _read_status(sock)
    finally:
        sock.close()


@pytest.mark.parametrize("expect", [False, True])
def test_oversized_upload_gets_a_readable_413_with_and_without_expect(
    server: _Server, expect: bool
) -> None:
    result = _send(server.port, declared=30 * MiB, send_bytes=30 * MiB, expect=expect)

    assert result["status"] == 413
    assert result["got_100"] is expect  # with Expect the body is invited, then drained
    assert server.calls == 0


@pytest.mark.parametrize("declared", [63 * MiB, 64 * MiB])
def test_expect_bodies_up_to_the_drain_bound_get_a_413(server: _Server, declared: int) -> None:
    result = _send(server.port, declared=declared, send_bytes=declared, expect=True)

    assert result["status"] == 413
    assert server.calls == 0


def test_a_truncating_proxy_forwarding_expect_gets_a_413_not_a_reset(server: _Server) -> None:
    """The CI failure: self-hosted Next answered 100 itself, sent its 27 MiB buffer of a
    100 MiB upload with the original Content-Length and Expect, and went quiet."""
    for _ in range(5):
        result = _send(server.port, declared=100 * MiB, send_bytes=27 * MiB, expect=True)
        assert result["status"] == 413
    assert server.calls == 0


def test_expect_body_past_the_drain_bound_is_cut_off_quickly(server: _Server) -> None:
    """Documented trade-off: declared over DRAIN_MAX_BYTES and sent in full, the server
    stops reading at the bound and closes; the client may see a reset instead of the
    413, but the server's work is bounded and it keeps serving."""
    result = _send(server.port, declared=100 * MiB, send_bytes=100 * MiB, expect=True)

    assert result["status"] in (413, None)
    assert result["sent"] < 100 * MiB or result["status"] == 413
    assert server.calls == 0
    assert _ok_upload(server.port) == 200


def test_body_exactly_at_the_upload_cap_is_accepted(server: _Server) -> None:
    body = _multipart(CAP - len(_multipart(0)))
    assert len(body) == CAP
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=60)
    try:
        sock.sendall(_request_head(len(body), expect=True))
        sock.sendall(body)
        assert _read_status(sock, timeout=60) == 200
    finally:
        sock.close()
    assert server.calls == 1


def test_one_byte_over_the_cap_is_refused(server: _Server) -> None:
    result = _send(server.port, declared=CAP + 1, send_bytes=CAP + 1, expect=False)

    assert result["status"] == 413
    assert server.calls == 0


def test_a_sender_that_goes_quiet_gets_its_413_after_the_idle_bound(server: _Server) -> None:
    result = _send(server.port, declared=40 * MiB, send_bytes=1 * MiB, expect=False)

    assert result["status"] == 413
    assert result["seconds"] < 10


def test_a_slow_sender_is_cut_off_at_the_drain_time_bound(server: _Server, monkeypatch) -> None:
    monkeypatch.setattr("app.core.request_size.DRAIN_MAX_SECONDS", 1.0)
    result = _send(
        server.port,
        declared=40 * MiB,
        send_bytes=40 * MiB,
        expect=False,
        chunk=64 * 1024,
        delay=0.05,
    )

    # Cut off after ~1 s instead of reading for the ~30 s the trickle would take.
    assert result["seconds"] < 15
    assert result["status"] in (413, None)
    assert _ok_upload(server.port) == 200


def test_a_client_that_disconnects_mid_drain_does_not_hurt_the_server(server: _Server) -> None:
    for expect in (False, True):
        _send(server.port, declared=40 * MiB, send_bytes=5 * MiB, expect=expect, close_early=True)
    time.sleep(0.5)

    assert _ok_upload(server.port) == 200
    assert server.calls == 1


def test_retries_after_a_refusal_work_on_new_connections(server: _Server) -> None:
    statuses = [
        _send(server.port, declared=30 * MiB, send_bytes=30 * MiB, expect=e)["status"]
        for e in (False, True, False)
    ]

    assert statuses == [413, 413, 413]
    assert _ok_upload(server.port, size=2 * MiB) == 200


def test_concurrent_refused_uploads_stay_bounded_in_memory(server: _Server) -> None:
    """16 clients each push 40 MiB at once (640 MiB in total). Draining keeps nothing,
    so the server's extra memory must stay far below the bytes in flight."""
    tracemalloc.start()
    tracemalloc.reset_peak()
    baseline, _ = tracemalloc.get_traced_memory()
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(
            pool.map(
                lambda i: _send(
                    server.port, declared=40 * MiB, send_bytes=40 * MiB, expect=bool(i % 2)
                ),
                range(16),
            )
        )
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert [r["status"] for r in results] == [413] * 16
    assert server.calls == 0
    # Allocation peak above baseline, across the server thread and the 16 client threads
    # (whose own 256 KiB send buffers are included): bounded, not ~640 MiB.
    assert peak - baseline < 64 * MiB, f"peak grew by {(peak - baseline) / MiB:.1f} MiB"
    assert _ok_upload(server.port) == 200


def test_refused_chunked_uploads_leave_no_temporary_files(
    server: _Server, tmp_path, monkeypatch
) -> None:
    """A chunked body has no declared length, so the app starts parsing the multipart
    upload (spooling to a temporary file past 1 MiB) before the cap is hit."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    results = []
    for _ in range(3):
        sock = socket.create_connection(("127.0.0.1", server.port), timeout=30)
        try:
            sock.sendall(_request_head(None, expect=False))
            head = (
                b'--B\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n'
                b"Content-Type: text/plain\r\n\r\n"
            )
            sock.sendall(f"{len(head):x}\r\n".encode() + head + b"\r\n")
            piece = b"0" * (1 * MiB)
            for _ in range(30):  # 30 MiB, past the 26 MiB cap
                try:
                    sock.sendall(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                except OSError:
                    break
            results.append(_read_status(sock))
        finally:
            sock.close()
    time.sleep(0.5)

    assert results == [413, 413, 413]
    assert server.calls == 0
    assert list(tmp_path.iterdir()) == []


def test_the_server_stays_responsive_while_refusals_drain(server: _Server) -> None:
    """A refused upload being drained must not block other requests."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(
            _send,
            server.port,
            declared=40 * MiB,
            send_bytes=40 * MiB,
            expect=False,
            chunk=128 * 1024,
            delay=0.01,
        )
        time.sleep(0.3)
        started = time.monotonic()
        assert _ok_upload(server.port) == 200
        assert time.monotonic() - started < 5
        assert slow.result()["status"] in (413, None)
