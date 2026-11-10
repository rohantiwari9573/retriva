"""Decides whether a destructive script may touch the database it was given.

Fails closed: a target is accepted only when every check below positively passes,
and anything unrecognised is refused before any connection is opened. Errors name the
failed check, never the URL, host, user or password.

Accepted targets (the only supported setups):
- A Postgres on this machine: host `localhost`, `127.0.0.1` (any 127.0.0.0/8) or
  `::1`, where every address the name resolves to is a loopback address. Use this
  for a container started with `-p 127.0.0.1:5432:5432`.
- The `postgres` service of this repo's docker-compose.yml, when running inside the
  Compose network, where every address it resolves to is private.

And always:
- the database name is `vector_experiment` or starts with `vector_experiment_`,
  so the default databases (`nexus`, `nexus_test`, `neondb`, `postgres`) never match
- the caller confirms that exact database name separately
- no URL parameter or libpq environment variable can redirect the connection
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

LOOPBACK_NAMES = frozenset({"localhost"})
COMPOSE_SERVICE_NAMES = frozenset({"postgres"})
DRIVERS = frozenset({"postgresql", "postgresql+asyncpg", "postgresql+psycopg"})
DATABASE_NAME = re.compile(r"^vector_experiment(_[a-z0-9]+)*$")
# Local containers don't use TLS. Any other parameter could change the target
# (libpq honours host=, hostaddr= and service= in the query string), so none are allowed.
ALLOWED_QUERY = {"sslmode": frozenset({"disable"})}
# libpq reads these even when a URL names a host: PGHOSTADDR replaces the address,
# and a service file can supply other settings.
REDIRECTING_ENVIRONMENT = ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGSYSCONFDIR")

Resolver = Callable[[str, int], list[str]]
Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class UnsafeTargetError(Exception):
    """The target isn't positively a permitted local test database."""


@dataclass(frozen=True)
class LocalTestDatabase:
    url: URL

    @property
    def libpq_url(self) -> str:
        return self.url.set(drivername="postgresql").render_as_string(hide_password=False)


def resolve(host: str, port: int) -> list[str]:
    return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, port)})


def _addresses(host: str, port: int, resolver: Resolver) -> list[Address]:
    try:
        resolved = resolver(host, port)
    except OSError as exc:
        raise UnsafeTargetError("the database host could not be resolved") from exc
    if not resolved:
        raise UnsafeTargetError("the database host could not be resolved")
    try:
        return [ipaddress.ip_address(address.split("%", 1)[0]) for address in resolved]
    except ValueError as exc:
        raise UnsafeTargetError("the database host resolved to an unrecognised address") from exc


def _check_host(host: str, port: int, resolver: Resolver) -> None:
    try:
        literal: Address | None = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if not literal.is_loopback:
            raise UnsafeTargetError("the database host is not a loopback address")
        return
    if host in LOOPBACK_NAMES:
        if not all(address.is_loopback for address in _addresses(host, port, resolver)):
            raise UnsafeTargetError("localhost does not resolve only to loopback addresses")
        return
    if host in COMPOSE_SERVICE_NAMES:
        addresses = _addresses(host, port, resolver)
        if not all(address.is_private or address.is_loopback for address in addresses):
            raise UnsafeTargetError("the Compose database host resolves to a public address")
        return
    raise UnsafeTargetError("the database host is not a permitted local host")


def check_target(
    raw_url: str | None,
    confirmed_database: str | None,
    *,
    environ: Mapping[str, str],
    resolver: Resolver | None = None,
) -> LocalTestDatabase:
    if not raw_url:
        raise UnsafeTargetError("DATABASE_URL is not set")
    try:
        url = make_url(raw_url)
    except (ArgumentError, ValueError) as exc:
        raise UnsafeTargetError("DATABASE_URL is not a valid database URL") from exc

    if url.drivername not in DRIVERS:
        raise UnsafeTargetError("DATABASE_URL is not a supported Postgres URL")
    for name, value in url.query.items():
        values = (value,) if isinstance(value, str) else tuple(value)
        if name not in ALLOWED_QUERY or not set(values) <= ALLOWED_QUERY[name]:
            raise UnsafeTargetError("DATABASE_URL has a parameter that isn't permitted")
    for variable in REDIRECTING_ENVIRONMENT:
        if environ.get(variable):
            raise UnsafeTargetError(f"{variable} is set and could redirect the connection")

    host = (url.host or "").strip("[]")
    if not host:
        raise UnsafeTargetError("DATABASE_URL names no host")
    if "," in host:
        raise UnsafeTargetError("DATABASE_URL names more than one host")
    _check_host(host.lower(), url.port or 5432, resolver or resolve)

    database = url.database or ""
    if not DATABASE_NAME.fullmatch(database):
        raise UnsafeTargetError(
            "the database name must be vector_experiment or start with vector_experiment_"
        )
    if confirmed_database != database:
        raise UnsafeTargetError("the confirmed database name does not match DATABASE_URL")
    return LocalTestDatabase(url=url)
