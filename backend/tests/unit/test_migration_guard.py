"""R0.1: pure logic of the migration step (no database)."""

from pathlib import Path

import pytest

from app.core.migrations import destructive_operations, sync_database_url

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


@pytest.mark.parametrize(
    "body",
    [
        'op.drop_table("documents")',
        'op.drop_column("documents", "status")',
        'op.execute("DROP TABLE documents")',
        'op.execute("ALTER TABLE documents DROP COLUMN status")',
        'op.execute("truncate documents")',
        'op.execute("DELETE FROM documents")',
        'op.execute(sa.text("DROP TABLE documents"))',
    ],
)
def test_data_losing_operations_in_upgrade_are_detected(body: str) -> None:
    source = f"def upgrade() -> None:\n    {body}\n\ndef downgrade() -> None:\n    pass\n"
    assert destructive_operations(source)


@pytest.mark.parametrize(
    "body",
    [
        'op.create_table("x")',
        'op.drop_index("ix_x")',
        'op.drop_constraint("uq_x", "x")',
        'op.execute("CREATE INDEX ix ON x (y)")',
    ],
)
def test_recoverable_operations_are_not_flagged(body: str) -> None:
    source = f"def upgrade() -> None:\n    {body}\n"
    assert destructive_operations(source) == []


def test_drops_in_downgrade_do_not_count() -> None:
    source = (
        "def upgrade() -> None:\n    pass\n\n"
        'def downgrade() -> None:\n    op.drop_table("x")\n'
    )
    assert destructive_operations(source) == []


def test_no_existing_migration_is_destructive() -> None:
    """If this fails, a fresh install would refuse to start."""
    for path in VERSIONS.glob("*.py"):
        assert destructive_operations(path.read_text(encoding="utf-8")) == [], path.name


@pytest.mark.parametrize(
    ("async_url", "sync_url"),
    [
        (
            "postgresql+asyncpg://u:p@db:5432/x",
            "postgresql+psycopg://u:p@db:5432/x",
        ),
        (
            # Production form: asyncpg spells it ssl=, psycopg rejects that.
            "postgresql+asyncpg://u:p@host.neon.tech/x?ssl=require",
            "postgresql+psycopg://u:p@host.neon.tech/x?sslmode=require",
        ),
        (
            "postgresql+asyncpg://u:p@host/x?ssl=require&application_name=a",
            "postgresql+psycopg://u:p@host/x?sslmode=require&application_name=a",
        ),
    ],
)
def test_sync_database_url(async_url: str, sync_url: str) -> None:
    assert sync_database_url(async_url) == sync_url
