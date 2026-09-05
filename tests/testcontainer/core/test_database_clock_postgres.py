"""PostgreSQL twin of the Landscape database-clock pins (ADR-047, C6.0).

What SQLite cannot show: a ``timestamptz`` under a non-UTC session time zone
comes back as the same instant in UTC; every read inside one transaction is
the same instant (transaction time, the property the in-SQL fence deadline
relies on) and differs from ``clock_timestamp()``; a later transaction reads
a later instant.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.database_clock import read_landscape_transaction_time

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def postgres_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as postgres:
        yield postgres.get_connection_url()


@pytest.fixture
def postgres_db(postgres_url: str) -> Iterator[LandscapeDB]:
    db = LandscapeDB(postgres_url)
    try:
        yield db
    finally:
        db.close()


def test_non_utc_session_time_zone_reads_as_the_same_instant_in_utc(postgres_db: LandscapeDB) -> None:
    with postgres_db.engine.begin() as conn:
        conn.execute(text("SET TIME ZONE 'Australia/Sydney'"))
        moment = read_landscape_transaction_time(conn)
        session_local = conn.scalar(text("SELECT CURRENT_TIMESTAMP"))
        utc_naive = conn.scalar(text("SELECT CURRENT_TIMESTAMP AT TIME ZONE 'UTC'"))
    assert moment.tzinfo is UTC
    assert moment.utcoffset() == timedelta(0)
    assert type(session_local) is datetime and session_local.tzinfo is not None
    assert session_local.utcoffset() in {timedelta(hours=10), timedelta(hours=11)}
    assert moment == session_local
    assert type(utc_naive) is datetime and utc_naive.tzinfo is None
    assert moment == utc_naive.replace(tzinfo=UTC)


def test_every_read_inside_one_transaction_is_the_transaction_instant(postgres_db: LandscapeDB) -> None:
    with postgres_db.engine.begin() as conn:
        first = read_landscape_transaction_time(conn)
        conn.execute(text("SELECT pg_sleep(0.05)"))
        second = read_landscape_transaction_time(conn)
        wall = conn.scalar(text("SELECT clock_timestamp()"))
    assert first == second
    assert type(wall) is datetime and wall.tzinfo is not None
    assert wall.astimezone(UTC) > first


def test_a_later_transaction_reads_a_later_instant(postgres_db: LandscapeDB) -> None:
    with postgres_db.engine.begin() as conn:
        first = read_landscape_transaction_time(conn)
    with postgres_db.engine.begin() as conn:
        conn.execute(text("SELECT pg_sleep(0.05)"))
        second = read_landscape_transaction_time(conn)
    assert second > first
