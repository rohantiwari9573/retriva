"""A body that stops arriving must not hold a connection open.

uvicorn waits for body bytes with no timeout and FastAPI reads the body before any
dependency, so these run the real app (and a small one) under a real uvicorn server
and talk to it over raw sockets: only that shows the 408 is written, the connection
is closed, and the server's request tasks are released.
"""

import asyncio
import contextlib
import json
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
import uvicorn
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.core.request_size import RequestBodyLimitMiddleware
from app.main import app as real_app

IDLE = 0.3
DEADLINE = 1.5
# Generous bound on anything the server should do promptly; failing tests time out
# here instead of hanging.
WAIT = 10.0


UPLOAD_DEADLINE = 4.0
UPLOAD_PATH = "/api/v1/organizations/org/documents"


@pytest.fixture(autouse=True)
def _short_timeouts(monkeypatch) -> None:
    monkeypatch.setattr("app.core.request_size.BODY_IDLE_SECONDS", IDLE)
    monkeypatch.setattr("app.core.request_size.BODY_MAX_SECONDS", DEADLINE)
    monkeypatch.setattr("app.core.request_size.UPLOAD_BODY_MAX_SECONDS", UPLOAD_DEADLINE)


@contextmanager
def _serve(app) -> Iterator[tuple[uvicorn.Server, int]]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        _wait_until(lambda: server.started, "server start")
        yield server, sock.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(WAIT)
        sock.close()


def _wait_until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


def _idle(server: uvicorn.Server) -> bool:
    return not server.server_state.connections and not server.server_state.tasks


def _connect(port: int) -> socket.socket:
    return socket.create_connection(("127.0.0.1", port), timeout=WAIT)


def _start_post(
    port: int, path: str, *, declared: int, body: bytes, headers: bytes = b""
) -> socket.socket:
    conn = _connect(port)
    conn.sendall(
        f"POST {path} HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\n".encode()
        + f"Content-Length: {declared}\r\n".encode()
        + headers
        + b"\r\n"
        + body
    )
    return conn


def _read_to_eof(conn: socket.socket) -> bytes:
    """Everything until the server closes; a socket timeout fails the test."""
    data = b""
    while chunk := conn.recv(65536):
        data += chunk
    return data


def _status(response: bytes) -> int:
    return int(response.split(b" ", 2)[1])


def _json_body(response: bytes) -> dict:
    return json.loads(response.split(b"\r\n\r\n", 1)[1])


def _get(port: int, path: str) -> bytes:
    conn = _connect(port)
    try:
        conn.sendall(f"GET {path} HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n".encode())
        return _read_to_eof(conn)
    finally:
        conn.close()


def test_a_stalled_body_gets_a_408_and_the_connection_is_closed() -> None:
    origin = settings.CORS_ORIGINS[0]
    with _serve(real_app) as (server, port):
        conn = _start_post(
            port,
            "/api/v1/auth/login",
            declared=500,
            body=b'{"email": ',
            headers=f"Origin: {origin}\r\n".encode(),
        )
        try:
            started = time.monotonic()
            response = _read_to_eof(conn)
            elapsed = time.monotonic() - started
        finally:
            conn.close()

        assert _status(response) == 408
        assert _json_body(response)["error"]["code"] == "REQUEST_TIMEOUT"
        assert f"access-control-allow-origin: {origin}".encode() in response.lower()
        assert IDLE * 0.9 <= elapsed < WAIT
        _wait_until(lambda: _idle(server), "the connection and its tasks to be released")


def test_a_sender_that_trickles_is_cut_off_at_the_deadline() -> None:
    """Sending a byte just inside the idle timeout must not keep a body open forever."""
    stop = threading.Event()

    def trickle(conn: socket.socket) -> None:
        while not stop.wait(IDLE / 3):
            try:
                conn.sendall(b" ")
            except OSError:
                return

    with _serve(real_app) as (server, port):
        conn = _start_post(port, "/api/v1/auth/login", declared=100_000, body=b"{")
        sender = threading.Thread(target=trickle, args=(conn,), daemon=True)
        sender.start()
        try:
            started = time.monotonic()
            response = _read_to_eof(conn)
            elapsed = time.monotonic() - started
        finally:
            stop.set()
            sender.join(WAIT)
            conn.close()

        assert _status(response) == 408
        assert DEADLINE * 0.9 <= elapsed < WAIT
        _wait_until(lambda: _idle(server), "the connection and its tasks to be released")


def test_a_client_that_disconnects_mid_body_is_cleaned_up() -> None:
    with _serve(real_app) as (server, port):
        conn = _start_post(port, "/api/v1/auth/login", declared=500, body=b'{"email": ')
        _wait_until(lambda: server.server_state.tasks, "the request to start")
        conn.close()

        _wait_until(lambda: _idle(server), "the connection and its tasks to be released")
        assert _status(_get(port, "/no-such-route")) == 404


def test_stalled_clients_do_not_hold_up_other_requests_and_are_all_released() -> None:
    with _serve(real_app) as (server, port):
        stalled = [
            _start_post(port, "/api/v1/auth/login", declared=500, body=b"{") for _ in range(20)
        ]
        try:
            _wait_until(lambda: len(server.server_state.connections) >= 20, "20 connections")

            started = time.monotonic()
            assert _status(_get(port, "/no-such-route")) == 404
            assert time.monotonic() - started < IDLE  # answered while they are still stalled

            assert all(_status(_read_to_eof(conn)) == 408 for conn in stalled)
        finally:
            for conn in stalled:
                conn.close()

        _wait_until(lambda: _idle(server), "every connection and task to be released")
        assert _status(_get(port, "/no-such-route")) == 404


def test_a_complete_small_body_is_unaffected() -> None:
    with _serve(real_app) as (_, port):
        body = b'{"email": "nobody@example.com", "password": "wrong-password"}'
        conn = _start_post(
            port,
            "/api/v1/auth/login",
            declared=len(body),
            body=body,
            headers=b"Connection: close\r\n",
        )
        try:
            response = _read_to_eof(conn)
        finally:
            conn.close()

    assert _status(response) in (401, 422)


def test_a_declared_oversized_body_is_still_a_413() -> None:
    with _serve(real_app) as (_, port):
        conn = _start_post(port, "/api/v1/auth/login", declared=10 * 1024 * 1024, body=b"{")
        try:
            response = _read_to_eof(conn)
        finally:
            conn.close()

    assert _status(response) == 413
    assert _json_body(response)["error"]["code"] == "REQUEST_TOO_LARGE"


def _streaming_app() -> FastAPI:
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    @app.post("/stream")
    async def stream(request: Request) -> StreamingResponse:
        await request.body()

        async def events():
            yield b"first\n"
            # Longer than the idle timeout and the deadline: a streamed answer may go
            # quiet once the request body is in.
            await asyncio.sleep(DEADLINE + IDLE)
            yield b"second\n"

        return StreamingResponse(events(), media_type="text/plain")

    # Same path as the real upload route, so it gets the upload cap and deadline.
    @app.post(UPLOAD_PATH)
    async def upload(file: UploadFile = File(...)) -> dict[str, int]:
        return {"bytes": len(await file.read())}

    @app.middleware("http")
    async def passthrough(request: Request, call_next):
        # BaseHTTPMiddleware, as in app.main: it waits on receive() for a disconnect.
        return await call_next(request)

    app.add_middleware(RequestBodyLimitMiddleware)
    return app


def test_a_streamed_response_may_go_quiet_after_the_body_is_read() -> None:
    with _serve(_streaming_app()) as (_, port):
        conn = _start_post(
            port, "/stream", declared=2, body=b"{}", headers=b"Connection: close\r\n"
        )
        try:
            response = _read_to_eof(conn)
        finally:
            conn.close()

    assert _status(response) == 200
    assert b"first" in response and b"second" in response


def test_a_chunked_body_within_the_cap_is_unaffected() -> None:
    with _serve(_streaming_app()) as (_, port):
        conn = _connect(port)
        try:
            conn.sendall(
                b"POST /echo HTTP/1.1\r\nHost: test\r\nTransfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
            )
            response = _read_to_eof(conn)
        finally:
            conn.close()

    assert _status(response) == 200
    assert _json_body(response) == {"bytes": 11}


def test_a_stalled_chunked_body_gets_a_408() -> None:
    with _serve(_streaming_app()) as (_, port):
        conn = _connect(port)
        try:
            conn.sendall(
                b"POST /echo HTTP/1.1\r\nHost: test\r\nTransfer-Encoding: chunked\r\n\r\n"
                b"5\r\nhello\r\n"
            )
            response = _read_to_eof(conn)
        finally:
            conn.close()

    assert _status(response) == 408


def _open_deleted_files() -> int:
    """Spooled upload parts are unlinked temp files: open but deleted."""
    count = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{fd}").endswith("(deleted)"):
                count += 1
        except OSError:
            pass
    return count


def _multipart_head(declared_file: int) -> tuple[bytes, int]:
    part = (
        b'--B\r\nContent-Disposition: form-data; name="file"; filename="s.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
    )
    tail = b"\r\n--B--\r\n"
    return part, len(part) + declared_file + len(tail)


def _upload_headers(total: int, extra: bytes = b"") -> bytes:
    return (
        f"POST {UPLOAD_PATH} HTTP/1.1\r\nHost: test\r\n".encode()
        + b"Content-Type: multipart/form-data; boundary=B\r\n"
        + f"Content-Length: {total}\r\n".encode()
        + extra
        + b"\r\n"
    )


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs Linux /proc")
def test_a_stalled_upload_gets_a_408_and_leaves_no_temp_file() -> None:
    """Over a megabyte is spooled to a temp file before the stall: it must be closed."""
    with _serve(_streaming_app()) as (server, port):
        before = _open_deleted_files()
        part, total = _multipart_head(5 * 1024 * 1024)
        conn = _connect(port)
        conn.sendall(_upload_headers(total) + part + b"a" * 2 * 1024 * 1024)
        try:
            response = _read_to_eof(conn)
        finally:
            conn.close()

        assert _status(response) == 408
        _wait_until(lambda: _idle(server), "the connection and its tasks to be released")
        _wait_until(lambda: _open_deleted_files() <= before, "spooled temp files to be closed")


def test_an_upload_gets_its_own_longer_deadline() -> None:
    """Trickling past the default deadline is fine on the upload route, within its own."""
    size = 12
    with _serve(_streaming_app()) as (_, port):
        part, total = _multipart_head(size)
        conn = _connect(port)
        conn.sendall(_upload_headers(total, b"Connection: close\r\n") + part)
        started = time.monotonic()
        try:
            for _ in range(size):  # one byte every IDLE/2: 13 * 0.15 s, about 1.3x DEADLINE
                time.sleep(IDLE / 2)
                conn.sendall(b"a")
            time.sleep(IDLE / 2)
            conn.sendall(b"\r\n--B--\r\n")
            response = _read_to_eof(conn)
        finally:
            conn.close()
        elapsed = time.monotonic() - started

    assert DEADLINE < elapsed < UPLOAD_DEADLINE
    assert _status(response) == 200
    assert _json_body(response) == {"bytes": size}


def test_a_slow_chunked_body_that_keeps_arriving_succeeds() -> None:
    with _serve(_streaming_app()) as (_, port):
        conn = _connect(port)
        try:
            conn.sendall(
                b"POST /echo HTTP/1.1\r\nHost: test\r\nTransfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n"
            )
            for _ in range(3):  # gaps under the idle timeout, total under the deadline
                time.sleep(IDLE / 2)
                conn.sendall(b"2\r\nab\r\n")
            conn.sendall(b"0\r\n\r\n")
            response = _read_to_eof(conn)
        finally:
            conn.close()

    assert _status(response) == 200
    assert _json_body(response) == {"bytes": 6}


def test_repeated_stalls_and_reconnects_leave_the_server_healthy() -> None:
    with _serve(real_app) as (server, port):
        for _ in range(10):
            conn = _start_post(port, "/api/v1/auth/login", declared=500, body=b"{")
            try:
                assert _status(_read_to_eof(conn)) == 408
            finally:
                conn.close()
        _wait_until(lambda: _idle(server), "every connection and task to be released")
        assert _status(_get(port, "/no-such-route")) == 404


def test_the_rest_of_the_body_arriving_after_the_408_is_harmless() -> None:
    """The race where a slow sender resumes just after the timeout fired."""
    with _serve(real_app) as (server, port):
        conn = _start_post(port, "/api/v1/auth/login", declared=500, body=b"{")
        try:
            assert _status(_read_to_eof(conn)) == 408
            with contextlib.suppress(OSError):  # the server may already have closed
                conn.sendall(b'"email": "late"}' + b" " * 480)
        finally:
            conn.close()
        _wait_until(lambda: _idle(server), "the connection and its tasks to be released")
        assert _status(_get(port, "/no-such-route")) == 404
