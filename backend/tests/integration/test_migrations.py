"""R0.1: the dedicated migration step, run for real against throwaway databases.

Each test gets its own empty Postgres database and invokes the same command the
`migrate` Compose service runs, in a subprocess, so exit codes and the advisory
lock are exercised as they are in deployment.
"""

import os
import subprocess
import sys
import time
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
# The revision production ran before the vector-index migration (head), so the
# states below are the ones a deploy actually meets.
PREVIOUS_REVISION = "6f45bc893ebc"
VECTOR_INDEX = "ix_document_chunks_embedding_cosine"


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


def _migrate_process(database_url: str, *args: str, **env: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "scripts.migrate", *args],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _run_migrate(database_url: str, *args: str, **env: str) -> tuple[int, str]:
    process = _migrate_process(database_url, *args, **env)
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
    previous = PREVIOUS_REVISION
    with psycopg.connect(conninfo, autocommit=True) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version VALUES (%s)", (previous,))

    with pytest.raises(SchemaNotMigratedError, match=previous):
        await _check_schema(fresh_database)


async def test_api_schema_check_accepts_a_migrated_database(fresh_database: str) -> None:
    assert _run_migrate(_database_url(fresh_database))[0] == EXIT_OK

    await _check_schema(fresh_database)


def _mark_as_newer(database: str) -> None:
    conninfo = _database_url(database).replace("postgresql+asyncpg://", "postgresql://")
    with psycopg.connect(conninfo, autocommit=True) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version VALUES ('a_future_revision')")


async def test_api_schema_check_tolerates_a_newer_database(fresh_database: str) -> None:
    """Rolling the app back after a forward migration must not block boot."""
    _mark_as_newer(fresh_database)

    await _check_schema(fresh_database)


@pytest.mark.parametrize("args", [(), ("--check",)])
def test_migrate_tolerates_a_newer_database(fresh_database: str, args: tuple[str, ...]) -> None:
    """Found against a Neon branch already carrying a later migration: the runner
    raised on the unknown revision, so the migrate service would have blocked the
    API even though the API's own check accepts this state."""
    _mark_as_newer(fresh_database)

    code, output = _run_migrate(_database_url(fresh_database), *args)

    assert code == EXIT_OK, output
    assert "migration_database_newer" in output
    assert _query(fresh_database, "SELECT version_num FROM alembic_version") == [
        ("a_future_revision",)
    ]


# --- the migration step driving the vector-index migration --------------------


def _upgrade_to(database: str, revision: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": _database_url(database)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _revision_and_index(database: str) -> tuple[str, bool]:
    version = _query(database, "SELECT version_num FROM alembic_version")[0][0]
    has_index = _query(database, f"SELECT to_regclass('{VECTOR_INDEX}') IS NOT NULL")[0][0]
    return version, has_index


def test_migration_check_reports_the_vector_migration_as_pending(fresh_database: str) -> None:
    """Dropping an index loses no data, so the destructive guard must not refuse it."""
    _upgrade_to(fresh_database, PREVIOUS_REVISION)

    code, output = _run_migrate(_database_url(fresh_database), "--check")

    assert code == EXIT_OK, output
    assert "migration_pending" in output
    assert "migration_refused_destructive" not in output
    assert _revision_and_index(fresh_database) == (PREVIOUS_REVISION, True)


def test_migration_step_upgrades_the_previous_production_revision(fresh_database: str) -> None:
    _upgrade_to(fresh_database, PREVIOUS_REVISION)

    code, output = _run_migrate(_database_url(fresh_database))

    assert code == EXIT_OK, output
    assert _revision_and_index(fresh_database) == (expected_head(), False)


def test_migration_step_gives_up_on_a_held_table_lock_and_can_be_rerun(
    fresh_database: str,
) -> None:
    """A live reader blocks DROP INDEX. The step must fail fast with nothing applied,
    release the advisory lock, and succeed once the reader is gone."""
    _upgrade_to(fresh_database, PREVIOUS_REVISION)
    conninfo = _database_url(fresh_database).replace("postgresql+asyncpg://", "postgresql://")

    with psycopg.connect(conninfo) as blocker:
        blocker.execute("LOCK TABLE document_chunks IN ACCESS SHARE MODE")
        started = time.monotonic()
        code, output = _run_migrate(_database_url(fresh_database))
        elapsed = time.monotonic() - started

        assert code == EXIT_MIGRATION_FAILED, output
        assert "LockNotAvailable" in output or "lock timeout" in output, output
        assert elapsed < 60, elapsed
        assert _revision_and_index(fresh_database) == (PREVIOUS_REVISION, True)
        blocker.rollback()

    code, output = _run_migrate(_database_url(fresh_database))

    assert code == EXIT_OK, output
    assert _revision_and_index(fresh_database) == (expected_head(), False)


async def test_api_schema_check_rejects_the_previous_production_revision(
    fresh_database: str,
) -> None:
    _upgrade_to(fresh_database, PREVIOUS_REVISION)

    with pytest.raises(SchemaNotMigratedError, match=PREVIOUS_REVISION):
        await _check_schema(fresh_database)


# --- the migration step killed mid-run -----------------------------------------


def _wait_for_lock_wait(database: str, timeout: float = 30.0) -> None:
    """Until a session in `database` is waiting on a lock, i.e. the step is mid-run."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waiting = _query(
            database,
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND wait_event_type = 'Lock'",
        )[0][0]
        if waiting:
            return
        time.sleep(0.05)
    raise AssertionError("the migration step never reached the blocked statement")


def _advisory_locks(database: str) -> int:
    return _query(
        database,
        "SELECT count(*) FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
        "WHERE l.locktype = 'advisory' AND d.datname = current_database()",
    )[0][0]


def _kill(process: subprocess.Popen[str]) -> None:
    process.kill()  # SIGKILL: no cleanup, no rollback from the client side
    process.communicate(timeout=30)


def _wait_until(condition, what: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


def test_killed_migration_step_applies_nothing_and_releases_its_lock(
    fresh_database: str,
) -> None:
    """The production path (6f45bc893ebc -> head), killed while it waits on a table lock."""
    _upgrade_to(fresh_database, PREVIOUS_REVISION)
    conninfo = _database_url(fresh_database).replace("postgresql+asyncpg://", "postgresql://")

    with psycopg.connect(conninfo) as blocker:
        blocker.execute("LOCK TABLE document_chunks IN ACCESS SHARE MODE")
        process = _migrate_process(_database_url(fresh_database))
        _wait_for_lock_wait(fresh_database)
        assert _advisory_locks(fresh_database) == 1  # the step holds the runner lock
        _kill(process)
        blocker.rollback()

    # The server notices the dead client, rolls its transaction back and frees the lock.
    _wait_until(lambda: _advisory_locks(fresh_database) == 0, "the advisory lock to be freed")
    assert _revision_and_index(fresh_database) == (PREVIOUS_REVISION, True)

    code, output = _run_migrate(_database_url(fresh_database))

    assert code == EXIT_OK, output
    assert _revision_and_index(fresh_database) == (expected_head(), False)


def test_killed_multi_revision_upgrade_leaves_no_partial_revision(fresh_database: str) -> None:
    """All pending revisions run in one transaction: killed part-way through, even the
    statements that already ran (here CREATE EXTENSION vector) are rolled back."""
    start = "31c50429804f"  # documents exist; the next revision alters them
    _upgrade_to(fresh_database, start)
    conninfo = _database_url(fresh_database).replace("postgresql+asyncpg://", "postgresql://")
    had_vector = _query(
        fresh_database, "SELECT count(*) FROM pg_extension WHERE extname = 'vector'"
    )[0][0]
    assert had_vector == 0  # so finding it absent afterwards proves the rollback

    with psycopg.connect(conninfo) as blocker:
        blocker.execute("LOCK TABLE documents IN ACCESS EXCLUSIVE MODE")
        process = _migrate_process(_database_url(fresh_database))
        _wait_for_lock_wait(fresh_database)
        _kill(process)
        blocker.rollback()

    _wait_until(lambda: _advisory_locks(fresh_database) == 0, "the advisory lock to be freed")
    assert _query(fresh_database, "SELECT version_num FROM alembic_version") == [(start,)]
    assert _query(fresh_database, "SELECT to_regclass('document_chunks') IS NULL")[0][0]
    columns = {
        row[0]
        for row in _query(
            fresh_database,
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'documents'",
        )
    }
    assert "failure_reason" not in columns
    assert (
        _query(fresh_database, "SELECT count(*) FROM pg_extension WHERE extname = 'vector'")[0][0]
        == had_vector
    )

    code, output = _run_migrate(_database_url(fresh_database))

    assert code == EXIT_OK, output
    assert _revision_and_index(fresh_database) == (expected_head(), False)


def test_killed_upgrade_rolls_back_revisions_that_had_already_finished(
    fresh_database: str,
) -> None:
    """The live (online) path, killed in its second pending revision. 284636820202
    only touches documents and has fully run when 6f45bc893ebc blocks creating its
    foreign key to organizations; the kill must undo the finished revision too.
    With transaction_per_migration it would stay committed at 284636820202."""
    start = "31c50429804f"
    _upgrade_to(fresh_database, start)
    conninfo = _database_url(fresh_database).replace("postgresql+asyncpg://", "postgresql://")

    with psycopg.connect(conninfo) as blocker:
        blocker.execute("LOCK TABLE organizations IN ACCESS EXCLUSIVE MODE")
        process = _migrate_process(_database_url(fresh_database))
        _wait_for_lock_wait(fresh_database)
        blocked_on = _query(
            fresh_database,
            "SELECT query FROM pg_stat_activity "
            "WHERE datname = current_database() AND wait_event_type = 'Lock'",
        )[0][0]
        # Proves the first pending revision has finished and the second is running.
        assert "CREATE TABLE conversations" in blocked_on, blocked_on
        _kill(process)
        blocker.rollback()

    _wait_until(lambda: _advisory_locks(fresh_database) == 0, "the advisory lock to be freed")
    assert _query(fresh_database, "SELECT version_num FROM alembic_version") == [(start,)]
    assert _query(fresh_database, "SELECT to_regclass('document_chunks') IS NULL")[0][0]
    assert _query(fresh_database, "SELECT to_regclass('conversations') IS NULL")[0][0]
    assert (
        _query(fresh_database, "SELECT count(*) FROM pg_extension WHERE extname = 'vector'")[0][0]
        == 0
    )

    code, output = _run_migrate(_database_url(fresh_database))

    assert code == EXIT_OK, output
    assert _revision_and_index(fresh_database) == (expected_head(), False)


def test_all_pending_revisions_run_in_one_transaction() -> None:
    """What makes an interrupted multi-revision upgrade all-or-nothing. A switch to
    transaction_per_migration would let a kill leave earlier revisions applied."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "base:head", "--sql"],
        cwd=BACKEND_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    statements = [line.strip() for line in result.stdout.splitlines()]

    assert statements.count("BEGIN;") == 1
    assert statements.count("COMMIT;") == 1
    assert sum("INSERT INTO alembic_version" in line for line in statements) == 1
