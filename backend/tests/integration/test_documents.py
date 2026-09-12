"""Document upload/list/get/download/delete: RBAC, cross-tenant isolation,
and file-safety checks against the real API (storage is the in-memory fake -
see conftest.fake_storage)."""

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
