"""One PostgreSQL target seam for every ``testcontainer``-marked suite.

The ``testcontainer`` selection is the tree's only proof of PostgreSQL
behaviour, and the acceptance drivers re-run it against the PostgreSQL they
provisioned (Flexible Server, RDS) and store the outcome as a
``testcontainer-run`` receipt (elspeth-0ec6918940). That receipt can only
say "provisioned" honestly if EVERY suite in the selection reached the
provisioned server, so this module is the single place a suite obtains a
PostgreSQL URL from, and
``tests/unit/architecture/test_postgres_test_target_authority.py`` pins that
no ``PostgresContainer`` is constructed anywhere else under ``tests/``.

Two branches, chosen by :data:`PROVISIONED_POSTGRES_URL_ENV`:

* unset — a ``PostgresContainer`` exactly as the suites constructed it
  before this seam existed (same image, same driver, the caller's own
  container arguments), unless a paired per-run resource assignment
  replaces the caller's existing CPU or memory cap independently. Missing
  and zero caps remain uncapped. CI's required testcontainer job and a
  developer's ``pytest tests/ -m
  testcontainer -n 0`` run this branch.
* set — the provisioned server. The value is an admin-capable SQLAlchemy
  PostgreSQL URL (the suites ``CREATE DATABASE`` / ``CREATE ROLE`` and
  ``pg_terminate_backend`` other roles' backends, so it must carry a role
  with those rights: the Flexible Server admin, the RDS master user). Each
  entry creates a fresh ``elspeth_tc_<hex>`` database on that server and
  yields the admin URL re-pointed at it with the caller's driver, so the
  per-fixture isolation the container branch gets for free (a fresh
  cluster) is kept as a fresh database; the database is dropped on exit.
  Query parameters (``sslmode``, ``sslrootcert``) are preserved, and a
  caller that needs authenticated TLS says so with ``require_tls`` and is
  refused a plain URL.

The value is read from the environment by name only; it is never logged,
never written into a receipt, and the receipt-side identity
(``elspeth.web._acceptance_common.testcontainer_run``) hashes host, port
and database, never credentials.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from docker.utils import parse_bytes
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

PROVISIONED_POSTGRES_URL_ENV = "ELSPETH_TEST_POSTGRES_URL"
"""The environment variable naming a provisioned PostgreSQL for the whole ``testcontainer`` selection."""

POSTGRES_NANO_CPUS_ENV = "ELSPETH_TEST_POSTGRES_NANO_CPUS"
POSTGRES_MEMORY_BYTES_ENV = "ELSPETH_TEST_POSTGRES_MEMORY_BYTES"

POSTGRES_IMAGE = "postgres:16-alpine"
"""The image every suite used before the seam; the container branch keeps it."""

_DATABASE_PREFIX = "elspeth_tc_"


def _positive_ascii_integer(name: str, raw: str) -> int:
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise pytest.UsageError(f"{name} must be a positive ASCII integer")
    try:
        value = int(raw)
    except ValueError:
        raise pytest.UsageError(f"{name} must be a supported positive ASCII integer") from None
    if value <= 0:
        raise pytest.UsageError(f"{name} must be a positive ASCII integer")
    return value


def postgres_container_resource_assignment() -> tuple[int, int] | None:
    """Read an optional exact CPU/memory assignment for Docker targets only.

    Both variables must be absent or carry positive ASCII decimal integers.
    No allocation, Docker range adjustment, or per-run default is inferred.
    """
    cpu = os.environ.get(POSTGRES_NANO_CPUS_ENV)
    memory = os.environ.get(POSTGRES_MEMORY_BYTES_ENV)
    if cpu is None and memory is None:
        return None
    if cpu is None or memory is None:
        raise pytest.UsageError(f"{POSTGRES_NANO_CPUS_ENV} and {POSTGRES_MEMORY_BYTES_ENV} must be set together")
    return (
        _positive_ascii_integer(POSTGRES_NANO_CPUS_ENV, cpu),
        _positive_ascii_integer(POSTGRES_MEMORY_BYTES_ENV, memory),
    )


def _docker_container_kwargs(container_kwargs: dict[str, Any]) -> dict[str, Any]:
    assignment = postgres_container_resource_assignment()
    result = dict(container_kwargs)
    if assignment is None:
        return result
    cpu, memory = assignment
    previous_cpu = result.get("nano_cpus")
    # Do not turn invalid caller types or negative limits into valid requests.
    if isinstance(previous_cpu, int) and not isinstance(previous_cpu, bool) and previous_cpu > 0:
        result["nano_cpus"] = cpu
    previous_memory = result.get("mem_limit")
    if previous_memory is not None:
        # Match Docker's unit-string/zero semantics without parsing the new
        # assignment through its floating-point size conversion. Invalid
        # caller sizes retain the SDK's refusal rather than gaining a cap.
        parsed_memory = parse_bytes(previous_memory)
        if isinstance(parsed_memory, int) and not isinstance(parsed_memory, bool) and parsed_memory > 0:
            result["mem_limit"] = memory
    return result


def provisioned_postgres_url(*, require_tls: bool = False) -> URL | None:
    """Return the provisioned server's admin URL, or ``None`` when the selection runs on testcontainers.

    Raises :class:`pytest.UsageError` for a value that cannot serve the
    suites: not a PostgreSQL URL, missing the host / role / password /
    database an admin connection needs, or (``require_tls``) missing the
    ``sslmode=verify-full`` + ``sslrootcert`` pair the deployment-acceptance
    suites read back from the URL.
    """
    raw = os.environ.get(PROVISIONED_POSTGRES_URL_ENV)
    if raw is None or raw.strip() == "":
        return None
    try:
        url = make_url(raw)
    except Exception as exc:  # sqlalchemy raises ArgumentError; anything else is equally unusable
        raise pytest.UsageError(f"{PROVISIONED_POSTGRES_URL_ENV} is not a SQLAlchemy URL: {type(exc).__name__}") from None
    if url.get_backend_name() != "postgresql":
        raise pytest.UsageError(f"{PROVISIONED_POSTGRES_URL_ENV} must be a postgresql URL, got backend {url.get_backend_name()!r}")
    if not url.host or not url.username or url.password is None or not url.database:
        raise pytest.UsageError(f"{PROVISIONED_POSTGRES_URL_ENV} must carry host, username, password and an admin database")
    if require_tls and (url.query.get("sslmode") != "verify-full" or not url.query.get("sslrootcert")):
        raise pytest.UsageError(
            f"{PROVISIONED_POSTGRES_URL_ENV} must carry sslmode=verify-full and sslrootcert for the deployment-acceptance suites"
        )
    return url


@contextmanager
def postgres_test_target(*, driver: str = "psycopg2", require_tls: bool = False, **container_kwargs: Any) -> Iterator[str]:
    """Yield a PostgreSQL connection URL for one suite or fixture.

    ``driver`` is the SQLAlchemy driver name the suite connects with
    (``psycopg2`` is testcontainers' own default and what the suites that
    passed no driver were getting; ``psycopg`` for the ones that asked for
    it). ``container_kwargs`` reach ``PostgresContainer`` unchanged unless
    the paired assignment replaces an existing nonzero CPU or memory cap.
    Swap and all other options are preserved. The provisioned branch ignores
    container arguments and assignments: that server is not ours to configure.
    """
    admin = provisioned_postgres_url(require_tls=require_tls)
    if admin is None:
        assigned_kwargs = _docker_container_kwargs(container_kwargs)
        with PostgresContainer(POSTGRES_IMAGE, driver=driver, **assigned_kwargs) as postgres:
            yield postgres.get_connection_url()
        return

    database = f"{_DATABASE_PREFIX}{uuid.uuid4().hex}"
    admin_url = admin.set(drivername=f"postgresql+{driver}")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
        try:
            yield admin_url.set(database=database).render_as_string(hide_password=False)
        finally:
            with admin_engine.connect() as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
    finally:
        admin_engine.dispose()
