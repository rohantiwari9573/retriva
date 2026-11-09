"""Dedicated migration step: wait for Postgres, migrate, verify, exit.

Runs once per deployment, as the `migrate` Compose service locally and as a
manual pre-deploy command for production (see DEPLOYMENT.md). Exit codes are in
app.core.migrations. Use a direct (non-pooled) connection: the session-level
advisory lock doesn't work through a transaction-mode pooler.

    python -m scripts.migrate                       # upgrade to head
    python -m scripts.migrate --check               # report pending work, change nothing
    python -m scripts.migrate --allow-destructive   # permit a reviewed drop
"""

import argparse
import os
import sys
import time
from pathlib import Path

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from alembic import command
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.migrations import (
    EXIT_DATABASE_UNAVAILABLE,
    EXIT_DESTRUCTIVE_REFUSED,
    EXIT_MIGRATION_FAILED,
    EXIT_OK,
    alembic_config,
    destructive_operations,
    expected_head,
    sync_database_url,
)

logger = get_logger(__name__)

# Arbitrary fixed key: concurrent runners queue here instead of colliding.
_MIGRATION_LOCK_KEY = 727_190_001


def _connect_with_retry(engine, wait_seconds: float) -> Connection | None:
    deadline = time.monotonic() + wait_seconds
    delay = 0.5
    while True:
        try:
            return engine.connect()
        except OperationalError as exc:
            if time.monotonic() >= deadline:
                logger.error("migration_database_unavailable", reason=str(exc).splitlines()[0])
                return None
            logger.info("migration_waiting_for_database")
            time.sleep(delay)
            delay = min(delay * 2, 5.0)


def _current_revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def _is_unknown_revision(current: str | None) -> bool:
    if current is None:
        return False
    script = ScriptDirectory.from_config(alembic_config())
    return current not in {revision.revision for revision in script.walk_revisions()}


def _pending_destructive_operations(current: str | None) -> dict[str, list[str]]:
    script = ScriptDirectory.from_config(alembic_config())
    pending = script.iterate_revisions("heads", current or "base")
    findings = {}
    for revision in pending:
        operations = destructive_operations(Path(revision.path).read_text(encoding="utf-8"))
        if operations:
            findings[revision.revision] = operations
    return findings


def run(*, allow_destructive: bool, check_only: bool, wait_seconds: float) -> int:
    engine = create_engine(
        sync_database_url(settings.DATABASE_URL),
        poolclass=NullPool,
        connect_args={"connect_timeout": 5},
    )
    connection = _connect_with_retry(engine, wait_seconds)
    if connection is None:
        return EXIT_DATABASE_UNAVAILABLE

    head = expected_head()
    try:
        with connection.execution_options(isolation_level="AUTOCOMMIT") as lock_connection:
            lock_connection.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY}
            )
            current = _current_revision(lock_connection)
            logger.info("migration_state", current=current, target=head)

            if current == head:
                logger.info("migration_nothing_to_do", revision=head)
                return EXIT_OK

            # A revision this build doesn't know means the database is newer, as after
            # rolling the code back. Nothing here can be applied, and the API's startup
            # check accepts this state too, so it isn't a failure.
            if _is_unknown_revision(current):
                logger.warning("migration_database_newer", current=current, target=head)
                return EXIT_OK

            destructive = _pending_destructive_operations(current)
            if destructive and not allow_destructive:
                logger.error("migration_refused_destructive", operations=destructive)
                return EXIT_DESTRUCTIVE_REFUSED
            if check_only:
                logger.info("migration_pending", current=current, target=head)
                return EXIT_OK

            command.upgrade(alembic_config(), "head")

            applied = _current_revision(lock_connection)
            if applied != head:
                logger.error("migration_verification_failed", current=applied, target=head)
                return EXIT_MIGRATION_FAILED
            logger.info("migration_complete", previous=current, current=applied)
            return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - every failure must become an exit code
        logger.error("migration_failed", exc_info=exc)
        return EXIT_MIGRATION_FAILED
    finally:
        # Closing the session releases the advisory lock.
        connection.close()
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-destructive", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    configure_logging()
    return run(
        allow_destructive=args.allow_destructive,
        check_only=args.check,
        wait_seconds=float(os.environ.get("MIGRATE_DB_WAIT_SECONDS", "60")),
    )


if __name__ == "__main__":
    sys.exit(main())
