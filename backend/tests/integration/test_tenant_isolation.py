"""Cross-tenant isolation: a user in Organization A must never be able to
read or mutate Organization B's data, even by guessing/enumerating UUIDs."""

PASSWORD = "correct-horse-99"


async def _register_and_create_org(client, email: str, org_name: str):
    await client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    org = (await client.post("/api/v1/organizations", json={"name": org_name})).json()
    client.cookies.clear()
    return org


async def test_user_cannot_view_other_orgs_details(client):
    org_a = await _register_and_create_org(client, "a-owner@example.com", "Org Alpha")
    await _register_and_create_org(client, "b-owner@example.com", "Org Beta")

    await client.post(
        "/api/v1/auth/login", json={"email": "b-owner@example.com", "password": PASSWORD}
    )
    response = await client.get(f"/api/v1/organizations/{org_a['id']}")
    assert response.status_code == 404


async def test_user_cannot_list_other_orgs_members(client):
    org_a = await _register_and_create_org(client, "a-owner2@example.com", "Org Alpha 2")
    await _register_and_create_org(client, "b-owner2@example.com", "Org Beta 2")

    await client.post(
        "/api/v1/auth/login", json={"email": "b-owner2@example.com", "password": PASSWORD}
    )
    response = await client.get(f"/api/v1/organizations/{org_a['id']}/members")
    assert response.status_code == 404


async def test_user_cannot_rename_other_orgs(client):
    org_a = await _register_and_create_org(client, "a-owner3@example.com", "Org Alpha 3")
    await _register_and_create_org(client, "b-owner3@example.com", "Org Beta 3")

    await client.post(
        "/api/v1/auth/login", json={"email": "b-owner3@example.com", "password": PASSWORD}
    )
    response = await client.patch(
        f"/api/v1/organizations/{org_a['id']}", json={"name": "Pwned"}
    )
    assert response.status_code == 404


async def test_user_cannot_add_members_to_other_orgs(client):
    org_a = await _register_and_create_org(client, "a-owner4@example.com", "Org Alpha 4")
    await _register_and_create_org(client, "b-owner4@example.com", "Org Beta 4")

    await client.post(
        "/api/v1/auth/login", json={"email": "b-owner4@example.com", "password": PASSWORD}
    )
    response = await client.post(
        f"/api/v1/organizations/{org_a['id']}/members",
        json={"email": "b-owner4@example.com", "role": "OWNER"},
    )
    assert response.status_code == 404


async def test_user_cannot_remove_members_from_other_orgs(client):
    org_a = await _register_and_create_org(client, "a-owner5@example.com", "Org Alpha 5")
    await client.post(
        "/api/v1/auth/login", json={"email": "a-owner5@example.com", "password": PASSWORD}
    )
    members = (await client.get(f"/api/v1/organizations/{org_a['id']}/members")).json()
    target_membership_id = members[0]["id"]
    client.cookies.clear()

    await _register_and_create_org(client, "b-owner5@example.com", "Org Beta 5")
    await client.post(
        "/api/v1/auth/login", json={"email": "b-owner5@example.com", "password": PASSWORD}
    )
    response = await client.delete(
        f"/api/v1/organizations/{org_a['id']}/members/{target_membership_id}"
    )
    assert response.status_code == 404


async def test_member_of_org_a_and_b_only_sees_permitted_org_when_scoped(client):
    """A user who legitimately belongs to both orgs should see A's data when
    acting in A's context and B's data when acting in B's context - but never
    cross-contaminate a single request."""
    org_a = await _register_and_create_org(client, "multi-owner@example.com", "Org Alpha 6")

    await client.post(
        "/api/v1/auth/login", json={"email": "multi-owner@example.com", "password": PASSWORD}
    )
    org_b = (await client.post("/api/v1/organizations", json={"name": "Org Beta 6"})).json()

    orgs_response = await client.get("/api/v1/organizations")
    org_ids = {org["id"] for org in orgs_response.json()}
    assert org_ids == {org_a["id"], org_b["id"]}

    a_detail = await client.get(f"/api/v1/organizations/{org_a['id']}")
    b_detail = await client.get(f"/api/v1/organizations/{org_b['id']}")
    assert a_detail.status_code == 200
    assert b_detail.status_code == 200
    assert a_detail.json()["name"] != b_detail.json()["name"]
