"""R0's login limits and the request-body limits from PR #3, together.

The body cap runs before routing, so a refused body never reaches the login route and
must not spend anyone's per-address or per-account login budget. Both must also keep
working while Redis is down, when the limits fall back to per-process counters.
"""

import uuid

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.core.request_size import DEFAULT_MAX_BODY_BYTES
from tests.integration.test_login_abuse import PASSWORD, WRONG, _account, _login, _signed

ATTACKER_IP = "198.51.100.77"


@pytest.fixture(autouse=True)
def _edge_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.integration.test_login_abuse import SECRET

    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", SECRET)
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_ACCOUNT_DELAY_BASE_SECONDS", 0.0)


async def _oversized_login(client: AsyncClient, email: str, ip: str) -> int:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "a" * (DEFAULT_MAX_BODY_BYTES + 1)},
        headers=_signed(ip),
    )
    client.cookies.clear()
    return response.status_code


async def test_refused_bodies_do_not_spend_the_address_login_budget(client: AsyncClient) -> None:
    email = await _account(client)
    for _ in range(settings.RATE_LIMIT_LOGIN_PER_MINUTE + 3):
        assert await _oversized_login(client, email, ATTACKER_IP) == 413

    # The full per-address budget is still there for ordinary attempts.
    for _ in range(settings.RATE_LIMIT_LOGIN_PER_MINUTE):
        assert await _login(client, email, WRONG, ATTACKER_IP) == 401
    assert await _login(client, email, WRONG, ATTACKER_IP) == 429


async def test_refused_bodies_do_not_spend_the_account_failure_budget(
    client: AsyncClient,
) -> None:
    """Oversized requests from many addresses must not lock the owner out."""
    email = await _account(client)
    for i in range(settings.RATE_LIMIT_LOGIN_ACCOUNT_FAILURES_PER_MINUTE + 5):
        assert await _oversized_login(client, email, f"203.0.113.{i + 1}") == 413

    assert await _login(client, email, PASSWORD, "192.0.2.10") == 200


async def test_body_cap_holds_while_redis_is_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = await _account(client)
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")

    assert await _oversized_login(client, email, ATTACKER_IP) == 413
    assert await _login(client, email, PASSWORD, f"192.0.2.{uuid.uuid4().int % 200 + 20}") == 200
