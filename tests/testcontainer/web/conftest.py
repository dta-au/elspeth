"""Shared fixtures for PostgreSQL-backed web deployment acceptance."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator

import pytest
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]
from xdist import is_xdist_worker

_SEQUENTIAL_TEST_COMMAND = (
    "CI=1 uv run --frozen pytest -q -n 0 -m testcontainer "
    "tests/testcontainer/web/test_schema_probe_postgres.py "
    "tests/testcontainer/web/test_external_deployment_postgres.py "
    "tests/testcontainer/web/test_aws_ecs_validate_only_startup.py "
    "tests/testcontainer/web/test_doctor_aws_ecs_postgres.py "
    "tests/testcontainer/web/test_aws_ecs_readiness_postgres.py "
    "tests/testcontainer/web/test_landscape_write_gate_postgres.py"
)


def _require_sequential_postgres_acceptance(request: pytest.FixtureRequest) -> None:
    """Reject xdist workers before any PostgreSQL container is constructed."""
    if is_xdist_worker(request) or os.environ.get("PYTEST_XDIST_WORKER") is not None:
        raise pytest.UsageError(
            f"The PostgreSQL deployment acceptance suite must run sequentially to share one container. Run: {_SEQUENTIAL_TEST_COMMAND}"
        )


@pytest.fixture(scope="session")
def external_deployment_postgres_url(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Provide one authenticated-TLS PostgreSQL container for the acceptance suite."""
    _require_sequential_postgres_acceptance(request)
    tls_dir = tmp_path_factory.mktemp("external-postgres-tls")
    certificate = tls_dir / "server.crt"
    private_key = tls_dir / "server.key"
    entrypoint = tls_dir / "tls-entrypoint.sh"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    entrypoint.write_text(
        """#!/bin/sh
set -eu
mkdir -p /var/lib/postgresql/tls
cp /tls-source/server.crt /var/lib/postgresql/tls/server.crt
cp /tls-source/server.key /var/lib/postgresql/tls/server.key
chown -R postgres:postgres /var/lib/postgresql/tls
chmod 600 /var/lib/postgresql/tls/server.key
exec /usr/local/bin/docker-entrypoint.sh "$@"
""",
        encoding="utf-8",
    )

    volumes = [
        (str(certificate), "/tls-source/server.crt", "ro"),
        (str(private_key), "/tls-source/server.key", "ro"),
        (str(entrypoint), "/tls-source/tls-entrypoint.sh", "ro"),
    ]
    command = "postgres -c ssl=on -c ssl_cert_file=/var/lib/postgresql/tls/server.crt -c ssl_key_file=/var/lib/postgresql/tls/server.key"
    with PostgresContainer(
        "postgres:16-alpine",
        driver="psycopg",
        command=command,
        entrypoint=["/bin/sh", "/tls-source/tls-entrypoint.sh"],
        volumes=volumes,
    ) as postgres:
        tls_url = make_url(postgres.get_connection_url()).update_query_dict({"sslmode": "verify-full", "sslrootcert": str(certificate)})
        yield tls_url.render_as_string(hide_password=False)
