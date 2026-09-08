"""Fresh effect decisions after PostgreSQL-observed target-row contention."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event
from time import monotonic, sleep

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Connection
from tests.fixtures.landscape import expire_sink_effect_lease, leader_coordination_token, make_factory
from tests.helpers.postgres_target import postgres_test_target
from tests.unit.core.landscape.test_sink_effect_finalization import _prepared, _request
from tests.unit.core.landscape.test_sink_effect_lifecycle import _claim, _plan, _reserved

from elspeth.contracts.sink_effects import SinkEffectLease
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.schema import artifacts_table, sink_effects_table

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def db() -> Iterator[LandscapeDB]:
    with postgres_test_target(driver="psycopg") as url:
        database = LandscapeDB(url)
        try:
            yield database
        finally:
            database.close()


def _after_target_wait[T](
    db: LandscapeDB, effect_id: str, operation: Callable[[], T], *, expires_during_wait: datetime | None = None
) -> tuple[T, datetime]:
    """Run an operation whose effect lock demonstrably waits for a distinct backend."""
    attempted = Event()
    waiter: list[int] = []
    transaction_started: list[datetime] = []

    def observe(conn: Connection, _cursor: object, statement: str, _parameters: object, _context: object, _executemany: bool) -> None:
        if "FROM sink_effects" in statement and "FOR UPDATE" in statement and not attempted.is_set():
            waiter.append(int(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()))
            transaction_started.append(conn.exec_driver_sql("SELECT CURRENT_TIMESTAMP").scalar_one())
            attempted.set()

    with db.engine.connect() as holder, db.engine.connect() as observer:
        transaction = holder.begin()
        holder.execute(select(sink_effects_table).where(sink_effects_table.c.effect_id == effect_id).with_for_update()).one()
        blocker = int(holder.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
        event.listen(db.engine, "before_cursor_execute", observe)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(operation)
                try:
                    assert attempted.wait(5), "effect operation never attempted its target lock"
                    until = monotonic() + 5
                    while not observer.exec_driver_sql("SELECT %s = ANY(pg_blocking_pids(%s))", (blocker, waiter[0])).scalar_one():
                        assert monotonic() < until, "operation never blocked on the effect holder"
                        sleep(0.01)
                    assert waiter[0] != blocker
                    if expires_during_wait is not None:
                        assert transaction_started[0] < expires_during_wait, "lease must start live in the waiting transaction"
                    holder.exec_driver_sql("SELECT pg_sleep(0.5)")
                    released_after = holder.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
                finally:
                    transaction.rollback()
                return future.result(timeout=10), released_after
        finally:
            if transaction.is_active:
                transaction.rollback()
            event.remove(db.engine, "before_cursor_execute", observe)


@pytest.mark.parametrize("action", ("preparation", "acquire", "reserved_heartbeat", "in_flight_heartbeat", "takeover"))
def test_issued_deadline_starts_after_target_lock(db: LandscapeDB, action: str) -> None:
    factory = make_factory(db)
    effect = _reserved(factory)
    repo = factory.execution.sink_effects
    token = leader_coordination_token(factory, effect.run_id)
    claim = _claim(factory, effect.effect_id, run_id=effect.run_id)
    if action in {"acquire", "in_flight_heartbeat", "takeover"}:
        repo.complete_plan(effect.effect_id, _plan(effect.effect_id), claim=claim, coordination_token=token)
    if action in {"in_flight_heartbeat", "takeover"}:
        claim = repo.acquire_lease(effect.effect_id, owner="worker-a", ttl=timedelta(seconds=30), coordination_token=token)
    if action == "takeover":
        expire_sink_effect_lease(db.engine, effect.effect_id)

    def issue() -> SinkEffectLease:
        if action == "preparation":
            return repo.claim_preparation(effect.effect_id, owner="worker-a", ttl=timedelta(seconds=5), coordination_token=token)
        if action == "acquire":
            return repo.acquire_lease(effect.effect_id, owner="worker-a", ttl=timedelta(seconds=5), coordination_token=token)
        if action == "takeover":
            return repo.takeover_expired(effect.effect_id, owner="worker-b", ttl=timedelta(seconds=5), coordination_token=token)
        return repo.heartbeat_lease(
            effect.effect_id, owner=claim.owner, generation=claim.generation, ttl=timedelta(seconds=5), coordination_token=token
        )

    lease, released_after = _after_target_wait(db, effect.effect_id, issue)
    with db.read_only_connection() as conn:
        persisted = conn.execute(select(sink_effects_table).where(sink_effects_table.c.effect_id == effect.effect_id)).one()
    assert persisted.lease_heartbeat_at >= released_after
    assert persisted.lease_expires_at == lease.expires_at
    assert persisted.lease_expires_at - persisted.lease_heartbeat_at == timedelta(seconds=5)
    assert persisted.updated_at == persisted.lease_heartbeat_at
    assert persisted.generation == lease.generation
    assert persisted.lease_owner == lease.owner


@pytest.mark.parametrize("action", ("reserved_heartbeat", "in_flight_heartbeat", "same_owner_acquire", "takeover", "finalize"))
def test_expiry_crossed_during_lock_wait_controls_the_decision(db: LandscapeDB, action: str) -> None:
    factory = make_factory(db)
    repo = factory.execution.sink_effects
    if action == "reserved_heartbeat":
        effect = _reserved(factory)
        lease = _claim(factory, effect.effect_id, run_id=effect.run_id)
        request = None
    else:
        effect, members, lease = _prepared(factory)
        request = _request(factory, effect, members, lease) if action == "finalize" else None
    token = leader_coordination_token(factory, effect.run_id)
    with db.engine.begin() as conn:
        expiry = conn.exec_driver_sql("SELECT clock_timestamp() + interval '0.3 seconds'").scalar_one()
        conn.execute(sink_effects_table.update().where(sink_effects_table.c.effect_id == effect.effect_id).values(lease_expires_at=expiry))

    def decide() -> SinkEffectLease | LandscapeRecordError:
        try:
            if action == "finalize":
                assert request is not None
                repo.finalize(request, coordination_token=token)
                pytest.fail("expired effect finalized")
            if action == "same_owner_acquire":
                return repo.acquire_lease(effect.effect_id, owner=lease.owner, ttl=timedelta(seconds=5), coordination_token=token)
            if action == "takeover":
                return repo.takeover_expired(effect.effect_id, owner="worker-b", ttl=timedelta(seconds=5), coordination_token=token)
            return repo.heartbeat_lease(
                effect.effect_id, owner=lease.owner, generation=lease.generation, ttl=timedelta(seconds=5), coordination_token=token
            )
        except LandscapeRecordError as exc:
            return exc

    result, released_after = _after_target_wait(db, effect.effect_id, decide, expires_during_wait=expiry)
    assert released_after > expiry
    with db.read_only_connection() as conn:
        persisted = conn.execute(select(sink_effects_table).where(sink_effects_table.c.effect_id == effect.effect_id)).one()
        artifact = conn.execute(select(artifacts_table).where(artifacts_table.c.sink_effect_id == effect.effect_id)).first()
    if action in {"reserved_heartbeat", "takeover"}:
        assert isinstance(result, SinkEffectLease)
        assert result.expires_at == persisted.lease_expires_at
        assert persisted.lease_heartbeat_at >= released_after
        assert result.generation == lease.generation + (1 if action == "takeover" else 0)
    else:
        assert isinstance(result, LandscapeRecordError)
        assert persisted.lease_expires_at == expiry
        assert persisted.generation == lease.generation
        assert persisted.lease_owner == lease.owner
        assert artifact is None
