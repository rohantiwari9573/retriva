"""Async SQLAlchemy engine/session setup.

A single engine is created at import time with a bounded connection pool. Sessions
are provided per-request via the `get_db` FastAPI dependency and always closed,
never leaked across requests.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """A callable that opens a fresh, independently-committing session -
    for code that must NOT tie its session lifetime to a single request/
    response dependency scope, most importantly a StreamingResponse
    generator: a yield-dependency like get_db() is torn down as soon as the
    route handler function returns, which happens as soon as the
    StreamingResponse object is constructed - before the generator body
    (which runs later, while the response streams) has done any of its own
    DB work. See app/api/v1/chat.py's chat_stream() for the concrete case,
    and app/workers/tasks/document_processing.py for the same "own
    session/engine, not the request-scoped one" pattern used by Celery
    tasks. Tests override this dependency with a wrapper around the
    per-test db_session (see tests/conftest.py's pipeline_session_factory)
    so streaming route tests still run inside the test's rolled-back
    transaction instead of writing to the real test database."""
    return AsyncSessionLocal


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Per-request session with unit-of-work semantics: commits if the route
    completed without raising, rolls back otherwise. Routes/services never
    need to call session.commit() themselves."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
