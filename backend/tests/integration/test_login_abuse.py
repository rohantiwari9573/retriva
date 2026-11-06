"""Login throttling against both password guessing and deliberate account lockout,
and the edge-proxy identity through real HTTP routes, including replay and expiry.
"""

import asyncio
import hmac
import time
import uuid
from hashlib import sha256

import pytest
from httpx import AsyncClient

from app.core import client_ip, rate_limit
from app.core.client_ip import SIGNATURE_HEADER, TIMESTAMP_HEADER, VERIFIED_IP_HEADER
from app.core.config import settings

PASSWORD = "Str0ng-Passw0rd!x"
WRONG = "Wrong-Passw0rd!x"
SECRET = "login-abuse-test-secret-at-least-32-chars"


def _signed(ip: str, timestamp: int | None = None, secret: str = SECRET) -> dict[str, str]:
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    signature = hmac.new(secret.encode(), f"{stamp}.{ip}".encode(), sha256).hexdigest()
    return {VERIFIED_IP_HEADER: ip, TIMESTAMP_HEADER: stamp, SIGNATURE_HEADER: signature}


async def _account(client: AsyncClient) -> str:
    email = f"abuse-{uuid.uuid4().hex[:10]}@example.com"
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "full_name": "Abuse Test"},
        headers=_signed("192.0.2.200"),
    )
    assert response.status_code == 201, response.text
    client.cookies.clear()
    return email


async def _login(client: AsyncClient, email: str, password: str, ip: str) -> int:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        headers=_signed(ip),
    )
    client.cookies.clear()
    return response.status_code


@pytest.fixture(autouse=True)
def _edge_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", SECRET)
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_ACCOUNT_DELAY_BASE_SECONDS", 0.0)


# --- deliberate lockout of a known account --------------------------------


async def test_one_attacker_address_cannot_lock_out_the_owner(client: AsyncClient) -> None:
    email = await _account(client)

    statuses = [await _login(client, email, WRONG, "198.51.100.66") for _ in range(10)]
    assert statuses[-1] == 429, "the attacker is stopped by the per-IP limit"

    assert await _login(client, email, PASSWORD, "203.0.113.5") == 200


async def test_three_attacker_addresses_cannot_lock_out_the_owner(client: AsyncClient) -> None:
    """The previous all-attempts limit of 10 per minute fell to exactly this."""
    email = await _account(client)

    for address in ("198.51.100.1", "198.51.100.2", "198.51.100.3"):
        for _ in range(5):
            assert await _login(client, email, WRONG, address) == 401

    assert await _login(client, email, PASSWORD, "203.0.113.5") == 200


async def test_successful_logins_do_not_consume_the_account_budget(client: AsyncClient) -> None:
    email = await _account(client)

    statuses = [
        await _login(client, email, PASSWORD, f"203.0.113.{index + 1}")
        for index in range(settings.RATE_LIMIT_LOGIN_ACCOUNT_FAILURES_PER_MINUTE + 1)
    ]

    assert set(statuses) == {200}


# --- password guessing is still bounded ------------------------------------


async def test_rotating_addresses_hit_the_account_failure_cap(client: AsyncClient) -> None:
    email = await _account(client)
    limit = settings.RATE_LIMIT_LOGIN_ACCOUNT_FAILURES_PER_MINUTE

    statuses = [
        await _login(client, email, WRONG, f"198.51.{index // 250}.{index % 250 + 1}")
        for index in range(limit + 1)
    ]

    assert statuses[:-1] == [401] * limit
    assert statuses[-1] == 429


async def test_correct_password_is_refused_once_the_cap_is_reached(client: AsyncClient) -> None:
    """Otherwise the response would tell a guesser which password was right."""
    email = await _account(client)
    for index in range(settings.RATE_LIMIT_LOGIN_ACCOUNT_FAILURES_PER_MINUTE):
        await _login(client, email, WRONG, f"198.51.{index // 250}.{index % 250 + 1}")

    assert await _login(client, email, PASSWORD, "203.0.113.5") == 429


async def test_email_case_cannot_be_used_to_get_a_fresh_budget(client: AsyncClient) -> None:
    email = await _account(client)
    limit = settings.RATE_LIMIT_LOGIN_ACCOUNT_FAILURES_PER_MINUTE
    for index in range(limit):
        await _login(client, email, WRONG, f"198.51.{index // 250}.{index % 250 + 1}")

    assert await _login(client, email.upper(), WRONG, "198.51.200.1") == 429


async def test_unknown_emails_are_throttled_the_same_way(client: AsyncClient) -> None:
    """A different outcome would reveal which addresses have accounts."""
    email = f"nobody-{uuid.uuid4().hex[:8]}@example.com"
    limit = settings.RATE_LIMIT_LOGIN_ACCOUNT_FAILURES_PER_MINUTE

    statuses = [
        await _login(client, email, WRONG, f"198.51.{index // 250}.{index % 250 + 1}")
        for index in range(limit + 1)
    ]

    assert statuses[:-1] == [401] * limit
    assert statuses[-1] == 429


async def test_progressive_delay_starts_at_the_soft_threshold_and_is_capped(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_ACCOUNT_DELAY_BASE_SECONDS", 0.25)
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def record(seconds: float) -> None:
        delays.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(rate_limit.asyncio, "sleep", record)
    email = await _account(client)
    soft = settings.RATE_LIMIT_LOGIN_ACCOUNT_SOFT_FAILURES

    for index in range(soft + 6):
        await _login(client, email, WRONG, f"198.51.{index // 250}.{index % 250 + 1}")

    assert len(delays) == 6, "no delay below the soft threshold"
    assert delays == sorted(delays)
    assert delays[0] == 0.25
    assert max(delays) <= settings.RATE_LIMIT_LOGIN_ACCOUNT_DELAY_MAX_SECONDS


async def test_account_failures_are_still_counted_while_redis_is_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = await _account(client)
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/15")
    limit = settings.RATE_LIMIT_LOGIN_ACCOUNT_FAILURES_PER_MINUTE

    statuses = [
        await _login(client, email, WRONG, f"198.51.{index // 250}.{index % 250 + 1}")
        for index in range(limit + 1)
    ]

    assert statuses[-1] == 429


# --- visitor identity through the real routes -------------------------------


async def test_two_visitors_on_different_addresses_are_independent(client: AsyncClient) -> None:
    for _ in range(settings.RATE_LIMIT_LOGIN_PER_MINUTE + 1):
        first = await _login(client, f"a{uuid.uuid4().hex}@example.com", WRONG, "198.51.100.1")
    assert first == 429
    assert await _login(client, "b@example.com", WRONG, "198.51.100.2") == 401


async def test_two_visitors_behind_one_address_share_its_limit(client: AsyncClient) -> None:
    """An office NAT is one IP identity; this is the per-IP limit working as designed."""
    for _ in range(settings.RATE_LIMIT_LOGIN_PER_MINUTE):
        await _login(client, f"a{uuid.uuid4().hex}@example.com", WRONG, "198.51.100.3")
    assert await _login(client, "colleague@example.com", WRONG, "198.51.100.3") == 429


async def _exhaust_peer_identity(client: AsyncClient) -> None:
    """Uses up the limit for the unasserted identity, the TCP peer."""
    for _ in range(settings.RATE_LIMIT_LOGIN_PER_MINUTE):
        await client.post(
            "/api/v1/auth/login",
            json={"email": f"p{uuid.uuid4().hex}@example.com", "password": WRONG},
        )


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({VERIFIED_IP_HEADER: "198.51.100.77"}, id="assertion-without-signature"),
        pytest.param(
            _signed("198.51.100.77", secret="not-the-real-secret-not-the-real-secret"),
            id="wrong-secret",
        ),
        pytest.param(
            {**_signed("198.51.100.77"), VERIFIED_IP_HEADER: "198.51.100.78"}, id="tampered-ip"
        ),
        pytest.param(_signed("not-an-address"), id="signed-garbage"),
        pytest.param({"X-Forwarded-For": "198.51.100.77"}, id="x-forwarded-for"),
        pytest.param({"True-Client-IP": "198.51.100.77"}, id="true-client-ip"),
        pytest.param({"X-Vercel-Forwarded-For": "198.51.100.77"}, id="x-vercel-forwarded-for"),
    ],
)
async def test_invalid_assertions_cannot_escape_the_limit(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    await _exhaust_peer_identity(client)

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": f"p{uuid.uuid4().hex}@example.com", "password": WRONG},
        headers=headers,
    )

    assert response.status_code == 429


async def test_expired_assertion_cannot_escape_the_limit(client: AsyncClient) -> None:
    await _exhaust_peer_identity(client)
    expired = int(time.time()) - settings.EDGE_PROXY_MAX_CLOCK_SKEW_SECONDS - 5

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": f"p{uuid.uuid4().hex}@example.com", "password": WRONG},
        headers=_signed("198.51.100.77", timestamp=expired),
    )

    assert response.status_code == 429


async def test_replayed_assertion_only_works_until_it_expires(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay inside the window is accepted by design: it only claims the IP that was
    already asserted, and assertions travel server-to-server, so a client never sees one.
    Outside the window the same headers fall back to the caller's own identity."""
    captured = _signed("198.51.100.90")
    body = {"email": "replay@example.com", "password": WRONG}
    assert (await client.post("/api/v1/auth/login", json=body, headers=captured)).status_code == 401

    resolved_while_fresh = await client.post("/api/v1/auth/login", json=body, headers=captured)
    assert resolved_while_fresh.status_code == 401

    later = time.time() + settings.EDGE_PROXY_MAX_CLOCK_SKEW_SECONDS + 5
    monkeypatch.setattr(client_ip.time, "time", lambda: later)
    await _exhaust_peer_identity(client)
    response = await client.post("/api/v1/auth/login", json=body, headers=captured)
    assert response.status_code == 429


async def test_secret_is_never_written_to_logs(
    client: AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = _signed("198.51.100.91", secret="not-the-real-secret-not-the-real-secret")
    await client.post(
        "/api/v1/auth/login", json={"email": "a@example.com", "password": WRONG}, headers=bad
    )
    await client.post(
        "/api/v1/auth/login",
        json={"email": "a@example.com", "password": WRONG},
        headers=_signed("198.51.100.92"),
    )

    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert bad[SIGNATURE_HEADER] not in captured.out + captured.err


async def test_missing_secret_means_assertions_are_ignored(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", "")
    await _exhaust_peer_identity(client)

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": f"p{uuid.uuid4().hex}@example.com", "password": WRONG},
        headers=_signed("198.51.100.77"),
    )

    assert response.status_code == 429
