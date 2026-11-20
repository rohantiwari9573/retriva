"""Auth routes must be reachable only by their exact lowercase paths.

The frontend proxy (which adds the signed client IP that the per-IP login limit keys
on) runs only on /api/v1/auth/*, and the frontend's rewrites are case-sensitive, so
/API/... is never forwarded. A path the rewrite does forward with other casing
(/api/v1/AUTH/login, /api/V1/...) must then not reach the login handler here, or it
would be rate-limited without the proxy's assertion. The API's routing is
case-sensitive; these tests pin that down.
"""

import pytest

LOGIN = {"email": "casing@example.com", "password": "Wrong-password-123"}


@pytest.mark.parametrize(
    "path",
    [
        "/API/v1/auth/login",
        "/Api/v1/auth/login",
        "/api/V1/auth/login",
        "/api/v1/AUTH/login",
        "/api/v1/Auth/login",
        "/api/v1/auth/LOGIN",
        "/api/v1/auth/Login",
        "/API/v1/auth/register",
        "/api/v1/AUTH/refresh",
    ],
)
async def test_auth_routes_with_other_casing_do_not_exist(client, path: str) -> None:
    response = await client.post(path, json=LOGIN)

    assert response.status_code == 404


async def test_the_exact_login_path_reaches_the_login_handler(client) -> None:
    response = await client.post("/api/v1/auth/login", json=LOGIN)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_a_query_string_does_not_change_the_route(client) -> None:
    response = await client.post("/api/v1/auth/login?next=/dashboard", json=LOGIN)

    assert response.status_code == 401
