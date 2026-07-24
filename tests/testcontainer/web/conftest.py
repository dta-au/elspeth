"""Shared fixtures for PostgreSQL-backed web deployment acceptance."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]
from xdist import is_xdist_worker

_SEQUENTIAL_TEST_COMMAND = (
    "CI=1 uv run --frozen pytest -q -n 0 -m testcontainer "
    "tests/testcontainer/web/test_external_deployment_postgres.py "
    "tests/testcontainer/web/test_aws_ecs_validate_only_startup.py "
    "tests/testcontainer/web/test_doctor_aws_ecs_postgres.py"
)


def _require_sequential_postgres_acceptance(request: pytest.FixtureRequest) -> None:
    """Reject xdist workers before any PostgreSQL container is constructed."""
    if is_xdist_worker(request) or os.environ.get("PYTEST_XDIST_WORKER") is not None:
        raise pytest.UsageError(
            f"The PostgreSQL deployment acceptance suite must run sequentially to share one container. Run: {_SEQUENTIAL_TEST_COMMAND}"
        )


@pytest.fixture(scope="session")
def external_deployment_postgres_url(request: pytest.FixtureRequest) -> Iterator[str]:
    """Provide exactly one PostgreSQL container for the focused acceptance suite."""
    _require_sequential_postgres_acceptance(request)
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as postgres:
        yield postgres.get_connection_url()
