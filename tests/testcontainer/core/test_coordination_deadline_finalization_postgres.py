"""Fresh coordination issuance after server-observed waits and owned bodies."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update
from tests.fixtures.landscape import expire_leader_seat, leader_coordination_token, make_factory
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts import RunStatus
from elspeth.contracts.coordination import CoordinationSnapshot, CoordinationToken
from elspeth.contracts.errors import JoinRefusedError
from elspeth.core.checkpoint.recovery import NonResumableRunError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
from elspeth.core.landscape.schema import run_coordination_events_table, run_coordination_table, run_workers_table, runs_table

pytestmark = [pytest.mark.testcontainer, pytest.mark.timeout(30)]

WINDOW_SECONDS = 1.2
HOLD_SECONDS = 1.6


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as url:
        yield url


@pytest.fixture
def db(postgres_url: str) -> Iterator[LandscapeDB]:
    database = LandscapeDB(postgres_url)
    try:
        yield database
    finally:
        database.close()


def _wait_for_blocker(db: LandscapeDB, *, waiter: int, blocker: int) -> None:
    until = time.monotonic() + 5
    while time.monotonic() < until:
        with db.engine.connect() as observer:
            blocked = observer.exec_driver_sql("SELECT %s = ANY(pg_blocking_pids(%s))", (blocker, waiter)).scalar_one()
        if blocked:
            return
        time.sleep(0.01)
    raise AssertionError(f"PostgreSQL did not report backend {waiter} blocked by {blocker}")


def _behind_seat_lock(
    db: LandscapeDB,
    token: CoordinationToken,
    operation: Callable[[], object],
    *,
    expected_refusal: type[Exception] | None = None,
) -> object:
    """Hold the production entry lock longer than the requested lease window."""
    waiter_pid: list[int] = []
    outcome: list[object] = []
    entered = threading.Event()

    def capture_pid(conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _many: bool) -> None:
        if threading.current_thread().name != "coordination-deadline-waiter" or waiter_pid:
            return
        if statement.startswith("SELECT pg_backend_pid"):
            return
        waiter_pid.append(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
        entered.set()

    def invoke() -> None:
        try:
            outcome.append(operation())
        except BaseException as exc:
            outcome.append(exc)

    waiter = threading.Thread(name="coordination-deadline-waiter", target=invoke)
    event.listen(db.engine, "before_cursor_execute", capture_pid)
    try:
        with db.engine.begin() as holder:
            holder.execute(select(run_coordination_table).where(run_coordination_table.c.run_id == token.run_id).with_for_update())
            holder_pid = holder.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            waiter.start()
            assert entered.wait(5)
            _wait_for_blocker(db, waiter=waiter_pid[0], blocker=holder_pid)
            holder.exec_driver_sql("SELECT pg_sleep(%s)", (HOLD_SECONDS,))
        waiter.join(10)
        assert not waiter.is_alive(), "coordination writer did not finish after lock release"
        assert len(outcome) == 1
        if expected_refusal is None:
            assert not isinstance(outcome[0], BaseException), repr(outcome[0])
        else:
            assert isinstance(outcome[0], expected_refusal), repr(outcome[0])
        return outcome[0]
    finally:
        waiter.join(10)
        event.remove(db.engine, "before_cursor_execute", capture_pid)


def _assert_fresh_pair(db: LandscapeDB, token: CoordinationToken) -> None:
    """Observe post-return residual, allowing half the nominal window for the tail."""
    with db.engine.connect() as observer:
        seat_expiry, member_expiry = observer.execute(
            select(
                run_coordination_table.c.leader_heartbeat_expires_at,
                run_workers_table.c.heartbeat_expires_at,
            )
            .join(run_workers_table, run_workers_table.c.worker_id == run_coordination_table.c.leader_worker_id)
            .where(run_coordination_table.c.run_id == token.run_id)
        ).one()
        observed = observer.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
    assert isinstance(observed, datetime)
    assert seat_expiry == member_expiry
    residual = (seat_expiry - observed).total_seconds()
    assert residual > WINDOW_SECONDS / 2, f"post-return residual {residual:.6f}s consumed by lock wait/body"


@pytest.mark.parametrize("operation", ["takeover", "export", "heartbeat"])
def test_coordination_deadline_pair_is_fresh_after_seat_lock_wait(db: LandscapeDB, operation: str) -> None:
    factory = make_factory(db)
    run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    leader = leader_coordination_token(factory, run.run_id)
    repo = factory.run_coordination
    if operation == "export":
        factory.run_lifecycle.complete_run(status=RunStatus.FAILED, coordination_token=leader)
        repo.release_seat(token=leader)
        result = _behind_seat_lock(
            db,
            leader,
            lambda: repo.acquire_export_leadership(run_id=run.run_id, worker_id=f"export:{uuid4().hex}", window_seconds=WINDOW_SECONDS),
        )
        assert isinstance(result, CoordinationToken)
        leader = result
    elif operation == "takeover":
        expire_leader_seat(db, run.run_id)
        result = _behind_seat_lock(
            db,
            leader,
            lambda: repo.acquire_run_leadership(run_id=run.run_id, worker_id=f"takeover:{uuid4().hex}", window_seconds=WINDOW_SECONDS),
        )
        assert isinstance(result, CoordinationToken)
        leader = result
    else:
        result = _behind_seat_lock(db, leader, lambda: repo.worker_heartbeat(member_token=leader.membership, window_seconds=WINDOW_SECONDS))
        assert isinstance(result, CoordinationSnapshot)
        assert result.seat_live and result.worker_active
    _assert_fresh_pair(db, leader)


def test_long_leader_body_renews_before_releasing_the_takeover_lock(db: LandscapeDB) -> None:
    factory = make_factory(db)
    run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    leader = leader_coordination_token(factory, run.run_id)
    repo = factory.run_coordination
    entered = threading.Event()
    waiter_pid: list[int] = []
    outcome: list[object] = []

    def capture_pid(conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _many: bool) -> None:
        if threading.current_thread().name != "long-body-takeover" or waiter_pid or statement.startswith("SELECT pg_backend_pid"):
            return
        waiter_pid.append(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
        entered.set()

    def attempt_takeover() -> None:
        try:
            outcome.append(repo.acquire_run_leadership(run_id=run.run_id, worker_id=f"rival:{uuid4().hex}", window_seconds=WINDOW_SECONDS))
        except BaseException as exc:
            outcome.append(exc)

    waiter = threading.Thread(name="long-body-takeover", target=attempt_takeover)
    event.listen(db.engine, "before_cursor_execute", capture_pid)
    try:
        with fenced_leader_transaction(db.engine, token=leader, window_seconds=WINDOW_SECONDS, verb="long_body_probe") as conn:
            holder_pid = conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            waiter.start()
            assert entered.wait(5)
            _wait_for_blocker(db, waiter=waiter_pid[0], blocker=holder_pid)
            conn.exec_driver_sql("SELECT pg_sleep(%s)", (HOLD_SECONDS,))
            conn.execute(update(runs_table).where(runs_table.c.run_id == run.run_id).values(source_schema_json='{"probe":true}'))
        waiter.join(10)
        assert not waiter.is_alive()
        assert len(outcome) == 1 and isinstance(outcome[0], NonResumableRunError), repr(outcome)
        with db.engine.connect() as observer:
            expiry = observer.execute(
                select(run_coordination_table.c.leader_heartbeat_expires_at).where(run_coordination_table.c.run_id == run.run_id)
            ).scalar_one()
            observed = observer.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
            payload = observer.execute(select(runs_table.c.source_schema_json).where(runs_table.c.run_id == run.run_id)).scalar_one()
        assert (expiry - observed).total_seconds() > WINDOW_SECONDS / 2
        assert payload == '{"probe":true}'
    finally:
        waiter.join(10)
        event.remove(db.engine, "before_cursor_execute", capture_pid)


def test_begin_run_finalizes_pair_after_mint_body_delay(db: LandscapeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.core.landscape import run_lifecycle_repository
    from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository

    original = RunCoordinationRepository.register_run_leader_on
    initial_registration: list[tuple[datetime, datetime]] = []

    def delayed_mint(self: RunCoordinationRepository, conn: Any, **kwargs: Any) -> CoordinationToken:
        token = original(self, conn, **kwargs)
        registered_at, expires = conn.execute(
            select(run_workers_table.c.registered_at, run_workers_table.c.heartbeat_expires_at).where(
                run_workers_table.c.worker_id == token.worker_id
            )
        ).one()
        initial_registration.append((registered_at, expires))
        conn.exec_driver_sql("SELECT pg_sleep(%s)", (HOLD_SECONDS,))
        return token

    monkeypatch.setattr(run_lifecycle_repository, "DEFAULT_RUN_LIVENESS_WINDOW_SECONDS", WINDOW_SECONDS)
    monkeypatch.setattr(RunCoordinationRepository, "register_run_leader_on", delayed_mint)
    factory = make_factory(db)
    run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    leader = leader_coordination_token(factory, run.run_id)
    _assert_fresh_pair(db, leader)
    with db.engine.connect() as observer:
        registered_at, final_expiry = observer.execute(
            select(run_workers_table.c.registered_at, run_workers_table.c.heartbeat_expires_at).where(
                run_workers_table.c.worker_id == leader.worker_id
            )
        ).one()
        admission_stamps = (
            observer.execute(
                select(run_coordination_events_table.c.recorded_at).where(
                    run_coordination_events_table.c.worker_id == leader.worker_id,
                    run_coordination_events_table.c.event_type.in_(("worker_register", "leader_acquire")),
                )
            )
            .scalars()
            .all()
        )
    assert len(initial_registration) == 1
    assert registered_at == initial_registration[0][0]
    assert admission_stamps == [registered_at, registered_at]
    assert (final_expiry - initial_registration[0][1]).total_seconds() > HOLD_SECONDS / 2


def test_follower_admission_refuses_seat_that_expires_while_waiting(db: LandscapeDB) -> None:
    factory = make_factory(db)
    run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    leader = leader_coordination_token(factory, run.run_id)
    joining_worker = f"joiner:{uuid4().hex}"
    with db.engine.begin() as conn:
        deadline = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one() + timedelta(seconds=0.8)
        conn.execute(
            update(run_coordination_table).where(run_coordination_table.c.run_id == run.run_id).values(leader_heartbeat_expires_at=deadline)
        )
        conn.execute(
            update(run_workers_table).where(run_workers_table.c.worker_id == leader.worker_id).values(heartbeat_expires_at=deadline)
        )
    _behind_seat_lock(
        db,
        leader,
        lambda: factory.run_coordination.admit_follower(
            run_id=run.run_id,
            worker_id=joining_worker,
            config_hash=run.config_hash,
            window_seconds=WINDOW_SECONDS,
        ),
        expected_refusal=JoinRefusedError,
    )
    with db.engine.connect() as observer:
        assert (
            observer.execute(select(run_workers_table.c.worker_id).where(run_workers_table.c.worker_id == joining_worker)).first() is None
        )


def test_follower_admission_rechecks_seat_after_its_own_body(db: LandscapeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository

    factory = make_factory(db)
    run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    joining_worker = f"delayed-joiner:{uuid4().hex}"
    with db.engine.begin() as conn:
        deadline = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one() + timedelta(seconds=0.8)
        conn.execute(
            update(run_coordination_table).where(run_coordination_table.c.run_id == run.run_id).values(leader_heartbeat_expires_at=deadline)
        )
    original = RunCoordinationRepository._insert_worker_row

    def delayed_member(conn: Any, **kwargs: Any) -> None:
        original(conn, **kwargs)
        conn.exec_driver_sql("SELECT pg_sleep(%s)", (HOLD_SECONDS,))

    monkeypatch.setattr(RunCoordinationRepository, "_insert_worker_row", staticmethod(delayed_member))
    with pytest.raises(JoinRefusedError, match="expired during follower admission"):
        factory.run_coordination.admit_follower(
            run_id=run.run_id, worker_id=joining_worker, config_hash=run.config_hash, window_seconds=WINDOW_SECONDS
        )
    with db.engine.connect() as observer:
        assert (
            observer.execute(select(run_workers_table.c.worker_id).where(run_workers_table.c.worker_id == joining_worker)).first() is None
        )
