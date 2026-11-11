"""The destructive vector experiment must refuse every target but a local test database."""

import pytest

from scripts import vector_search_experiment
from scripts.local_test_database import UnsafeTargetError, check_target

PASSWORD = "s3cret-Pa55word"
NAME = "vector_experiment"

LOOPBACK = {"localhost": ["127.0.0.1", "::1"]}
COMPOSE = {"postgres": ["172.18.0.2"]}
PUBLIC = "93.184.216.34"
NEON = "ep-cool-name-123456.us-east-2.aws.neon.tech"
NEON_POOLER = "ep-cool-name-123456-pooler.eu-central-1.aws.neon.tech"


def _resolver(table: dict[str, list[str]]):
    def resolve(host: str, port: int) -> list[str]:
        if host not in table:
            raise OSError("not found")
        return table[host]

    return resolve


def _check(url, confirm=NAME, *, environ=None, table=None):
    return check_target(
        url,
        confirm,
        environ=environ or {},
        resolver=_resolver({**LOOPBACK, **COMPOSE, **(table or {})}),
    )


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://nexus:{PASSWORD}@localhost:5432/{NAME}",
        f"postgresql+asyncpg://nexus:{PASSWORD}@localhost/{NAME}",
        f"postgresql+psycopg://nexus:{PASSWORD}@127.0.0.1:5432/{NAME}",
        f"postgresql://nexus:{PASSWORD}@127.0.0.2:5432/{NAME}",
        f"postgresql://nexus:{PASSWORD}@[::1]:5432/{NAME}",
        f"postgresql+asyncpg://nexus:{PASSWORD}@postgres:5432/{NAME}",
        f"postgresql://nexus:{PASSWORD}@localhost:5432/{NAME}_ci?sslmode=disable",
    ],
)
def test_accepts_documented_local_targets(url: str) -> None:
    database = url.rsplit("/", 1)[1].split("?")[0]

    target = _check(url, database)

    assert target.url.database == database


@pytest.mark.parametrize(
    ("url", "table"),
    [
        # Remote and cloud hosts, by name and by address.
        (f"postgresql://u:{PASSWORD}@db.example.com/{NAME}", {"db.example.com": [PUBLIC]}),
        (f"postgresql://neondb_owner:{PASSWORD}@{NEON}/{NAME}", {NEON: ["3.18.0.1"]}),
        (f"postgresql://u:{PASSWORD}@{NEON_POOLER}/{NAME}", {NEON_POOLER: ["3.18.0.1"]}),
        (f"postgresql://u:{PASSWORD}@dpg-abc.oregon-postgres.render.com/{NAME}", {}),
        (f"postgresql://u:{PASSWORD}@8.8.8.8/{NAME}", {}),
        (f"postgresql://u:{PASSWORD}@[2001:db8::1]/{NAME}", {}),
        # Private but not loopback: another machine on the network.
        (f"postgresql://u:{PASSWORD}@10.0.0.5/{NAME}", {}),
        (f"postgresql://u:{PASSWORD}@192.168.1.20/{NAME}", {}),
        # Names that only look local, or local names that resolve elsewhere.
        (
            f"postgresql://u:{PASSWORD}@localhost.example.com/{NAME}",
            {"localhost.example.com": ["127.0.0.1"]},
        ),
        (f"postgresql://u:{PASSWORD}@test-db/{NAME}", {"test-db": ["127.0.0.1"]}),
        (
            f"postgresql://u:{PASSWORD}@host.docker.internal/{NAME}",
            {"host.docker.internal": ["192.168.65.254"]},
        ),
        (f"postgresql://u:{PASSWORD}@localhost/{NAME}", {"localhost": ["127.0.0.1", PUBLIC]}),
        (f"postgresql://u:{PASSWORD}@postgres/{NAME}", {"postgres": [PUBLIC]}),
        (f"postgresql://u:{PASSWORD}@[::ffff:8.8.8.8]/{NAME}", {}),
        # Unresolvable.
        (f"postgresql://u:{PASSWORD}@localhost/{NAME}", {"localhost": []}),
    ],
)
def test_rejects_remote_cloud_and_unknown_hosts(url: str, table) -> None:
    with pytest.raises(UnsafeTargetError) as raised:
        _check(url, table=table)

    assert PASSWORD not in str(raised.value)


def test_rejects_a_host_that_does_not_resolve() -> None:
    def failing(host: str, port: int) -> list[str]:
        raise OSError("lookup failed")

    with pytest.raises(UnsafeTargetError, match="could not be resolved"):
        check_target(f"postgresql://u:p@localhost/{NAME}", NAME, environ={}, resolver=failing)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "not a url",
        "postgresql://",
        f"postgresql:///{NAME}",  # no host: libpq would pick a socket or PGHOST
        f"postgresql://u:{PASSWORD}@/{NAME}?host=/var/run/postgresql",
        f"mysql://u:{PASSWORD}@localhost/{NAME}",
        f"sqlite:///{NAME}.db",
        f"postgresql://u:{PASSWORD}@localhost/{NAME}?host=db.example.com",
        f"postgresql://u:{PASSWORD}@localhost/{NAME}?hostaddr=93.184.216.34",
        f"postgresql://u:{PASSWORD}@localhost/{NAME}?service=prod",
        f"postgresql://u:{PASSWORD}@localhost/{NAME}?sslmode=require",
        f"postgresql://u:{PASSWORD}@localhost,db.example.com/{NAME}",
    ],
)
def test_rejects_missing_malformed_and_ambiguous_urls(url) -> None:
    with pytest.raises(UnsafeTargetError) as raised:
        _check(url)

    assert PASSWORD not in str(raised.value)


@pytest.mark.parametrize(
    "database",
    [
        "nexus",
        "nexus_test",
        "neondb",
        "postgres",
        "retriva",
        "test",
        "local_vector_experiment",
        "vector_experiment-prod",
        "VECTOR_EXPERIMENT",
        "",
    ],
)
def test_rejects_databases_not_named_for_the_experiment(database: str) -> None:
    with pytest.raises(UnsafeTargetError, match="database name"):
        _check(f"postgresql://u:{PASSWORD}@localhost/{database}", database)


@pytest.mark.parametrize("confirm", [None, "", "vector_experiment_other", "nexus"])
def test_requires_the_database_name_to_be_confirmed(confirm) -> None:
    with pytest.raises(UnsafeTargetError, match="confirmed"):
        _check(f"postgresql://u:{PASSWORD}@localhost/{NAME}", confirm)


@pytest.mark.parametrize("variable", ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGSYSCONFDIR"])
def test_rejects_libpq_settings_that_could_redirect_the_connection(variable: str) -> None:
    with pytest.raises(UnsafeTargetError, match=variable):
        _check(f"postgresql://u:{PASSWORD}@localhost/{NAME}", environ={variable: "x"})


def test_confirmation_alone_is_not_enough() -> None:
    with pytest.raises(UnsafeTargetError):
        _check(
            f"postgresql://u:{PASSWORD}@db.example.com/{NAME}",
            table={"db.example.com": [PUBLIC]},
        )


# --- the script itself ------------------------------------------------------


@pytest.fixture
def no_database_access(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("the script touched a database")

    monkeypatch.setattr(vector_search_experiment.psycopg, "connect", forbidden)
    monkeypatch.setattr(vector_search_experiment.command, "downgrade", forbidden)
    monkeypatch.setattr(vector_search_experiment.command, "upgrade", forbidden)
    return calls


@pytest.mark.parametrize(
    "url",
    [
        None,
        f"postgresql+asyncpg://neondb_owner:{PASSWORD}@{NEON}/neondb?ssl=require",
        f"postgresql+asyncpg://nexus:{PASSWORD}@localhost:5433/nexus",
    ],
)
def test_script_exits_before_touching_any_database(
    url, monkeypatch, capsys, no_database_access, tmp_path
) -> None:
    if url is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(
        "sys.argv",
        [
            "vector_search_experiment",
            "--out",
            str(tmp_path / "out.json"),
            "--confirm-database",
            "neondb",
        ],
    )

    with pytest.raises(SystemExit) as exited:
        vector_search_experiment.main()

    assert exited.value.code == 2
    assert no_database_access == []
    error = capsys.readouterr().err
    assert error.startswith("Refusing to run:")
    assert PASSWORD not in error and "neon.tech" not in error and "localhost" not in error


def test_script_refuses_when_app_settings_name_another_database(
    monkeypatch, capsys, no_database_access, tmp_path
) -> None:
    """Alembic connects through the app settings, so they must match what was checked."""
    from app.core.config import settings

    checked = f"postgresql+asyncpg://nexus:{PASSWORD}@localhost:5432/{NAME}"
    monkeypatch.setenv("DATABASE_URL", checked)
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg://u:p@db.example.com/neondb")
    monkeypatch.setattr("scripts.local_test_database.resolve", lambda host, port: ["127.0.0.1"])
    monkeypatch.setattr(
        "sys.argv",
        [
            "vector_search_experiment",
            "--out",
            str(tmp_path / "out.json"),
            "--confirm-database",
            NAME,
        ],
    )

    with pytest.raises(SystemExit) as exited:
        vector_search_experiment.main()

    assert exited.value.code == 2
    assert no_database_access == []
    assert "different database" in capsys.readouterr().err
