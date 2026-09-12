"""Auth endpoint tests: registration, login, refresh rotation, logout, and the
error paths a real client (or an attacker) will actually hit."""

STRONG_PASSWORD = "correct-horse-99"


async def test_register_success(client):
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "alice@example.com", "password": STRONG_PASSWORD, "full_name": "Alice"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "alice@example.com"
    assert body["full_name"] == "Alice"
    assert "password" not in body
    assert "hashed_password" not in body
    assert "nexus_access_token" in response.cookies
    assert "nexus_refresh_token" in response.cookies


async def test_register_duplicate_email(client):
    payload = {"email": "bob@example.com", "password": STRONG_PASSWORD}
    first = await client.post("/api/v1/auth/register", json=payload)
    assert first.status_code == 201

    second = await client.post("/api/v1/auth/register", json=payload)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"


async def test_register_weak_password_rejected(client):
    response = await client.post(
        "/api/v1/auth/register", json={"email": "weak@example.com", "password": "short"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_login_success(client):
    await client.post(
        "/api/v1/auth/register", json={"email": "carol@example.com", "password": STRONG_PASSWORD}
    )
    client.cookies.clear()

    response = await client.post(
        "/api/v1/auth/login", json={"email": "carol@example.com", "password": STRONG_PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["email"] == "carol@example.com"


async def test_login_invalid_password(client):
    await client.post(
        "/api/v1/auth/register", json={"email": "dave@example.com", "password": STRONG_PASSWORD}
    )
    client.cookies.clear()

    response = await client.post(
        "/api/v1/auth/login", json={"email": "dave@example.com", "password": "wrong-password-1"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_login_nonexistent_user(client):
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": STRONG_PASSWORD},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_protected_endpoint_requires_auth(client):
    response = await client.get("/api/v1/users/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


async def test_protected_endpoint_with_garbage_token(client):
    client.cookies.set("nexus_access_token", "not-a-real-jwt")
    response = await client.get("/api/v1/users/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TOKEN_INVALID"


async def test_current_user_after_login(client):
    await client.post(
        "/api/v1/auth/register", json={"email": "erin@example.com", "password": STRONG_PASSWORD}
    )
    response = await client.get("/api/v1/users/me")
    assert response.status_code == 200
    assert response.json()["email"] == "erin@example.com"


async def test_refresh_rotates_token_and_old_one_becomes_invalid(client):
    await client.post(
        "/api/v1/auth/register", json={"email": "frank@example.com", "password": STRONG_PASSWORD}
    )
    old_refresh_cookie = client.cookies.get("nexus_refresh_token")
    assert old_refresh_cookie is not None

    refresh_response = await client.post("/api/v1/auth/refresh")
    assert refresh_response.status_code == 200
    new_refresh_cookie = client.cookies.get("nexus_refresh_token")
    assert new_refresh_cookie is not None
    assert new_refresh_cookie != old_refresh_cookie

    # Reusing the now-rotated-away-from token must fail, and per the reuse
    # detection in AuthService.refresh, also burns the new one.
    client.cookies.set("nexus_refresh_token", old_refresh_cookie)
    reuse_response = await client.post("/api/v1/auth/refresh")
    assert reuse_response.status_code == 401
    assert reuse_response.json()["error"]["code"] == "TOKEN_REVOKED"

    client.cookies.set("nexus_refresh_token", new_refresh_cookie)
    after_theft_response = await client.post("/api/v1/auth/refresh")
    assert after_theft_response.status_code == 401
    assert after_theft_response.json()["error"]["code"] == "TOKEN_REVOKED"


async def test_refresh_without_cookie(client):
    response = await client.post("/api/v1/auth/refresh")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


async def test_logout_revokes_session(client):
    await client.post(
        "/api/v1/auth/register", json={"email": "grace@example.com", "password": STRONG_PASSWORD}
    )
    issued_refresh_token = client.cookies.get("nexus_refresh_token")
    assert issued_refresh_token is not None

    logout_response = await client.post("/api/v1/auth/logout")
    assert logout_response.status_code == 204
    # The browser cookie itself is cleared by logout.
    assert client.cookies.get("nexus_refresh_token") is None

    # Simulate a stolen cookie being replayed after logout: the raw token is
    # still well-formed, but the server must have revoked it, not just told
    # the browser to forget it.
    client.cookies.set("nexus_refresh_token", issued_refresh_token)
    replay_response = await client.post("/api/v1/auth/refresh")
    assert replay_response.status_code == 401
    assert replay_response.json()["error"]["code"] == "TOKEN_REVOKED"


async def test_update_profile(client):
    await client.post(
        "/api/v1/auth/register", json={"email": "henry@example.com", "password": STRONG_PASSWORD}
    )
    response = await client.patch("/api/v1/users/me", json={"full_name": "Henry Updated"})
    assert response.status_code == 200
    assert response.json()["full_name"] == "Henry Updated"


async def test_login_rate_limited(client):
    payload = {"email": "ratelimit@example.com", "password": "wrong-password-1"}
    from app.core.config import settings

    for _ in range(settings.RATE_LIMIT_LOGIN_PER_MINUTE):
        response = await client.post("/api/v1/auth/login", json=payload)
        assert response.status_code == 401

    limited_response = await client.post("/api/v1/auth/login", json=payload)
    assert limited_response.status_code == 429
    assert limited_response.json()["error"]["code"] == "RATE_LIMITED"
