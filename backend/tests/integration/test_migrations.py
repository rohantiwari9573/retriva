"""R0.1: the dedicated migration step, run for real against throwaway databases.

Each test gets its own empty Postgres database and invokes the same command the
`migrate` Compose service runs, in a subprocess, so exit codes and the advisory
lock are exercised as they are in deployment.
"""

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.migrations import (
    EXIT_DATABASE_UNAVAILABLE,
    EXIT_MIGRATION_FAILED,
    EXIT_OK,
    SchemaNotMigratedError,
    assert_schema_is_current,
    expected_head,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _admin_conninfo() -> str:
    return settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


def _database_url(name: str) -> str:
    base, _, _ = settings.DATABASE_URL.rpartition("/")
    return f"{base}/{name}"


@pytest.fixture
def fresh_database() -> Iterator[str]:
    name = f"migrate_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_admin_conninfo(), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield name
    finally:
        with psycopg.connect(_admin_conninfo(), autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _migrate_process(database_url: str, **env: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "scripts.migrate"],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _run_migrate(database_url: str, **env: str) -> tuple[int, str]:
    process = _migrate_process(database_url, **env)
    output, _ = process.communicate(timeout=120)
    return process.returncode, output


def _query(database: str, statement: str) -> list[tuple]:
    conninfo = _database_url(database).replace("postgresql+asyncpg://", "postgresql://")
    with psycopg.connect(conninfo) as connection:
        return connection.execute(statement).fetchall()


def test_migrate_initializes_an_empty_database(fresh_database: str) -> None:
    code, output = _run_migrate(_database_url(fresh_database))

    assert code == EXIT_OK, output
    assert _query(fresh_database, "SELECT version_num FROM alembic_version") == [
        (expected_head(),)
    ]
    tables = {row[0] for row in _query(
        fresh_database, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )}
    assert {"users", "organizations", "documents", "document_chunks", "messages"} <= tables


def test_migrate_is_idempotent(fresh_database: str) -> None:
    url = _database_url(fresh_database)
    assert _run_migrate(url)[0] == EXIT_OK

    code, output = _run_migrate(url)

    assert code == EXIT_OK, output
    assert len(_query(fresh_database, "SELECT version_num FROM alembic_version")) == 1


def test_concurrent_runs_serialize_on_the_advisory_lock(fresh_database: str) -> None:
    """Without the lock, the losers crash with 'relation already exists'."""
    url = _database_url(fresh_database)

    processes = [_migrate_process(url) for _ in range(3)]
    results = [(p.communicate(timeout=180)[0], p.returncode) for p in processes]

    assert [code for _, code in results] == [EXIT_OK] * 3, results


def test_unreachable_database_fails_with_a_bounded_wait() -> None:
    process = _migrate_process(
        "postgresql+asyncpg://nexus:nexus@127.0.0.1:1/nope",
        MIGRATE_DB_WAIT_SECONDS="1",
    )
    output, _ = process.communicate(timeout=30)

    assert process.returncode == EXIT_DATABASE_UNAVAILABLE, output


def test_failed_migration_exits_nonzero_and_leaves_no_partial_head(fresh_database: str) -> None:
    conninfo = _database_url(fresh_database).replace("postgresql+asyncpg://", "postgresql://")
    with psycopg.connect(conninfo, autocommit=True) as connection:
        # Collides with the first migration, which creates this table.
        connection.execute("CREATE TABLE users (id int)")

    code, output = _run_migrate(_database_url(fresh_database))

    assert code == EXIT_MIGRATION_FAILED, output
    versions = _query(
        fresh_database,
        "SELECT to_regclass('public.alembic_version') IS NOT NULL",
    )[0][0]
    if versions:
        assert _query(fresh_database, "SELECT version_num FROM alembic_version") != [
            (expected_head(),)
        ]


async def _check_schema(database: str) -> None:
    engine = create_async_engine(_database_url(database))
    try:
        async with async_sessionmaker(engine)() as session:
            await assert_schema_is_current(session)
    finally:
        await engine.dispose()


async def test_api_schema_check_rejects_an_unmigrated_database(fresh_database: str) -> None:
    with pytest.raises(SchemaNotMigratedError, match="scripts.migrate"):
        await _check_schema(fresh_database)


async def test_api_schema_check_rejects_a_database_behind_head(fresh_database: str) -> None:
    conninfo = _database_url(fresh_database).replace("postgresql+asyncpg://", "postgresql://")
    previous = "284636820202"  # the revision immediately before current head
    with psycopg.connect(conninfo, autocommit=True) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version VALUES (%s)", (previous,))

    with pytest.raises(SchemaNotMigratedError, match=previous):
        await _check_schema(fresh_database)


async def test_api_schema_check_accepts_a_migrated_database(fresh_database: str) -> None:
    assert _run_migrate(_database_url(fresh_database))[0] == EXIT_OK

    await _check_schema(fresh_database)


async def test_api_schema_check_tolerates_a_newer_database(fresh_database: str) -> None:
    """Rolling the app back after a forward migration must not block boot."""
    conninfo = _database_url(fresh_database).replace("postgresql+asyncpg://", "postgresql://")
    with psycopg.connect(conninfo, autocommit=True) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version VALUES ('a_future_revision')")

    await _check_schema(fresh_database)
