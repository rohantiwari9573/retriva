"""The real app caps upload bodies before parsing them, and still accepts the
largest permitted document."""

import uuid
from collections.abc import AsyncIterator

from httpx import AsyncClient

from app.core.config import settings
from app.core.request_size import max_request_body_bytes
from tests.integration.test_documents import _register_and_create_org


async def _new_org(client: AsyncClient) -> str:
    email = f"size-{uuid.uuid4().hex[:8]}@example.com"
    return (await _register_and_create_org(client, email, "Size"))["id"]


async def test_the_largest_permitted_document_is_accepted(client: AsyncClient) -> None:
    """A file of exactly MAX_DOCUMENT_SIZE_MB plus multipart framing fits under the cap."""
    org_id = await _new_org(client)
    size = settings.MAX_DOCUMENT_SIZE_MB * 1024 * 1024
    content = b"largest permitted document" + b"a" * (size - 26)
    assert len(content) == size

    response = await client.post(
        f"/api/v1/organizations/{org_id}/documents",
        files={"file": ("largest.txt", content, "text/plain")},
    )

    assert response.status_code == 201, response.text


async def test_declared_oversized_upload_is_refused_with_cors_headers(
    client: AsyncClient,
) -> None:
    org_id = await _new_org(client)
    origin = settings.CORS_ORIGINS[0]

    response = await client.post(
        f"/api/v1/organizations/{org_id}/documents",
        content=b"x",
        headers={
            "content-type": "multipart/form-data; boundary=B",
            "content-length": str(max_request_body_bytes() + 1),
            "origin": origin,
        },
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert response.headers.get("access-control-allow-origin") == origin


async def test_streamed_oversized_upload_is_refused(client: AsyncClient) -> None:
    org_id = await _new_org(client)
    head = (
        b'--B\r\nContent-Disposition: form-data; name="file"; filename="big.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
    )
    total = max_request_body_bytes() + 1024 * 1024

    async def body() -> AsyncIterator[bytes]:
        yield head
        chunk = b"a" * (1024 * 1024)
        for _ in range(total // len(chunk)):
            yield chunk
        yield b"\r\n--B--\r\n"

    origin = settings.CORS_ORIGINS[0]

    response = await client.post(
        f"/api/v1/organizations/{org_id}/documents",
        content=body(),
        headers={"content-type": "multipart/form-data; boundary=B", "origin": origin},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert response.headers.get("access-control-allow-origin") == origin


async def test_ordinary_requests_are_unaffected(client: AsyncClient) -> None:
    org_id = await _new_org(client)

    response = await client.post(
        f"/api/v1/organizations/{org_id}/documents",
        files={"file": ("notes.txt", b"plain small upload body", "text/plain")},
    )

    assert response.status_code == 201


async def test_large_json_bodies_are_refused_before_auth(client: AsyncClient) -> None:
    """FastAPI reads JSON into memory before any dependency; the cap must stop it."""
    from app.core.request_size import DEFAULT_MAX_BODY_BYTES

    padding = "a" * (DEFAULT_MAX_BODY_BYTES + 1)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": padding},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"


async def test_streamed_large_json_is_refused(client: AsyncClient) -> None:
    from app.core.request_size import DEFAULT_MAX_BODY_BYTES

    async def body() -> AsyncIterator[bytes]:
        yield b'{"email": "nobody@example.com", "password": "'
        for _ in range(DEFAULT_MAX_BODY_BYTES // 65536 + 2):
            yield b"a" * 65536
        yield b'"}'

    response = await client.post(
        "/api/v1/auth/login", content=body(), headers={"content-type": "application/json"}
    )

    assert response.status_code == 413


async def test_ordinary_json_under_the_default_cap_is_unaffected(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "a" * 4000},
    )

    assert response.status_code in (401, 422)
