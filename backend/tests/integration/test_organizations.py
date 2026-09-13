"""Organization CRUD, membership, and role-management tests."""

PASSWORD = "correct-horse-99"


async def _register(client, email: str, full_name: str | None = None):
    payload = {"email": email, "password": PASSWORD}
    if full_name:
        payload["full_name"] = full_name
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201
    return response.json()


async def test_create_organization_makes_creator_owner(client):
    await _register(client, "owner@example.com")
    response = await client.post("/api/v1/organizations", json={"name": "Acme Inc"})
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Acme Inc"
    assert body["role"] == "OWNER"
    assert body["slug"] == "acme-inc"


async def test_duplicate_organization_names_get_unique_slugs(client):
    await _register(client, "owner2@example.com")
    first = await client.post("/api/v1/organizations", json={"name": "Acme Inc"})
    second = await client.post("/api/v1/organizations", json={"name": "Acme Inc"})
    assert first.json()["slug"] != second.json()["slug"]


async def test_list_organizations_only_shows_membership(client):
    await _register(client, "owner3@example.com")
    await client.post("/api/v1/organizations", json={"name": "Org A"})
    await client.post("/api/v1/organizations", json={"name": "Org B"})

    response = await client.get("/api/v1/organizations")
    assert response.status_code == 200
    names = {org["name"] for org in response.json()}
    assert names == {"Org A", "Org B"}


async def test_get_organization_requires_membership(client):
    await _register(client, "owner4@example.com")
    org = (await client.post("/api/v1/organizations", json={"name": "Org C"})).json()

    client.cookies.clear()
    await _register(client, "outsider@example.com")
    response = await client.get(f"/api/v1/organizations/{org['id']}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ORGANIZATION_NOT_FOUND"


async def test_get_nonexistent_organization(client):
    await _register(client, "owner5@example.com")
    fake_id = "00000000-0000-0000-0000-000000000000"
    response = await client.get(f"/api/v1/organizations/{fake_id}")
    assert response.status_code == 404


async def test_add_member_flow_and_role_visibility(client):
    await _register(client, "owner6@example.com")
    org = (await client.post("/api/v1/organizations", json={"name": "Org D"})).json()
    await _register(client, "member6@example.com")

    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login", json={"email": "owner6@example.com", "password": PASSWORD}
    )

    add_response = await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "member6@example.com", "role": "MEMBER"},
    )
    assert add_response.status_code == 201
    assert add_response.json()["role"] == "MEMBER"

    members_response = await client.get(f"/api/v1/organizations/{org['id']}/members")
    emails = {m["email"] for m in members_response.json()}
    assert emails == {"owner6@example.com", "member6@example.com"}


async def test_add_member_requires_existing_account(client):
    await _register(client, "owner7@example.com")
    org = (await client.post("/api/v1/organizations", json={"name": "Org E"})).json()

    response = await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "ghost@example.com", "role": "MEMBER"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


async def test_add_duplicate_member_conflicts(client):
    await _register(client, "owner8@example.com")
    org = (await client.post("/api/v1/organizations", json={"name": "Org F"})).json()
    await _register(client, "member8@example.com")
    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login", json={"email": "owner8@example.com", "password": PASSWORD}
    )

    await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "member8@example.com", "role": "MEMBER"},
    )
    dup = await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "member8@example.com", "role": "MEMBER"},
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "ALREADY_MEMBER"


async def test_viewer_cannot_add_members(client):
    await _register(client, "owner9@example.com")
    org = (await client.post("/api/v1/organizations", json={"name": "Org G"})).json()
    await _register(client, "viewer9@example.com")
    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login", json={"email": "owner9@example.com", "password": PASSWORD}
    )
    await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "viewer9@example.com", "role": "VIEWER"},
    )

    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login", json={"email": "viewer9@example.com", "password": PASSWORD}
    )
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "someone-else@example.com", "role": "MEMBER"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


async def test_member_cannot_update_organization(client):
    await _register(client, "owner10@example.com")
    org = (await client.post("/api/v1/organizations", json={"name": "Org H"})).json()
    await _register(client, "member10@example.com")
    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login", json={"email": "owner10@example.com", "password": PASSWORD}
    )
    await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "member10@example.com", "role": "MEMBER"},
    )

    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login", json={"email": "member10@example.com", "password": PASSWORD}
    )
    response = await client.patch(f"/api/v1/organizations/{org['id']}", json={"name": "Hacked"})
    assert response.status_code == 403


async def test_admin_can_update_organization(client):
    await _register(client, "owner11@example.com")
    org = (await client.post("/api/v1/organizations", json={"name": "Org I"})).json()
    await _register(client, "admin11@example.com")
    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login", json={"email": "owner11@example.com", "password": PASSWORD}
    )
    await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "admin11@example.com", "role": "ADMIN"},
    )

    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login", json={"email": "admin11@example.com", "password": PASSWORD}
    )
    response = await client.patch(f"/api/v1/organizations/{org['id']}", json={"name": "Renamed"})
    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"


async def test_cannot_demote_last_owner(client):
    await _register(client, "owner12@example.com")
    org = (await client.post("/api/v1/organizations", json={"name": "Org J"})).json()
    members = (await client.get(f"/api/v1/organizations/{org['id']}/members")).json()
    owner_membership_id = members[0]["id"]

    response = await client.patch(
        f"/api/v1/organizations/{org['id']}/members/{owner_membership_id}",
        json={"role": "ADMIN"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LAST_OWNER"


async def test_cannot_remove_last_owner(client):
    await _register(client, "owner13@example.com")
    org = (await client.post("/api/v1/organizations", json={"name": "Org K"})).json()
    members = (await client.get(f"/api/v1/organizations/{org['id']}/members")).json()
    owner_membership_id = members[0]["id"]

    response = await client.delete(
        f"/api/v1/organizations/{org['id']}/members/{owner_membership_id}"
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LAST_OWNER"


async def test_admin_cannot_grant_owner_role(client):
    await _register(client, "owner14@example.com")
    org = (await client.post("/api/v1/organizations", json={"name": "Org L"})).json()
    await _register(client, "admin14@example.com")
    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login", json={"email": "owner14@example.com", "password": PASSWORD}
    )
    await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "admin14@example.com", "role": "ADMIN"},
    )
    members = (await client.get(f"/api/v1/organizations/{org['id']}/members")).json()
    admin_membership_id = next(m["id"] for m in members if m["email"] == "admin14@example.com")

    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login", json={"email": "admin14@example.com", "password": PASSWORD}
    )
    # Privilege escalation attempt: an ADMIN tries to grant itself OWNER.
    response = await client.patch(
        f"/api/v1/organizations/{org['id']}/members/{admin_membership_id}",
        json={"role": "OWNER"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


async def test_member_cannot_remove_member(client):
    """MEMBER is below the ADMIN minimum member-management requires - a
    plain member must not be able to remove another member, only view."""
    await _register(client, "owner16@example.com")
    org = (await client.post("/api/v1/organizations", json={"name": "Org N"})).json()
    await _register(client, "member16a@example.com")
    await _register(client, "member16b@example.com")
    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login", json={"email": "owner16@example.com", "password": PASSWORD}
    )
    await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "member16a@example.com", "role": "MEMBER"},
    )
    await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "member16b@example.com", "role": "MEMBER"},
    )
    members = (await client.get(f"/api/v1/organizations/{org['id']}/members")).json()
    target_id = next(m["id"] for m in members if m["email"] == "member16b@example.com")

    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login", json={"email": "member16a@example.com", "password": PASSWORD}
    )
    response = await client.delete(f"/api/v1/organizations/{org['id']}/members/{target_id}")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


async def test_member_cannot_change_member_role(client):
    await _register(client, "owner17@example.com")
    org = (await client.post("/api/v1/organizations", json={"name": "Org O"})).json()
    await _register(client, "member17a@example.com")
    await _register(client, "member17b@example.com")
    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login", json={"email": "owner17@example.com", "password": PASSWORD}
    )
    await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "member17a@example.com", "role": "MEMBER"},
    )
    await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "member17b@example.com", "role": "MEMBER"},
    )
    members = (await client.get(f"/api/v1/organizations/{org['id']}/members")).json()
    target_id = next(m["id"] for m in members if m["email"] == "member17b@example.com")

    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login", json={"email": "member17a@example.com", "password": PASSWORD}
    )
    response = await client.patch(
        f"/api/v1/organizations/{org['id']}/members/{target_id}", json={"role": "ADMIN"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


async def test_remove_member(client):
    await _register(client, "owner15@example.com")
    org = (await client.post("/api/v1/organizations", json={"name": "Org M"})).json()
    await _register(client, "member15@example.com")
    client.cookies.clear()
    await client.post(
        "/api/v1/auth/login", json={"email": "owner15@example.com", "password": PASSWORD}
    )
    await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "member15@example.com", "role": "MEMBER"},
    )
    members = (await client.get(f"/api/v1/organizations/{org['id']}/members")).json()
    member_id = next(m["id"] for m in members if m["email"] == "member15@example.com")

    response = await client.delete(f"/api/v1/organizations/{org['id']}/members/{member_id}")
    assert response.status_code == 204

    remaining = (await client.get(f"/api/v1/organizations/{org['id']}/members")).json()
    assert all(m["id"] != member_id for m in remaining)
