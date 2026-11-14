"""Schema migration support: the deploy-time runner's logic and the API's check.

Migrations run exactly once per deployment from a dedicated process
(scripts/migrate.py, the `migrate` Compose service), never from API replicas.
The API only verifies the result at startup, so code never serves traffic
against a schema older than it expects.
"""

import ast
import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

EXIT_OK = 0
EXIT_MIGRATION_FAILED = 1
EXIT_DESTRUCTIVE_REFUSED = 2
EXIT_DATABASE_UNAVAILABLE = 3
# --check only: the database is behind and `scripts.migrate` would change it.
EXIT_MIGRATIONS_PENDING = 4
# Another runner held the migration lock past MIGRATE_LOCK_WAIT_SECONDS.
EXIT_LOCK_TIMEOUT = 5
# The database is at a revision this build doesn't know and wasn't told to accept.
EXIT_UNKNOWN_REVISION = 6

_BACKEND_ROOT = Path(__file__).resolve().parents[2]

# Dropping an index or constraint loses no data and can be recreated, so only
# operations that destroy rows or columns count.
_DESTRUCTIVE_OP_CALLS = {"drop_table", "drop_column"}
_DESTRUCTIVE_SQL = re.compile(
    r"\b(DROP\s+(TABLE|COLUMN|SCHEMA|DATABASE)|TRUNCATE|DELETE\s+FROM)\b", re.IGNORECASE
)


class SchemaNotMigratedError(RuntimeError):
    """The database schema is behind what this build of the code requires, or at a
    revision it doesn't know and wasn't told to accept."""


def accepted_newer_revisions() -> set[str]:
    """Revisions this build doesn't know but may run on: SCHEMA_ACCEPT_NEWER_REVISIONS.

    A revision the code doesn't recognize is normal just after rolling the app back
    past a migration, and abnormal otherwise (a mistyped `alembic stamp`, a database
    from another branch). Telling them apart needs the operator, so an unknown
    revision is refused unless it is named here, for the duration of the rollback.
    """
    raw = settings.SCHEMA_ACCEPT_NEWER_REVISIONS
    return {part.strip() for part in raw.split(",") if part.strip()}


def alembic_config() -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


def expected_head() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError("No Alembic migrations found.")
    return head


def sync_database_url(async_url: str) -> str:
    """The app's asyncpg URL in the form Alembic's psycopg driver accepts.

    The two drivers spell TLS differently: asyncpg takes ssl=require (as used
    on Neon), while libpq rejects that and wants sslmode=require.
    """
    url = async_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    return re.sub(r"([?&])ssl=", r"\1sslmode=", url)


def destructive_operations(migration_source: str) -> list[str]:
    """Data-destroying operations in a migration's upgrade().

    A safety net against an unreviewed drop reaching production, not a proof
    of safety: it can't see SQL built at runtime or a lossy ALTER COLUMN TYPE.
    """
    tree = ast.parse(migration_source)
    upgrade = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
        ),
        None,
    )
    if upgrade is None:
        return []

    found: list[str] = []
    for node in ast.walk(upgrade):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _DESTRUCTIVE_OP_CALLS:
                found.append(f"op.{node.func.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            match = _DESTRUCTIVE_SQL.search(node.value)
            if match:
                found.append(f"SQL: {match.group(0)}")
    return found


async def assert_schema_is_current(session: AsyncSession) -> None:
    """Refuse to serve traffic on a schema older than this code expects.

    A revision this build doesn't recognize is accepted only if it is listed in
    SCHEMA_ACCEPT_NEWER_REVISIONS: that is how a rollback past a migration runs on the
    newer schema (migrations are written to stay compatible with the previous
    release), without also accepting an arbitrary or mistyped revision.
    """
    script = ScriptDirectory.from_config(alembic_config())
    head = script.get_current_head()
    hint = "Run the migration step first: python -m scripts.migrate"

    has_table = await session.scalar(text("SELECT to_regclass('alembic_version') IS NOT NULL"))
    if not has_table:
        raise SchemaNotMigratedError(f"Database has no migration history. {hint}")

    current = await session.scalar(text("SELECT version_num FROM alembic_version"))
    if current == head:
        return
    if current is None:
        raise SchemaNotMigratedError(f"Database has no applied revision. {hint}")
    known = {revision.revision for revision in script.walk_revisions()}
    if current in known:
        raise SchemaNotMigratedError(
            f"Database is at revision {current} but this build requires {head}. {hint}"
        )
    if current not in accepted_newer_revisions():
        raise SchemaNotMigratedError(
            f"Database is at revision {current}, which this build doesn't know. If this "
            "is a rollback past a migration, set SCHEMA_ACCEPT_NEWER_REVISIONS="
            f"{current} until the newer code is deployed again; otherwise find out how "
            "the database got there before serving traffic."
        )
