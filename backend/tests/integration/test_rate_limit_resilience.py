"""R0.2 and R0.3 through the real HTTP routes.

Redis is made unreachable by pointing REDIS_URL at a closed port, and restored
by pointing it back, so "Redis" here is the real server whenever it's up. The
container-level version (stopping and restarting Redis) is in TESTING.md.
"""

import hmac
import logging
import time
import uuid
from hashlib import sha256

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis

from app.core import rate_limit
from app.core.client_ip import SIGNATURE_HEADER, TIMESTAMP_HEADER, VERIFIED_IP_HEADER
from app.core.config import settings

UNREACHABLE_REDIS = "redis://127.0.0.1:1/15"
PASSWORD = "Str0ng-Passw0rd!x"
SECRET = "integration-test-secret-at-least-32-chars"


def _credentials() -> dict[str, str]:
    return {"email": f"limit-{uuid.uuid4().hex[:10]}@example.com", "password": PASSWORD}


async def _register(client: AsyncClient, credentials: dict[str, str]) -> None:
    response = await client.post(
        "/api/v1/auth/register", json={**credentials, "full_name": "Limit Test"}
    )
    assert response.status_code == 201, response.text
    client.cookies.clear()


def _signed(ip: str) -> dict[str, str]:
    stamp = str(int(time.time()))
    signature = hmac.new(SECRET.encode(), f"{stamp}.{ip}".encode(), sha256).hexdigest()
    return {VERIFIED_IP_HEADER: ip, TIMESTAMP_HEADER: stamp, SIGNATURE_HEADER: signature}


@pytest.fixture
def redis_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "REDIS_URL", UNREACHABLE_REDIS)


# --- Redis unavailable ------------------------------------------------------


@pytest.mark.usefixtures("redis_down")
async def test_login_succeeds_while_redis_is_down(client: AsyncClient) -> None:
    credentials = _credentials()
    await _register(client, credentials)

    response = await client.post("/api/v1/auth/login", json=credentials)

    assert response.status_code == 200, response.text


@pytest.mark.usefixtures("redis_down")
async def test_wrong_password_is_still_a_401_while_redis_is_down(client: AsyncClient) -> None:
    credentials = _credentials()
    await _register(client, credentials)

    response = await client.post(
        "/api/v1/auth/login", json={**credentials, "password": "Wrong-Passw0rd!x"}
    )

    assert response.status_code == 401


@pytest.mark.usefixtures("redis_down")
async def test_rate_limit_is_still_enforced_while_redis_is_down(client: AsyncClient) -> None:
    """The fallback must not silently turn limiting off."""
    body = {"email": "nobody@example.com", "password": "Wrong-Passw0rd!x"}

    statuses = [
        (await client.post("/api/v1/auth/login", json=body)).status_code
        for _ in range(settings.RATE_LIMIT_LOGIN_PER_MINUTE + 1)
    ]

    assert statuses[:-1] == [401] * settings.RATE_LIMIT_LOGIN_PER_MINUTE
    assert statuses[-1] == 429


@pytest.mark.usefixtures("redis_down")
async def test_redis_outage_does_not_add_latency_to_every_request(client: AsyncClient) -> None:
    """Baseline with Redis down was 3-5 seconds per request, ending in a 500."""
    start = time.perf_counter()
    response = await client.get("/api/v1/users/me")
    await client.post("/api/v1/auth/login", json=_credentials())
    assert response.status_code == 401
    assert time.perf_counter() - start < 2.0


async def test_fallback_activation_is_logged_once(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        rate_limit.logger, "warning", lambda event, **_: events.append(event)
    )
    monkeypatch.setattr(settings, "REDIS_URL", UNREACHABLE_REDIS)
    monkeypatch.setattr(settings, "RATE_LIMIT_REDIS_RETRY_SECONDS", 0.0)
    body = {"email": "nobody@example.com", "password": "Wrong-Passw0rd!x"}

    for _ in range(3):
        await client.post("/api/v1/auth/login", json=body)

    assert events.count("rate_limit_fallback_activated") == 1
    assert rate_limit.fallback_active() is True


async def test_fallback_failure_rejects_instead_of_allowing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If both limiters are broken, deny rather than run unlimited."""
    monkeypatch.setattr(settings, "REDIS_URL", UNREACHABLE_REDIS)

    def broken(*_: object) -> int:
        raise RuntimeError("fallback exploded")

    monkeypatch.setattr(rate_limit._fallback, "hit", broken)

    response = await client.post(
        "/api/v1/auth/login", json={"email": "a@example.com", "password": "Wrong-Passw0rd!x"}
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "RATE_LIMITER_UNAVAILABLE"


# --- recovery ---------------------------------------------------------------


async def test_redis_backed_limiting_resumes_after_reconnect(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_redis_url = settings.REDIS_URL
    monkeypatch.setattr(settings, "RATE_LIMIT_REDIS_RETRY_SECONDS", 0.0)
    body = {"email": "nobody@example.com", "password": "Wrong-Passw0rd!x"}

    monkeypatch.setattr(settings, "REDIS_URL", UNREACHABLE_REDIS)
    await client.post("/api/v1/auth/login", json=body)
    assert rate_limit.fallback_active() is True

    monkeypatch.setattr(settings, "REDIS_URL", real_redis_url)
    await client.post("/api/v1/auth/login", json=body)
    assert rate_limit.fallback_active() is False

    redis = Redis.from_url(real_redis_url, decode_responses=True)
    keys = await redis.keys("ratelimit:login:ip:*")
    await redis.aclose()
    assert keys, "the request after recovery should have counted in Redis"


async def test_redis_is_not_retried_during_the_cooldown(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "REDIS_URL", UNREACHABLE_REDIS)
    monkeypatch.setattr(settings, "RATE_LIMIT_REDIS_RETRY_SECONDS", 60.0)
    attempts = 0
    original = rate_limit._redis_increment

    async def counting(*args: object) -> int:
        nonlocal attempts
        attempts += 1
        return await original(*args)

    monkeypatch.setattr(rate_limit, "_redis_increment", counting)
    body = {"email": "nobody@example.com", "password": "Wrong-Passw0rd!x"}

    for _ in range(3):
        await client.post("/api/v1/auth/login", json=body)

    assert attempts == 1


async def test_counter_without_a_ttl_cannot_lock_a_client_out_forever(
    client: AsyncClient,
) -> None:
    """A crash between INCR and EXPIRE used to leave a permanent counter."""
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    key = "ratelimit:login:ip:127.0.0.1"
    await redis.set(key, 1)
    assert await redis.ttl(key) == -1

    await client.post(
        "/api/v1/auth/login", json={"email": "a@example.com", "password": "Wrong-Passw0rd!x"}
    )

    assert 0 < await redis.ttl(key) <= 60
    await redis.aclose()


async def test_readiness_stays_ready_but_reports_redis_when_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth works without Redis, so an orchestrator must not pull the instance."""
    monkeypatch.setattr(settings, "REDIS_URL", UNREACHABLE_REDIS)

    response = await client.get("/readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["redis"] == "unavailable"
    assert body["degraded"] == ["redis"]


# --- identity behind the proxy ----------------------------------------------


async def test_visitors_behind_the_proxy_do_not_share_a_login_limit(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", SECRET)
    body = {"email": "nobody@example.com", "password": "Wrong-Passw0rd!x"}

    for _ in range(settings.RATE_LIMIT_LOGIN_PER_MINUTE + 1):
        last = await client.post("/api/v1/auth/login", json=body, headers=_signed("198.51.100.1"))
    assert last.status_code == 429

    other = await client.post(
        "/api/v1/auth/login",
        json={"email": "other@example.com", "password": "Wrong-Passw0rd!x"},
        headers=_signed("198.51.100.2"),
    )
    assert other.status_code == 401


async def test_forged_forwarding_headers_cannot_escape_the_limit(client: AsyncClient) -> None:
    statuses = []
    for index in range(settings.RATE_LIMIT_LOGIN_PER_MINUTE + 1):
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": f"n{index}@example.com", "password": "Wrong-Passw0rd!x"},
            headers={
                "X-Forwarded-For": f"1.2.3.{index}",
                "X-Real-IP": f"1.2.3.{index}",
                "True-Client-IP": f"1.2.3.{index}",
                VERIFIED_IP_HEADER: f"1.2.3.{index}",
            },
        )
        statuses.append(response.status_code)

    assert statuses[-1] == 429


async def test_login_and_register_limits_are_independent(client: AsyncClient) -> None:
    login_body = {"email": "nobody@example.com", "password": "Wrong-Passw0rd!x"}
    for _ in range(settings.RATE_LIMIT_LOGIN_PER_MINUTE + 1):
        last = await client.post("/api/v1/auth/login", json=login_body)
    assert last.status_code == 429

    response = await client.post(
        "/api/v1/auth/register", json={**_credentials(), "full_name": "New User"}
    )
    assert response.status_code == 201


async def test_one_account_cannot_be_guessed_from_many_addresses(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rotating IPs must not give unlimited guesses against a single account."""
    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", SECRET)
    body = {"email": "Target@Example.com", "password": "Wrong-Passw0rd!x"}
    limit = settings.RATE_LIMIT_LOGIN_ACCOUNT_PER_MINUTE

    statuses = [
        (
            await client.post(
                "/api/v1/auth/login", json=body, headers=_signed(f"198.51.100.{index + 1}")
            )
        ).status_code
        for index in range(limit + 1)
    ]

    assert statuses[:-1] == [401] * limit
    assert statuses[-1] == 429


async def test_chat_limit_is_per_user_not_per_address(client: AsyncClient) -> None:
    """Chat runs behind login, so one address must not cap everyone behind it."""
    from app.api.v1 import chat as chat_routes

    route = next(
        route
        for route in chat_routes.router.routes
        if getattr(route, "path", "").endswith("/chat") and "POST" in route.methods
    )
    assert any(
        "current_user" in str(dependency.dependency.__code__.co_varnames)
        for dependency in route.dependencies
    )


@pytest.fixture(autouse=True)
def _quiet(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.CRITICAL)
