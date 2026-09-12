"""Shared pytest fixtures.

Test isolation strategy:
- `db_engine` (session-scoped) runs real Alembic migrations against a dedicated
  test database, exactly like production would - not Base.metadata.create_all(),
  so a broken migration fails the test suite rather than only showing up later.
- `db_session` (function-scoped) opens one connection + one outer transaction
  per test and hands the test a session bound to it; the transaction is rolled
  back after the test, so tests never see each other's data without needing to
  truncate tables or reset sequences between runs.
- `client` overrides the app's get_db dependency with that same session, and
  flushes the Redis test DB (rate-limit counters) before each test so limits
  from one test don't bleed into the next.
"""

import os

os.environ.setdefault("JWT_SECRET", "test-secret-please-override-1234567890")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://nexus:nexus@localhost:5433/nexus_test"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

from collections.abc import AsyncGenerator

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from app.core.config import settings
from app.core.database import get_db
from app.main import app
from app.rag.embedding.testing import DeterministicTestEmbeddingProvider
from app.storage.dependency import get_storage_provider
from app.storage.memory import InMemoryStorageProvider

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_migrations() -> None:
    config = Config(os.path.join(_BACKEND_ROOT, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(_BACKEND_ROOT, "alembic"))
    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest.fixture(scope="session", autouse=True)
def _apply_migrations():
    _run_migrations()
    yield


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(settings.DATABASE_URL)
    connection = await engine.connect()
    transaction = await connection.begin()

    # join_transaction_mode="create_savepoint": the Session's own begin/commit
    # cycle nests as a SAVEPOINT inside the outer transaction we already
    # opened, instead of erroring on "transaction already begun" - this is
    # what lets a full commit() inside a route handler still get discarded by
    # our rollback() below.
    session_factory = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    session = session_factory()

    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


@pytest.fixture(autouse=True)
async def _flush_rate_limits() -> AsyncGenerator[None, None]:
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    await redis.flushdb()
    await redis.aclose()
    yield


@pytest.fixture(autouse=True)
async def _flush_celery_broker() -> AsyncGenerator[None, None]:
    # Upload tests exercise the real DocumentService._enqueue_processing path,
    # which calls process_document.delay() against the real Celery broker
    # (Redis, a different logical DB than rate limiting) - nothing in the
    # test process consumes those messages, so flush between tests to avoid
    # an ever-growing queue in local/CI Redis.
    redis = Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)
    await redis.flushdb()
    await redis.aclose()
    yield


@pytest.fixture
def fake_storage() -> InMemoryStorageProvider:
    # No live MinIO in CI - upload/download/delete tests run against this
    # in-memory fake instead. Real S3/MinIO behavior (bucket creation,
    # presigned URL signing) is exercised manually against Docker Compose.
    return InMemoryStorageProvider()


@pytest.fixture
def fake_embedding_provider() -> DeterministicTestEmbeddingProvider:
    # No live LM Studio in CI - ingestion pipeline integration tests run
    # against this deterministic fake instead of real embeddings. See
    # app/rag/embedding/testing.py's module docstring for why this is
    # separate from a mocked provider (unit tests) and from real LM Studio
    # (manual E2E only).
    return DeterministicTestEmbeddingProvider(dimensions=settings.EMBEDDING_DIMENSIONS)


class _NoCloseSessionContext:
    """Wraps an already-open test session so it can be handed to code that
    expects an async_sessionmaker-shaped callable (`session_factory()` used
    as `async with ...`) without that code closing the shared test session -
    closing it would break the outer SAVEPOINT-based rollback this test
    suite relies on for isolation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __call__(self) -> "_NoCloseSessionContext":
        return self

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


@pytest.fixture
def pipeline_session_factory(db_session: AsyncSession) -> _NoCloseSessionContext:
    return _NoCloseSessionContext(db_session)


@pytest.fixture
async def client(
    db_session: AsyncSession, fake_storage: InMemoryStorageProvider
) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        # Mirrors app.core.database.get_db's commit/rollback-per-request
        # semantics, but commits land in the SAVEPOINT from db_session rather
        # than the real transaction, so the outer rollback still discards them.
        try:
            yield db_session
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_storage_provider] = lambda: fake_storage
    # raise_app_exceptions=False: match real deployment behavior, where an
    # unhandled exception becomes the generic-Exception handler's 500 JSON
    # response, not a Python exception escaping to the caller. Without this,
    # httpx's default re-raises the original error past our error handling
    # entirely, which is useful for debugging a broken test but wrong for
    # asserting on the actual HTTP contract.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()
