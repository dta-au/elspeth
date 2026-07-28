"""Shared fixtures for PostgreSQL-backed web deployment acceptance."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]
from xdist import is_xdist_worker

from elspeth.web import aws_rds_trust

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
            "-addext",
            "basicConstraints=critical,CA:TRUE",
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
    # testcontainers' default admin credentials are literally "test"/"test",
    # which collides with the substring "test" inside pytest's own tmp-dir
    # naming (`pytest-of-<user>/pytest-<n>/...`) once the doctor's rds_trust_root
    # check starts reporting that tmp-dir path in its detail string (see
    # aws_rds_trust_test_override below). Hex-only credentials cannot contain
    # "test" (the hex alphabet excludes both "t" and "s"), so the existing
    # redaction assertions stay meaningful instead of tripping on this
    # test-harness coincidence.
    admin_username = f"elspeth_admin_{uuid.uuid4().hex}"
    admin_password = uuid.uuid4().hex
    admin_dbname = f"elspeth_admin_{uuid.uuid4().hex}"
    with PostgresContainer(
        "postgres:16-alpine",
        driver="psycopg",
        username=admin_username,
        password=admin_password,
        dbname=admin_dbname,
        command=command,
        entrypoint=["/bin/sh", "/tls-source/tls-entrypoint.sh"],
        volumes=volumes,
    ) as postgres:
        tls_url = make_url(postgres.get_connection_url()).update_query_dict({"sslmode": "verify-full", "sslrootcert": str(certificate)})
        yield tls_url.render_as_string(hide_password=False)


@pytest.fixture
def aws_rds_trust_test_override(
    external_deployment_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = make_url(external_deployment_postgres_url)
    root = parsed.query["sslrootcert"]
    assert isinstance(root, str)
    path = Path(root)
    file_stat = path.stat()
    monkeypatch.setattr(aws_rds_trust, "AWS_RDS_GLOBAL_BUNDLE_PATH", path)
    monkeypatch.setattr(
        aws_rds_trust,
        "AWS_RDS_GLOBAL_BUNDLE_SHA256",
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        aws_rds_trust,
        "AWS_RDS_GLOBAL_BUNDLE_CERTIFICATE_COUNT",
        1,
    )
    monkeypatch.setattr(
        aws_rds_trust,
        "AWS_RDS_GLOBAL_BUNDLE_OWNER_UID",
        file_stat.st_uid,
    )
    monkeypatch.setattr(
        aws_rds_trust,
        "AWS_RDS_GLOBAL_BUNDLE_MODE",
        stat.S_IMODE(file_stat.st_mode),
    )


_SITECUSTOMIZE = '''\
"""Test-only RDS trust override injected via PYTHONPATH by the test suite."""
import os

if os.environ.get("ELSPETH_TEST_RDS_TRUST_PATH"):
    from pathlib import Path

    from elspeth.web import aws_rds_trust

    aws_rds_trust.AWS_RDS_GLOBAL_BUNDLE_PATH = Path(
        os.environ["ELSPETH_TEST_RDS_TRUST_PATH"]
    )
    aws_rds_trust.AWS_RDS_GLOBAL_BUNDLE_SHA256 = os.environ[
        "ELSPETH_TEST_RDS_TRUST_SHA256"
    ]
    aws_rds_trust.AWS_RDS_GLOBAL_BUNDLE_CERTIFICATE_COUNT = int(
        os.environ["ELSPETH_TEST_RDS_TRUST_COUNT"]
    )
    aws_rds_trust.AWS_RDS_GLOBAL_BUNDLE_OWNER_UID = int(
        os.environ["ELSPETH_TEST_RDS_TRUST_UID"]
    )
    aws_rds_trust.AWS_RDS_GLOBAL_BUNDLE_MODE = int(
        os.environ["ELSPETH_TEST_RDS_TRUST_MODE"]
    )
'''


@pytest.fixture
def aws_rds_trust_subprocess_env(
    external_deployment_postgres_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, str]:
    parsed = make_url(external_deployment_postgres_url)
    root = parsed.query["sslrootcert"]
    assert isinstance(root, str)
    path = Path(root)
    file_stat = path.stat()
    shim_dir = tmp_path_factory.mktemp("rds-trust-shim")
    (shim_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    return {
        "PYTHONPATH": str(shim_dir),
        "ELSPETH_TEST_RDS_TRUST_PATH": str(path),
        "ELSPETH_TEST_RDS_TRUST_SHA256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "ELSPETH_TEST_RDS_TRUST_COUNT": "1",
        "ELSPETH_TEST_RDS_TRUST_UID": str(file_stat.st_uid),
        "ELSPETH_TEST_RDS_TRUST_MODE": str(stat.S_IMODE(file_stat.st_mode)),
    }
