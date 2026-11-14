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
    EXIT_LOCK_TIMEOUT,
    EXIT_MIGRATION_FAILED,
    EXIT_MIGRATIONS_PENDING,
    EXIT_OK,
    EXIT_UNKNOWN_REVISION,
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


async def test_api_schema_check_rejects_an_unknown_revision(fresh_database: str) -> None:
    """A mistyped `alembic stamp` or another branch's database must not be served."""
    _mark_as_newer(fresh_database)

    with pytest.raises(SchemaNotMigratedError, match="SCHEMA_ACCEPT_NEWER_REVISIONS"):
        await _check_schema(fresh_database)


async def test_api_schema_check_accepts_a_declared_newer_revision(
    fresh_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rolling the app back after a forward migration must not block boot, once the
    operator names the newer revision."""
    _mark_as_newer(fresh_database)
    monkeypatch.setattr(settings, "SCHEMA_ACCEPT_NEWER_REVISIONS", "other, a_future_revision")

    await _check_schema(fresh_database)


async def test_api_schema_check_still_rejects_a_revision_not_on_the_list(
    fresh_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mark_as_newer(fresh_database)
    monkeypatch.setattr(settings, "SCHEMA_ACCEPT_NEWER_REVISIONS", "a_different_revision")

    with pytest.raises(SchemaNotMigratedError, match="a_future_revision"):
        await _check_schema(fresh_database)


@pytest.mark.parametrize("args", [(), ("--check",)])
def test_migrate_refuses_an_unknown_revision(fresh_database: str, args: tuple[str, ...]) -> None:
    """It used to exit 0 here, so a deploy would go ahead on a database the code
    couldn't vouch for. Now it is a distinct failure and nothing is changed."""
    _mark_as_newer(fresh_database)

    code, output = _run_migrate(_database_url(fresh_database), *args)

    assert code == EXIT_UNKNOWN_REVISION, output
    assert "migration_unknown_revision" in output
    assert _query(fresh_database, "SELECT version_num FROM alembic_version") == [
        ("a_future_revision",)
    ]


@pytest.mark.parametrize("args", [(), ("--check",)])
def test_migrate_accepts_a_declared_newer_revision(
    fresh_database: str, args: tuple[str, ...]
) -> None:
    """Found against a Neon branch already carrying a later migration: during a
    rollback the migrate service must not block the API, which accepts the same list."""
    _mark_as_newer(fresh_database)

    code, output = _run_migrate(
        _database_url(fresh_database), *args, SCHEMA_ACCEPT_NEWER_REVISIONS="a_future_revision"
    )

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

    # Not 0: a script gating a deploy on --check must see that work is pending.
    assert code == EXIT_MIGRATIONS_PENDING, output
    assert "migration_pending" in output
    assert "migration_refused_destructive" not in output
    assert _revision_and_index(fresh_database) == (PREVIOUS_REVISION, True)


def test_migration_check_at_head_exits_zero_and_pending_on_an_empty_database(
    fresh_database: str,
) -> None:
    url = _database_url(fresh_database)
    assert _run_migrate(url, "--check")[0] == EXIT_MIGRATIONS_PENDING  # nothing applied yet
    assert _query(fresh_database, "SELECT to_regclass('alembic_version') IS NULL")[0][0]

    assert _run_migrate(url)[0] == EXIT_OK
    code, output = _run_migrate(url, "--check")

    assert code == EXIT_OK, output
    assert "migration_nothing_to_do" in output


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


# --- waiting for the runner lock is bounded -----------------------------------


def _hold_runner_lock(database: str) -> psycopg.Connection:
    """A session holding the runner lock, as a wedged or slow runner would."""
    from scripts.migrate import _MIGRATION_LOCK_KEY

    conninfo = _database_url(database).replace("postgresql+asyncpg://", "postgresql://")
    holder = psycopg.connect(conninfo, autocommit=True)
    holder.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
    return holder


@pytest.mark.parametrize("args", [(), ("--check",)])
def test_a_held_runner_lock_times_out_with_its_own_exit_code(
    fresh_database: str, args: tuple[str, ...]
) -> None:
    """pg_advisory_lock used to wait forever, so a deploy script running --check
    behind a wedged runner hung with it."""
    holder = _hold_runner_lock(fresh_database)
    try:
        started = time.monotonic()
        code, output = _run_migrate(
            _database_url(fresh_database), *args, MIGRATE_LOCK_WAIT_SECONDS="2"
        )
        elapsed = time.monotonic() - started
    finally:
        holder.close()

    assert code == EXIT_LOCK_TIMEOUT, output
    assert "migration_lock_timeout" in output
    assert elapsed < 30
    assert _query(fresh_database, "SELECT to_regclass('alembic_version') IS NULL")[0][0]


def test_a_runner_waiting_for_the_lock_proceeds_once_it_is_released(
    fresh_database: str,
) -> None:
    holder = _hold_runner_lock(fresh_database)
    process = _migrate_process(_database_url(fresh_database), MIGRATE_LOCK_WAIT_SECONDS="60")
    try:
        time.sleep(2)  # well inside the wait: the runner is polling for the lock
        assert process.poll() is None
        assert _query(fresh_database, "SELECT to_regclass('alembic_version') IS NULL")[0][0]
    finally:
        holder.close()  # the other runner finishes

    output, _ = process.communicate(timeout=120)

    assert process.returncode == EXIT_OK, output
    assert "migration_waiting_for_lock" in output
    assert _query(fresh_database, "SELECT version_num FROM alembic_version") == [
        (expected_head(),)
    ]


def test_a_runner_killed_while_waiting_changes_nothing_and_a_rerun_recovers(
    fresh_database: str,
) -> None:
    holder = _hold_runner_lock(fresh_database)
    try:
        process = _migrate_process(_database_url(fresh_database), MIGRATE_LOCK_WAIT_SECONDS="60")
        time.sleep(2)
        _kill(process)
        # The holder's lock is untouched, and the killed waiter applied nothing.
        assert _advisory_locks(fresh_database) == 1
        assert _query(fresh_database, "SELECT to_regclass('alembic_version') IS NULL")[0][0]
    finally:
        holder.close()

    _wait_until(lambda: _advisory_locks(fresh_database) == 0, "the advisory lock to be freed")
    code, output = _run_migrate(_database_url(fresh_database))

    assert code == EXIT_OK, output
    assert _query(fresh_database, "SELECT version_num FROM alembic_version") == [
        (expected_head(),)
    ]
