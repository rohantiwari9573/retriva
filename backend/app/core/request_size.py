"""Caps request bodies before anything buffers them.

The 25 MB document limit (DocumentService._read_and_hash) only runs after Starlette
has parsed the whole multipart body, spooling file parts to a temporary file with no
size cap. Without this, any signed-in user could make the API write an arbitrarily
large body to disk before getting a 413, and a body cut short by a proxy would leave
the request waiting for bytes that never arrive.

The cap is the document limit plus room for multipart framing, so every valid upload
still fits. A declared Content-Length over it is refused before the body is read; a
body without one (chunked) is counted as it arrives and refused once it passes.

Before the 413 is sent, the rest of the body is read and discarded, never stored, up
to a bound in bytes and time. Closing with unread data in the socket makes the
client's TCP stack reset the connection, and a reset discards the 413 before the
client reads it. It has to happen first: once a response is complete, uvicorn's
receive() reports a disconnect instead of more body.
"""

import asyncio
import json
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings

# Multipart boundaries and part headers add well under this.
MULTIPART_OVERHEAD_BYTES = 1024 * 1024
# How much of a refused body is read and thrown away so the client sees the 413.
DRAIN_MAX_BYTES = 64 * 1024 * 1024
DRAIN_MAX_SECONDS = 15.0
# A sender that goes quiet won't finish: a proxy that truncated the body (Next.js
# past proxyClientMaxBodySize) keeps the original Content-Length and stops sending.
DRAIN_IDLE_SECONDS = 2.0


def max_request_body_bytes() -> int:
    return settings.MAX_DOCUMENT_SIZE_MB * 1024 * 1024 + MULTIPART_OVERHEAD_BYTES


class _BodyTooLarge(Exception):
    pass


async def _send_too_large(send: Send) -> None:
    body = json.dumps(
        {
            "error": {
                "code": "FILE_TOO_LARGE",
                "message": f"File exceeds the maximum size of {settings.MAX_DOCUMENT_SIZE_MB} MB.",
            }
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _drain(receive: Receive, already_received: bool) -> None:
    """Discards the rest of a refused body, within DRAIN_MAX_BYTES/SECONDS."""
    if already_received:
        return
    drained = 0
    deadline = time.monotonic() + DRAIN_MAX_SECONDS
    while drained <= DRAIN_MAX_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            message = await asyncio.wait_for(receive(), timeout=min(remaining, DRAIN_IDLE_SECONDS))
        except (TimeoutError, OSError):
            return
        if message["type"] != "http.request":
            return
        drained += len(message.get("body", b""))
        if not message.get("more_body", False):
            return


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int | None = None) -> None:
        self.app = app
        self._max_bytes = max_bytes

    @property
    def max_bytes(self) -> int:
        # Read per request, so it always follows the current settings.
        return self._max_bytes if self._max_bytes is not None else max_request_body_bytes()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        max_bytes = self.max_bytes
        headers = dict(scope.get("headers", []))

        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                declared: int | None = int(raw_length)
            except ValueError:
                declared = None  # malformed: the server and app reject it as before
            if declared is not None and declared > max_bytes:
                # Draining is only worth it when the client will then read the 413:
                # not when it is waiting for "100 Continue" (reading would invite the
                # body it hasn't sent), and not when the body is too large to drain
                # within the bound (the connection resets regardless).
                expects_continue = headers.get(b"expect", b"").lower() == b"100-continue"
                if not expects_continue and declared <= DRAIN_MAX_BYTES:
                    await _drain(receive, already_received=False)
                await _send_too_large(send)
                return

        received = 0
        exceeded = False
        body_complete = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded, body_complete
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                body_complete = not message.get("more_body", False)
                if received > max_bytes:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            # Once the body is over the cap, whatever the app makes of the error
            # (FastAPI turns it into a 400) is replaced by the 413 below.
            if exceeded:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        # If the app had already started a response before the body went over the
        # cap, the rest of that response is dropped and no 413 can be sent; no route
        # reads its body after responding, so this is a guard, not a path in use.
        if exceeded and not response_started:
            await _drain(receive, already_received=body_complete)
            await _send_too_large(send)
