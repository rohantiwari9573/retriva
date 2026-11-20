"""A response must never be sent before the request's transaction commits.

`get_db` commits in its teardown (after `yield`). If that teardown runs after the
response has been sent, a client can act on a 201 before the row it describes
exists: "register, then immediately use the account" fails with 401. The
in-process ASGI client waits for dependency teardown before returning, which
hides this, so these tests use a real uvicorn server over real sockets, and slow
every commit down so the ordering is deterministic rather than a timing race.
"""

import asyncio
import threading
import time
import uuid

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.rate_limit import reset_fallback_state
from app.models.user import User

COMMIT_DELAY_SECONDS = 1.0
PASSWORD = "correct-horse-battery-staple-42"


class _Server:
    def __init__(self) -> None:
        from app.main import app

        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "_Server":
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        assert self.server.started, "uvicorn did not start"
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


# How commits behave in the server for the current test ("slow" or "fail").
_COMMIT_MODE = {"mode": "slow"}


@pytest.fixture(scope="module")
def server():
    """One server for the module: the app's global connection pool is bound to the
    server's event loop, so it must not be restarted between tests."""
    real_commit = AsyncSession.commit

    async def commit(self):
        if _COMMIT_MODE["mode"] == "fail":
            raise RuntimeError("simulated commit failure")
        await asyncio.sleep(COMMIT_DELAY_SECONDS)
        await real_commit(self)

    # get_db, unchanged, but on a NullPool engine: the app's pooled connections
    # would stay bound to this server's event loop after it stops and break the
    # next real-server test.
    from app.core import database

    real_factory = database.AsyncSessionLocal
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    database.AsyncSessionLocal = async_sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False
    )
    AsyncSession.commit = commit  # type: ignore[method-assign]
    try:
        with _Server() as running:
            yield running
    finally:
        AsyncSession.commit = real_commit  # type: ignore[method-assign]
        database.AsyncSessionLocal = real_factory


@pytest.fixture(autouse=True)
def _slow_by_default():
    reset_fallback_state()
    _COMMIT_MODE["mode"] = "slow"
    yield
    _COMMIT_MODE["mode"] = "slow"


async def _users_with_email(email: str) -> int:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as session:
            return (
                await session.execute(select(func.count()).where(User.email == email))
            ).scalar_one()
    finally:
        await engine.dispose()


def _email() -> str:
    return f"race-{uuid.uuid4().hex[:12]}@example.com"


def test_a_registered_account_is_usable_as_soon_as_the_201_arrives(server):
    email = _email()
    with httpx.Client(base_url=server.base, timeout=30) as client:
        created = client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
        assert created.status_code == 201, created.text
        me = client.get("/api/v1/users/me")  # immediately, with the cookies just set
        assert me.status_code == 200, f"account not usable yet: {me.status_code} {me.text}"
        login = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert login.status_code == 200, login.text
    assert asyncio.run(_users_with_email(email)) == 1


def test_a_201_is_never_sent_before_the_commit(server):
    """The response itself must wait for the commit: with commits slowed by
    COMMIT_DELAY_SECONDS, a register can't come back faster than that."""
    with httpx.Client(base_url=server.base, timeout=30) as client:
        started = time.monotonic()
        created = client.post(
            "/api/v1/auth/register", json={"email": _email(), "password": PASSWORD}
        )
        elapsed = time.monotonic() - started
    assert created.status_code == 201
    assert elapsed >= COMMIT_DELAY_SECONDS


def test_concurrent_registrations_of_one_email_create_one_account(server):
    email = _email()

    def register() -> int:
        with httpx.Client(base_url=server.base, timeout=30) as client:
            return client.post(
                "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
            ).status_code

    results: list[int] = []
    # 3 = the per-IP register limit per minute (Redis is flushed between tests).
    threads = [threading.Thread(target=lambda: results.append(register())) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 1, results
    assert set(results) <= {201, 409}, results
    assert asyncio.run(_users_with_email(email)) == 1


def test_a_failed_commit_returns_an_error_not_a_201(server):
    """If the commit fails, the client must get an error (nothing was saved),
    never a 201 for an account that doesn't exist."""
    _COMMIT_MODE["mode"] = "fail"
    email = _email()
    with httpx.Client(base_url=server.base, timeout=30) as client:
        created = client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    assert created.status_code >= 500, created.status_code
    assert asyncio.run(_users_with_email(email)) == 0


def test_every_route_commits_before_responding():
    """Guards the fix: a route that declares a bare Depends(get_db) would bring
    the race back (FastAPI's default "request" scope tears down after the
    response is sent)."""
    from fastapi.routing import APIRoute

    from app.core.database import get_db
    from app.main import app

    def walk(dependant, path):
        for dep in dependant.dependencies:
            if dep.call is get_db:
                yield path, dep.scope
            yield from walk(dep, path)

    def api_routes(routes):
        # Recent FastAPI keeps included routers nested instead of flattening them.
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            for attr in ("original_router", "router", "app"):
                inner = getattr(route, attr, None)
                if inner is not None and hasattr(inner, "routes"):
                    yield from api_routes(inner.routes)
            if hasattr(route, "routes") and not isinstance(route, APIRoute):
                yield from api_routes(route.routes)

    found = [
        (path, scope)
        for route in api_routes(app.routes)
        for path, scope in walk(route.dependant, route.path)
    ]
    assert found, "no route uses get_db?"
    wrong = sorted({path for path, scope in found if scope != "function"})
    assert not wrong, f"routes that respond before committing: {wrong}"
