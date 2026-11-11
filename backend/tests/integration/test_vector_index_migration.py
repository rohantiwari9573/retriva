"""The vector-index migration must give up on a held lock instead of queueing.

Each test migrates its own throwaway database in a separate process, the way a deploy
runs Alembic, while a second connection holds a conflicting lock where needed.
"""

import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from sqlalchemy.engine import make_url

from app.core.config import settings

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BEFORE = "6f45bc893ebc"
AFTER = "b4f2c81d9e37"
INDEX = "ix_document_chunks_embedding_cosine"


def _libpq_url(database: str | None = None) -> str:
    url = make_url(settings.DATABASE_URL).set(drivername="postgresql")
    if database is not None:
        url = url.set(database=database)
    return url.render_as_string(hide_password=False)


@pytest.fixture
def database() -> Iterator[str]:
    name = f"vector_migration_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_libpq_url(), autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield name
    finally:
        with psycopg.connect(_libpq_url(), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _alembic(database: str, *args: str) -> subprocess.CompletedProcess[str]:
    async_url = make_url(settings.DATABASE_URL).set(database=database)
    env = {**os.environ, "DATABASE_URL": async_url.render_as_string(hide_password=False)}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _state(database: str) -> tuple[str, bool]:
    with psycopg.connect(_libpq_url(database), autocommit=True) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        index = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (INDEX,)).fetchone()
    assert version is not None and index is not None
    return version[0], index[0]


def _assert_nothing_left_behind(database: str) -> None:
    with psycopg.connect(_libpq_url(database), autocommit=True) as conn:
        # A closed connection's server process can take a moment to exit.
        deadline = time.monotonic() + 10
        while True:
            others = conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            ).fetchone()
            if others == (0,) or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        stored_settings = conn.execute(
            "SELECT count(*) FROM pg_db_role_setting s JOIN pg_database d "
            "ON d.oid = s.setdatabase WHERE d.datname = current_database()"
        ).fetchone()
        lock_timeout = conn.execute("SHOW lock_timeout").fetchone()
    assert others == (0,), "a migration session is still open"
    assert stored_settings == (0,), "the migration changed a persistent setting"
    assert lock_timeout == ("0",), "the lock timeout leaked beyond the migration"


def _migrate_while_locked(database: str, lock_mode: str, *args: str):
    blocker = psycopg.connect(_libpq_url(database))
    try:
        # Opens a transaction that holds the lock until rollback.
        blocker.execute(f"LOCK TABLE document_chunks IN {lock_mode} MODE")
        started = time.monotonic()
        result = _alembic(database, *args)
        elapsed = time.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()
    return result, elapsed


def test_upgrade_drops_the_index_when_nothing_holds_a_lock(database) -> None:
    assert _alembic(database, "upgrade", BEFORE).returncode == 0
    assert _state(database) == (BEFORE, True)

    result = _alembic(database, "upgrade", AFTER)

    assert result.returncode == 0, result.stderr
    assert _state(database) == (AFTER, False)
    _assert_nothing_left_behind(database)


def test_upgrade_gives_up_on_a_held_lock_and_changes_nothing(database) -> None:
    assert _alembic(database, "upgrade", BEFORE).returncode == 0

    # Any open transaction that has read the table, such as ingestion, holds this.
    result, elapsed = _migrate_while_locked(database, "ACCESS SHARE", "upgrade", AFTER)

    assert result.returncode != 0
    assert "lock timeout" in result.stderr
    assert 4 <= elapsed < 60, f"took {elapsed:.1f}s: the timeout isn't 5 seconds"
    assert _state(database) == (BEFORE, True)
    _assert_nothing_left_behind(database)

    # Retrying once the lock is gone succeeds.
    retry = _alembic(database, "upgrade", AFTER)
    assert retry.returncode == 0, retry.stderr
    assert _state(database) == (AFTER, False)


def test_downgrade_gives_up_on_a_held_lock_and_changes_nothing(database) -> None:
    assert _alembic(database, "upgrade", AFTER).returncode == 0

    # Writers hold this, and CREATE INDEX must wait for them.
    result, elapsed = _migrate_while_locked(database, "ROW EXCLUSIVE", "downgrade", BEFORE)

    assert result.returncode != 0
    assert "lock timeout" in result.stderr
    assert 4 <= elapsed < 60
    assert _state(database) == (AFTER, False)
    _assert_nothing_left_behind(database)

    retry = _alembic(database, "downgrade", BEFORE)
    assert retry.returncode == 0, retry.stderr
    assert _state(database) == (BEFORE, True)


def test_timeout_is_transaction_local_and_set_before_the_drop() -> None:
    result = _alembic("unused", "upgrade", f"{BEFORE}:{AFTER}", "--sql")

    assert result.returncode == 0, result.stderr
    sql = result.stdout
    begin = sql.index("BEGIN;")
    set_timeout = sql.index("SET LOCAL lock_timeout = '5s';")
    drop = sql.index(f"DROP INDEX IF EXISTS {INDEX};")
    reset = sql.index("SET LOCAL lock_timeout TO DEFAULT;")
    commit = sql.index("COMMIT;")
    assert begin < set_timeout < drop < reset < commit
    assert "ALTER DATABASE" not in sql and "ALTER ROLE" not in sql
