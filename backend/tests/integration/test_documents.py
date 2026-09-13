"""Document upload/list/get/download/delete: RBAC, cross-tenant isolation,
and file-safety checks against the real API (storage is the in-memory fake -
see conftest.fake_storage)."""

import pytest

PASSWORD = "correct-horse-99"
TXT_BYTES = b"Employees are entitled to 24 annual leave days."


async def _register_and_create_org(client, email: str, org_name: str):
    await client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    org = (await client.post("/api/v1/organizations", json={"name": org_name})).json()
    return org


async def _login(client, email: str):
    client.cookies.clear()
    await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})


def _upload_files(filename: str = "handbook.txt", content: bytes = TXT_BYTES):
    return {"file": (filename, content, "text/plain")}


async def test_upload_document_success(client):
    org = await _register_and_create_org(client, "owner1@example.com", "Org A")
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/documents", files=_upload_files()
    )
    assert response.status_code == 201
    body = response.json()
    assert body["original_filename"] == "handbook.txt"
    assert body["status"] == "PROCESSING"
    assert body["mime_type"] == "text/plain"


async def test_uploaded_object_actually_stored(client, fake_storage):
    org = await _register_and_create_org(client, "owner2@example.com", "Org B")
    await client.post(f"/api/v1/organizations/{org['id']}/documents", files=_upload_files())
    assert len(fake_storage.objects) == 1
    stored_key = next(iter(fake_storage.objects))
    assert stored_key.startswith(f"organizations/{org['id']}/documents/")
    assert fake_storage.objects[stored_key] == TXT_BYTES


async def test_list_documents(client):
    org = await _register_and_create_org(client, "owner3@example.com", "Org C")
    await client.post(f"/api/v1/organizations/{org['id']}/documents", files=_upload_files())
    response = await client.get(f"/api/v1/organizations/{org['id']}/documents")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1


async def test_get_document_detail(client):
    org = await _register_and_create_org(client, "owner4@example.com", "Org D")
    doc = (
        await client.post(f"/api/v1/organizations/{org['id']}/documents", files=_upload_files())
    ).json()
    response = await client.get(f"/api/v1/organizations/{org['id']}/documents/{doc['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == doc["id"]


async def test_download_url_generated_after_role_check(client):
    org = await _register_and_create_org(client, "owner5@example.com", "Org E")
    doc = (
        await client.post(f"/api/v1/organizations/{org['id']}/documents", files=_upload_files())
    ).json()
    response = await client.get(
        f"/api/v1/organizations/{org['id']}/documents/{doc['id']}/download"
    )
    assert response.status_code == 200
    body = response.json()
    assert "url" in body
    assert body["expires_in"] > 0


async def test_delete_document_removes_row_and_storage_object(client, fake_storage):
    org = await _register_and_create_org(client, "owner6@example.com", "Org F")
    doc = (
        await client.post(f"/api/v1/organizations/{org['id']}/documents", files=_upload_files())
    ).json()
    assert len(fake_storage.objects) == 1

    response = await client.delete(f"/api/v1/organizations/{org['id']}/documents/{doc['id']}")
    assert response.status_code == 204
    assert len(fake_storage.objects) == 0

    get_response = await client.get(f"/api/v1/organizations/{org['id']}/documents/{doc['id']}")
    assert get_response.status_code == 404


async def test_duplicate_content_race_maps_to_409_not_500(client, monkeypatch):
    """The get_by_content_hash pre-check in DocumentService.upload() is
    itself a TOCTOU race - two concurrent uploads of the same content can
    both pass it before either commits. Simulates the race by forcing the
    pre-check to always report "no existing document", so the real guard,
    the uq_document_org_content_hash DB constraint, is what actually catches
    the second upload - verifying that path is mapped to 409, not left as an
    unhandled IntegrityError surfacing as a 500."""
    from app.repositories.document_repository import DocumentRepository

    async def _always_none(self, org_id, content_hash):
        return None

    monkeypatch.setattr(DocumentRepository, "get_by_content_hash", _always_none)

    org = await _register_and_create_org(client, "owner16@example.com", "Org P")
    first = await client.post(
        f"/api/v1/organizations/{org['id']}/documents", files=_upload_files("a16.txt")
    )
    assert first.status_code == 201

    second = await client.post(
        f"/api/v1/organizations/{org['id']}/documents", files=_upload_files("b16.txt")
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "DUPLICATE_DOCUMENT"


async def test_unrelated_integrity_error_is_not_reported_as_duplicate(client, monkeypatch):
    """A NOT NULL/other constraint violation on the create() flush must not
    be misreported as DUPLICATE_DOCUMENT just because it's an IntegrityError
    - only the specific uq_document_org_content_hash constraint should map
    to that 409; anything else re-raises (surfacing as a generic 500,
    matching the project's existing "unexpected exception -> 500" contract,
    not a misleading duplicate-content message)."""
    from sqlalchemy.exc import IntegrityError

    from app.repositories.document_repository import DocumentRepository

    async def _boom(self, **kwargs):
        raise IntegrityError(
            "INSERT INTO documents ...", {}, Exception("null value in column x")
        )

    monkeypatch.setattr(DocumentRepository, "create", _boom)
    org = await _register_and_create_org(client, "owner16b@example.com", "Org P2")

    response = await client.post(
        f"/api/v1/organizations/{org['id']}/documents", files=_upload_files()
    )
    assert response.status_code == 500


async def test_duplicate_content_rejected(client):
    org = await _register_and_create_org(client, "owner7@example.com", "Org G")
    first = await client.post(
        f"/api/v1/organizations/{org['id']}/documents", files=_upload_files("a.txt")
    )
    assert first.status_code == 201
    duplicate = await client.post(
        f"/api/v1/organizations/{org['id']}/documents", files=_upload_files("b.txt")
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "DUPLICATE_DOCUMENT"


async def test_storage_failure_marks_document_failed_and_persists_despite_rollback(
    client, fake_storage, monkeypatch
):
    """Exercises the exact bug the advisor flagged during planning: a route
    exception normally rolls back the whole request via get_db, which would
    silently erase the FAILED status too unless it's committed immediately -
    mirrors the fix already applied to AuthService.refresh's reuse-detection
    path."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated MinIO outage")

    monkeypatch.setattr(fake_storage, "upload", _boom)

    org = await _register_and_create_org(client, "owner15@example.com", "Org O")
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/documents", files=_upload_files()
    )
    assert response.status_code == 500  # unhandled RuntimeError -> generic 500

    list_response = await client.get(f"/api/v1/organizations/{org['id']}/documents")
    body = list_response.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "FAILED"


async def test_oversized_file_rejected(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "MAX_DOCUMENT_SIZE_MB", 0)  # anything nonempty exceeds 0 MB
    org = await _register_and_create_org(client, "owner8@example.com", "Org H")
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/documents", files=_upload_files()
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


async def test_unsupported_file_type_rejected(client):
    org = await _register_and_create_org(client, "owner9@example.com", "Org I")
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/documents",
        files={"file": ("malware.exe", b"MZ\x90\x00", "application/octet-stream")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"


async def test_extension_content_mismatch_rejected(client):
    org = await _register_and_create_org(client, "owner10@example.com", "Org J")
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/documents",
        files={"file": ("fake.pdf", b"not actually a pdf", "application/pdf")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"


async def test_path_traversal_filename_does_not_escape_storage_prefix(client, fake_storage):
    org = await _register_and_create_org(client, "owner11@example.com", "Org K")
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/documents",
        files=_upload_files("../../etc/passwd.txt"),
    )
    assert response.status_code == 201
    stored_key = next(iter(fake_storage.objects))
    assert stored_key.startswith(f"organizations/{org['id']}/documents/")
    assert ".." not in stored_key


async def test_viewer_cannot_upload(client):
    org = await _register_and_create_org(client, "owner12@example.com", "Org L")
    await client.post(
        "/api/v1/auth/register", json={"email": "viewer12@example.com", "password": PASSWORD}
    )
    await _login(client, "owner12@example.com")
    await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "viewer12@example.com", "role": "VIEWER"},
    )

    await _login(client, "viewer12@example.com")
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/documents", files=_upload_files()
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


async def test_viewer_can_view_and_download(client):
    org = await _register_and_create_org(client, "owner13@example.com", "Org M")
    doc = (
        await client.post(f"/api/v1/organizations/{org['id']}/documents", files=_upload_files())
    ).json()
    await client.post(
        "/api/v1/auth/register", json={"email": "viewer13@example.com", "password": PASSWORD}
    )
    await _login(client, "owner13@example.com")
    await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "viewer13@example.com", "role": "VIEWER"},
    )

    await _login(client, "viewer13@example.com")
    list_response = await client.get(f"/api/v1/organizations/{org['id']}/documents")
    assert list_response.status_code == 200
    download_response = await client.get(
        f"/api/v1/organizations/{org['id']}/documents/{doc['id']}/download"
    )
    assert download_response.status_code == 200


async def test_member_cannot_delete(client):
    org = await _register_and_create_org(client, "owner14@example.com", "Org N")
    doc = (
        await client.post(f"/api/v1/organizations/{org['id']}/documents", files=_upload_files())
    ).json()
    await client.post(
        "/api/v1/auth/register", json={"email": "member14@example.com", "password": PASSWORD}
    )
    await _login(client, "owner14@example.com")
    await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "member14@example.com", "role": "MEMBER"},
    )

    await _login(client, "member14@example.com")
    response = await client.delete(f"/api/v1/organizations/{org['id']}/documents/{doc['id']}")
    assert response.status_code == 403


async def test_cross_tenant_cannot_view_documents(client):
    org_a = await _register_and_create_org(client, "a-owner@example.com", "Org Alpha")
    doc = (
        await client.post(f"/api/v1/organizations/{org_a['id']}/documents", files=_upload_files())
    ).json()

    await _register_and_create_org(client, "b-owner@example.com", "Org Beta")
    await _login(client, "b-owner@example.com")

    list_response = await client.get(f"/api/v1/organizations/{org_a['id']}/documents")
    assert list_response.status_code == 404

    get_response = await client.get(
        f"/api/v1/organizations/{org_a['id']}/documents/{doc['id']}"
    )
    assert get_response.status_code == 404

    download_response = await client.get(
        f"/api/v1/organizations/{org_a['id']}/documents/{doc['id']}/download"
    )
    assert download_response.status_code == 404

    delete_response = await client.delete(
        f"/api/v1/organizations/{org_a['id']}/documents/{doc['id']}"
    )
    assert delete_response.status_code == 404


async def test_cross_tenant_cannot_upload(client):
    org_a = await _register_and_create_org(client, "a-owner2@example.com", "Org Alpha 2")
    await _register_and_create_org(client, "b-owner2@example.com", "Org Beta 2")
    await _login(client, "b-owner2@example.com")

    response = await client.post(
        f"/api/v1/organizations/{org_a['id']}/documents", files=_upload_files()
    )
    assert response.status_code == 404


async def _upload_and_fail(client, org_id: str, fake_storage, monkeypatch, filename: str):
    """Drives a document into FAILED via the real upload flow (storage
    raises), matching test_storage_failure_marks_document_failed_and_persists
    - the only way FAILED is legitimately reached without directly poking
    the DB."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated MinIO outage")

    monkeypatch.setattr(fake_storage, "upload", _boom)
    await client.post(
        f"/api/v1/organizations/{org_id}/documents", files=_upload_files(filename)
    )
    monkeypatch.undo()  # restore real upload for any subsequent calls in the test

    list_response = await client.get(f"/api/v1/organizations/{org_id}/documents")
    doc = list_response.json()["items"][0]
    assert doc["status"] == "FAILED"
    return doc


async def test_retry_failed_document_succeeds(client, fake_storage, monkeypatch):
    org = await _register_and_create_org(client, "owner17@example.com", "Org Q")
    doc = await _upload_and_fail(client, org["id"], fake_storage, monkeypatch, "retry17.txt")

    response = await client.post(
        f"/api/v1/organizations/{org['id']}/documents/{doc['id']}/retry"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PROCESSING"
    assert body["failure_reason"] is None


async def test_retry_non_failed_document_rejected(client):
    org = await _register_and_create_org(client, "owner18@example.com", "Org R")
    doc = (
        await client.post(f"/api/v1/organizations/{org['id']}/documents", files=_upload_files())
    ).json()
    assert doc["status"] == "PROCESSING"

    response = await client.post(
        f"/api/v1/organizations/{org['id']}/documents/{doc['id']}/retry"
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATUS_TRANSITION"


async def test_cross_tenant_cannot_retry(client, fake_storage, monkeypatch):
    org_a = await _register_and_create_org(client, "a-owner3@example.com", "Org Alpha 3")
    doc = await _upload_and_fail(client, org_a["id"], fake_storage, monkeypatch, "retry-a.txt")

    await _register_and_create_org(client, "b-owner3@example.com", "Org Beta 3")
    await _login(client, "b-owner3@example.com")

    response = await client.post(
        f"/api/v1/organizations/{org_a['id']}/documents/{doc['id']}/retry"
    )
    assert response.status_code == 404


async def test_viewer_cannot_retry(client, fake_storage, monkeypatch):
    org = await _register_and_create_org(client, "owner19@example.com", "Org S")
    doc = await _upload_and_fail(client, org["id"], fake_storage, monkeypatch, "retry19.txt")
    await client.post(
        "/api/v1/auth/register", json={"email": "viewer19@example.com", "password": PASSWORD}
    )
    await _login(client, "owner19@example.com")
    await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "viewer19@example.com", "role": "VIEWER"},
    )

    await _login(client, "viewer19@example.com")
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/documents/{doc['id']}/retry"
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


async def test_concurrent_retry_only_one_transitions(db_session, fake_storage):
    """Two callers racing DocumentService.retry() on the same FAILED document:
    only one may win the FAILED->PROCESSING transition. This proves the SQL
    semantics of the atomic conditional UPDATE (TESTED) - it does not, on a
    single shared session/connection, prove behavior under two genuinely
    concurrent database connections (NOT independently verified here)."""
    import uuid

    from app.models.document import Document
    from app.models.enums import DocumentStatus
    from app.models.organization import Organization
    from app.models.user import User
    from app.services.document_service import DocumentService

    org = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    user = User(email=f"{uuid.uuid4().hex}@example.com", hashed_password="x")
    db_session.add_all([org, user])
    await db_session.flush()
    document = Document(
        organization_id=org.id,
        uploaded_by=user.id,
        original_filename="race.txt",
        storage_key=f"organizations/{org.id}/documents/race.txt",
        mime_type="text/plain",
        size_bytes=3,
        content_hash=uuid.uuid4().hex,
        status=DocumentStatus.FAILED,
        failure_reason="boom",
    )
    db_session.add(document)
    await db_session.commit()

    service = DocumentService(db_session, fake_storage)

    first = await service.retry(document)
    assert first.status == DocumentStatus.PROCESSING

    from app.core.exceptions import ConflictError

    with pytest.raises(ConflictError):
        await service.retry(document)


async def test_upload_rate_limited(client):
    # The rate-limit dependency's max_requests is bound to settings at route
    # registration time (Depends(rate_limit_for_user("upload",
    # settings.RATE_LIMIT_UPLOAD_PER_MINUTE))), so monkeypatching the
    # setting after app startup has no effect - exhaust the real configured
    # default instead, same pattern as test_login_rate_limited.
    from app.core.config import settings

    org = await _register_and_create_org(client, "owner20@example.com", "Org T")

    for i in range(settings.RATE_LIMIT_UPLOAD_PER_MINUTE):
        response = await client.post(
            f"/api/v1/organizations/{org['id']}/documents",
            files=_upload_files(f"u{i}.txt", content=f"content {i}".encode()),
        )
        assert response.status_code == 201

    limited = await client.post(
        f"/api/v1/organizations/{org['id']}/documents", files=_upload_files("uover.txt")
    )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


async def test_retry_rate_limited(client, fake_storage, monkeypatch):
    """The rate-limit dependency counts every request regardless of business
    outcome (same as login's wrong-password-still-counts pattern) - a single
    document can only actually transition once, so calls after the first
    return 409, not 200, but still consume the budget."""
    from app.core.config import settings

    org = await _register_and_create_org(client, "owner21@example.com", "Org U")
    doc = await _upload_and_fail(client, org["id"], fake_storage, monkeypatch, "retry21.txt")

    for _ in range(settings.RATE_LIMIT_RETRY_PER_MINUTE):
        response = await client.post(
            f"/api/v1/organizations/{org['id']}/documents/{doc['id']}/retry"
        )
        assert response.status_code in (200, 409)

    limited = await client.post(
        f"/api/v1/organizations/{org['id']}/documents/{doc['id']}/retry"
    )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
