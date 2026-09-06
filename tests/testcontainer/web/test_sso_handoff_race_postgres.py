"""Two ``complete`` calls racing one handoff code, on real PostgreSQL.

A handoff code authorises minting a session token, and it must do so exactly
once. The unit suite proves the single-use LOGIC, but it runs on SQLite, which
serialises writers — a race staged there passes whether or not the claim is
atomic, so it proves nothing about the property that matters.

This file stages the race the way production meets it: independent engines,
independent connections, released together on a barrier. A ``SELECT`` then an
``UPDATE`` loses here, and only here.

The race is not exotic. A double-submitted form, a retried request, or a user
who reloads the completion page produces two ``complete`` calls on one code as
a matter of course.
"""

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import Engine, create_engine, insert, select
from sqlalchemy.engine import make_url
from tests.helpers.postgres_target import postgres_test_target

from elspeth.web.auth.sso import ConsumedHandoff, handoff_code_hash, new_handoff_code
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.schema_probe import init_session_schema
from elspeth.web.sessions.models import identities_table, sso_handoffs_table
from elspeth.web.sessions.sso_handoff_repository import SsoHandoffRepository

pytestmark = pytest.mark.testcontainer

_IDENTITY = "identity-racer"
_CONTENDERS = 8


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as postgres_url:
        yield postgres_url


@pytest.fixture
def postgres_engine(postgres_url: str) -> Iterator[Engine]:
    identifier = f"elspeth_handoff_{uuid.uuid4().hex}"
    assert re.fullmatch(r"[a-z0-9_]+", identifier)
    admin = create_engine(postgres_url)
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{identifier}"')
    engine = create_engine(make_url(postgres_url).set(database=identifier))
    init_session_schema(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(identities_table).values(
                identity_id=_IDENTITY,
                provider="local",
                subject="subject-racer",
                username="racer",
                first_seen_at=database_now(conn),
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{identifier}" WITH (FORCE)')
        admin.dispose()


def _race(postgres_engine: Engine, *, code_hash: str, contenders: int) -> list[ConsumedHandoff | None]:
    """Release ``contenders`` independent stores onto one code simultaneously.

    Each gets its OWN engine, so no two share a connection or a transaction —
    the point being that the database, not the process, is what serialises
    them.
    """
    engines = [create_engine(postgres_engine.url) for _ in range(contenders)]
    barrier = threading.Barrier(contenders)

    def consume(engine: Engine) -> ConsumedHandoff | None:
        store = SsoHandoffRepository(engine)
        barrier.wait()
        return store.consume(code_hash=code_hash)

    try:
        with ThreadPoolExecutor(max_workers=contenders) as pool:
            return [future.result() for future in [pool.submit(consume, engine) for engine in engines]]
    finally:
        for engine in engines:
            engine.dispose()


def test_exactly_one_of_two_racing_consumers_wins(postgres_engine: Engine) -> None:
    """The minimal race, and the one a double-submitted form produces."""
    code = new_handoff_code()
    SsoHandoffRepository(postgres_engine).issue(code_hash=handoff_code_hash(code), identity_id=_IDENTITY, request_id="req-1")

    results = _race(postgres_engine, code_hash=handoff_code_hash(code), contenders=2)

    assert sorted(results, key=lambda value: value is None) == [ConsumedHandoff(_IDENTITY, "req-1"), None]


def test_exactly_one_of_many_racing_consumers_wins(postgres_engine: Engine) -> None:
    """Widened past two: a claim that merely NARROWS the window would pass the
    two-contender case often enough to look green and fail under load."""
    code = new_handoff_code()
    SsoHandoffRepository(postgres_engine).issue(code_hash=handoff_code_hash(code), identity_id=_IDENTITY, request_id="req-1")

    results = _race(postgres_engine, code_hash=handoff_code_hash(code), contenders=_CONTENDERS)

    assert results.count(ConsumedHandoff(_IDENTITY, "req-1")) == 1, results
    assert results.count(None) == _CONTENDERS - 1, results


def test_the_losers_did_not_leave_the_row_half_claimed(postgres_engine: Engine) -> None:
    """One winner is only half the property: the row must also record exactly
    one consume, not the last writer's timestamp over an earlier one."""
    code = new_handoff_code()
    store = SsoHandoffRepository(postgres_engine)
    store.issue(code_hash=handoff_code_hash(code), identity_id=_IDENTITY, request_id="req-1")

    _race(postgres_engine, code_hash=handoff_code_hash(code), contenders=_CONTENDERS)

    with postgres_engine.connect() as conn:
        rows = conn.execute(select(sso_handoffs_table.c.consumed_at).where(sso_handoffs_table.c.code_hash == handoff_code_hash(code))).all()

    assert len(rows) == 1
    assert rows[0].consumed_at is not None
    assert store.consume(code_hash=handoff_code_hash(code)) is None


def test_racing_distinct_codes_all_succeed(postgres_engine: Engine) -> None:
    """The positive control for the race harness itself.

    Without it, a ``consume`` that deadlocked or refused under any concurrency
    would make every test above pass for the wrong reason.
    """
    codes = [new_handoff_code() for _ in range(_CONTENDERS)]
    store = SsoHandoffRepository(postgres_engine)
    for index, code in enumerate(codes):
        store.issue(code_hash=handoff_code_hash(code), identity_id=_IDENTITY, request_id=f"req-{index}")

    engines = [create_engine(postgres_engine.url) for _ in codes]
    barrier = threading.Barrier(len(codes))

    def consume(engine: Engine, code: str) -> ConsumedHandoff | None:
        barrier.wait()
        return SsoHandoffRepository(engine).consume(code_hash=handoff_code_hash(code))

    try:
        with ThreadPoolExecutor(max_workers=len(codes)) as pool:
            results = [f.result() for f in [pool.submit(consume, engine, code) for engine, code in zip(engines, codes, strict=True)]]
    finally:
        for engine in engines:
            engine.dispose()

    assert results == [ConsumedHandoff(_IDENTITY, f"req-{index}") for index in range(len(codes))]
