"""The request body cap must refuse oversized bodies before the app buffers them."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, File, Request, UploadFile
from starlette.types import Message

from app.core.request_size import RequestBodyLimitMiddleware

LIMIT = 1000


def _app(seen: dict[str, int]) -> FastAPI:
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()
        seen["bytes"] = len(body)
        return {"bytes": len(body)}

    @app.post("/upload")
    async def upload(file: UploadFile = File(...)) -> dict[str, int]:
        data = await file.read()
        seen["bytes"] = len(data)
        return {"bytes": len(data)}

    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=LIMIT)
    return app


async def _post(app: FastAPI, path: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(path, **kwargs)


async def _chunks(total: int, size: int = 100) -> AsyncIterator[bytes]:
    sent = 0
    while sent < total:
        piece = b"a" * min(size, total - sent)
        sent += len(piece)
        yield piece


def _assert_too_large(response: httpx.Response) -> None:
    assert response.status_code == 413
    assert response.json() == {
        "error": {
            "code": "FILE_TOO_LARGE",
            "message": response.json()["error"]["message"],
        }
    }
    assert "maximum size" in response.json()["error"]["message"]


async def test_body_at_the_limit_is_accepted() -> None:
    seen: dict[str, int] = {}
    response = await _post(_app(seen), "/echo", content=b"a" * LIMIT)

    assert response.status_code == 200
    assert seen["bytes"] == LIMIT


async def test_declared_length_over_the_limit_is_refused_without_calling_the_app() -> None:
    seen: dict[str, int] = {}
    response = await _post(_app(seen), "/echo", content=b"a" * (LIMIT + 1))

    _assert_too_large(response)
    assert seen == {}


async def test_chunked_body_over_the_limit_is_refused() -> None:
    seen: dict[str, int] = {}
    response = await _post(_app(seen), "/echo", content=_chunks(LIMIT * 5))

    _assert_too_large(response)
    assert seen == {}


async def test_chunked_body_under_the_limit_passes() -> None:
    seen: dict[str, int] = {}
    response = await _post(_app(seen), "/echo", content=_chunks(LIMIT - 1))

    assert response.status_code == 200
    assert seen["bytes"] == LIMIT - 1


async def test_chunked_multipart_upload_over_the_limit_is_a_413_not_a_400() -> None:
    """FastAPI reports a failed body read as 400; the cap must still answer 413."""
    seen: dict[str, int] = {}
    head = (
        b'--B\r\nContent-Disposition: form-data; name="file"; filename="x.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
    )

    async def body() -> AsyncIterator[bytes]:
        yield head
        async for chunk in _chunks(LIMIT * 5):
            yield chunk
        yield b"\r\n--B--\r\n"

    response = await _post(
        _app(seen),
        "/upload",
        content=body(),
        headers={"content-type": "multipart/form-data; boundary=B"},
    )

    _assert_too_large(response)
    assert seen == {}


async def test_malformed_content_length_is_left_to_the_server() -> None:
    seen: dict[str, int] = {}
    app = RequestBodyLimitMiddleware(_app(seen), max_bytes=LIMIT)
    sent: list[Message] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"abc", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/echo",
        "raw_path": b"/echo",
        "query_string": b"",
        "headers": [(b"content-length", b"not-a-number")],
        "http_version": "1.1",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
        "root_path": "",
    }
    await app(scope, receive, send)

    assert sent[0]["status"] == 200


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
async def test_non_http_scopes_pass_through(scope_type: str) -> None:
    called: list[str] = []

    async def inner(scope, receive, send) -> None:
        called.append(scope["type"])

    async def unused_receive() -> Message:
        raise AssertionError("not called")

    async def unused_send(message: Message) -> None:
        raise AssertionError("not called")

    await RequestBodyLimitMiddleware(inner, max_bytes=LIMIT)(
        {"type": scope_type}, unused_receive, unused_send
    )

    assert called == [scope_type]


def _raw_scope(content_length: int | None) -> dict:
    headers = [] if content_length is None else [(b"content-length", str(content_length).encode())]
    return {
        "type": "http",
        "method": "POST",
        "path": "/echo",
        "raw_path": b"/echo",
        "query_string": b"",
        "headers": headers,
        "http_version": "1.1",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
        "root_path": "",
    }


async def _run_raw(
    content_length: int | None,
    chunks: int,
    chunk_size: int = 500,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
):
    """Feeds the middleware a body in chunks; returns (status, chunks consumed)."""
    consumed = 0
    sent: list[Message] = []

    async def receive() -> dict:
        nonlocal consumed
        consumed += 1
        more = consumed < chunks
        return {"type": "http.request", "body": b"a" * chunk_size, "more_body": more}

    async def send(message: Message) -> None:
        # What a real server allows: after the response, no more body is readable.
        sent.append({**message, "consumed_before": consumed})

    scope = _raw_scope(content_length)
    scope["headers"] = scope["headers"] + (extra_headers or [])
    await RequestBodyLimitMiddleware(_app({}), max_bytes=LIMIT)(scope, receive, send)
    assert sent[0]["consumed_before"] == consumed, "the body must be drained before the 413"
    return sent[0]["status"], consumed


async def test_refused_declared_body_is_drained_before_the_413() -> None:
    status, consumed = await _run_raw(content_length=10 * 500, chunks=10)

    assert status == 413
    assert consumed == 10


async def test_refused_chunked_body_is_drained_to_its_end() -> None:
    status, consumed = await _run_raw(content_length=None, chunks=10)

    assert status == 413
    assert consumed == 10


async def test_draining_stops_at_its_byte_bound(monkeypatch) -> None:
    monkeypatch.setattr("app.core.request_size.DRAIN_MAX_BYTES", 2000)

    status, consumed = await _run_raw(content_length=None, chunks=10**6)

    assert status == 413
    # Up to the cap (LIMIT) is read before refusing, then at most the drain bound.
    assert consumed <= (LIMIT + 2000) // 500 + 2


async def test_draining_stops_when_the_sender_goes_quiet(monkeypatch) -> None:
    """A truncating proxy keeps the original Content-Length and then sends nothing."""
    import asyncio

    monkeypatch.setattr("app.core.request_size.DRAIN_IDLE_SECONDS", 0.05)
    sent: list[Message] = []
    calls = 0

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": b"a" * 10, "more_body": True}
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        sent.append(message)

    await asyncio.wait_for(
        RequestBodyLimitMiddleware(_app({}), max_bytes=LIMIT)(
            _raw_scope(LIMIT * 10), receive, send
        ),
        timeout=5,
    )

    assert sent[0]["status"] == 413


async def test_a_client_waiting_for_100_continue_is_not_asked_for_the_body() -> None:
    """Reading would make the server send 100 Continue and invite the upload."""
    status, consumed = await _run_raw(
        content_length=10 * 500, chunks=10, extra_headers=[(b"expect", b"100-continue")]
    )

    assert status == 413
    assert consumed == 0


async def test_a_body_too_large_to_drain_is_refused_without_reading_it(monkeypatch) -> None:
    monkeypatch.setattr("app.core.request_size.DRAIN_MAX_BYTES", 2000)

    status, consumed = await _run_raw(content_length=10**9, chunks=10**6)

    assert status == 413
    assert consumed == 0


async def test_app_errors_on_bodies_within_the_cap_still_propagate() -> None:
    async def failing(scope, receive, send) -> None:
        await receive()
        raise RuntimeError("boom")

    async def receive() -> Message:
        return {"type": "http.request", "body": b"small", "more_body": False}

    async def send(message: Message) -> None:
        raise AssertionError("no response expected")

    with pytest.raises(RuntimeError, match="boom"):
        await RequestBodyLimitMiddleware(failing, max_bytes=LIMIT)(_raw_scope(5), receive, send)
