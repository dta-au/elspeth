"""Server-observed heartbeat/takeover contention and bounded shutdown proofs."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from tests.fixtures.landscape import expire_leader_seat
from tests.helpers.postgres_target import postgres_test_target
from tests.helpers.run_coordination import register_run_leader
from tests.testcontainer.core.test_run_coordination_release_postgres import _seed_run

from elspeth.contracts.coordination import CoordinationSnapshot, CoordinationToken, WorkerMembershipLost, mint_worker_id
from elspeth.core.checkpoint.recovery import NonResumableRunError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository, _bound_heartbeat_statement_waits
from elspeth.core.landscape.schema import run_coordination_events_table, run_coordination_table, run_workers_table

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as url:
        yield url


def _wait_for_blocker(db: LandscapeDB, *, waiter: int, blocker: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with db.engine.connect() as conn:
            blocked = conn.exec_driver_sql("SELECT %s = ANY(pg_blocking_pids(%s))", (blocker, waiter)).scalar_one()
        if blocked:
            return
        time.sleep(0.01)
    raise AssertionError(f"PostgreSQL did not report pid {waiter} waiting for {blocker}")


@pytest.mark.parametrize("first_waiter", ["heartbeat", "takeover"])
@pytest.mark.timeout(60)
def test_heartbeat_and_takeover_serialize_without_either_deadlock_victim(postgres_url: str, first_waiter: str) -> None:
    """Hold the first acquired locks, then witness the exact backend blocker.

    On the old member-first implementation the second waiter closes a cycle;
    the first waiter's deadlock timer selects it as victim. With seat-first
    locking takeover waits, loses to the renewed seat and never evicts it.
    """
    db = LandscapeDB.from_url(postgres_url)
    repo = RunCoordinationRepository(db.engine)
    run_id = f"heartbeat-lock-order-{first_waiter}"
    _seed_run(db, run_id=run_id, now=datetime.now(UTC))
    token = register_run_leader(repo, run_id=run_id, worker_id=mint_worker_id(run_id), window_seconds=80)
    expire_leader_seat(db, run_id)
    gates = {name: threading.Event() for name in ("heartbeat", "takeover")}
    release = {name: threading.Event() for name in gates}
    first_lock: list[str] = []
    pids: dict[str, int] = {}
    outcomes: dict[str, object] = {}

    def before_sql(conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool) -> None:
        name = threading.current_thread().name
        normalized = " ".join(statement.upper().split())
        if name in gates and name not in pids and not normalized.startswith("SELECT PG_BACKEND_PID"):
            pids[name] = conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            conn.exec_driver_sql("SET LOCAL deadlock_timeout = '100ms'")
            conn.exec_driver_sql("SET LOCAL statement_timeout = '12000ms'")

    def after_sql(_conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool) -> None:
        name = threading.current_thread().name
        normalized = " ".join(statement.upper().split())
        if name not in gates or gates[name].is_set():
            return
        seat = normalized.startswith("UPDATE RUN_COORDINATION") or (
            normalized.startswith("SELECT") and "FROM RUN_COORDINATION" in normalized and "FOR UPDATE" in normalized
        )
        member = normalized.startswith("UPDATE RUN_WORKERS")
        if name == "heartbeat" and (seat or member):
            first_lock.append("seat" if seat else "member")
        elif name != "takeover" or not seat:
            return
        gates[name].set()
        assert release[name].wait(15), f"{name} interleaving gate timed out"

    def invoke(name: str, operation: Callable[[], object]) -> None:
        try:
            outcomes[name] = operation()
        except BaseException as exc:
            outcomes[name] = exc

    beat = threading.Thread(
        name="heartbeat", target=invoke, args=("heartbeat", lambda: repo.worker_heartbeat(member_token=token.membership, window_seconds=80))
    )
    takeover = threading.Thread(
        name="takeover",
        target=invoke,
        args=("takeover", lambda: repo.acquire_run_leadership(run_id=run_id, worker_id=mint_worker_id(run_id), window_seconds=80)),
    )
    event.listen(db.engine, "before_cursor_execute", before_sql)
    event.listen(db.engine, "after_cursor_execute", after_sql)
    try:
        beat.start()
        assert gates["heartbeat"].wait(10)
        takeover.start()
        if first_lock == ["member"]:
            assert gates["takeover"].wait(10)
            other = "takeover" if first_waiter == "heartbeat" else "heartbeat"
            release[first_waiter].set()
            _wait_for_blocker(db, waiter=pids[first_waiter], blocker=pids[other])
            release[other].set()
        else:
            deadline = time.monotonic() + 10
            while "takeover" not in pids and time.monotonic() < deadline:
                time.sleep(0.01)
            _wait_for_blocker(db, waiter=pids["takeover"], blocker=pids["heartbeat"])
            release["heartbeat"].set()
            release["takeover"].set()
        beat.join(20)
        takeover.join(20)
        assert not beat.is_alive() and not takeover.is_alive()
        print({name: repr(value) for name, value in outcomes.items()})
        assert isinstance(outcomes["heartbeat"], CoordinationSnapshot), outcomes
        assert isinstance(outcomes["takeover"], NonResumableRunError), outcomes
        with db.engine.connect() as conn:
            seat = conn.execute(
                select(run_coordination_table.c.leader_worker_id).where(run_coordination_table.c.run_id == run_id)
            ).scalar_one()
            status = conn.execute(select(run_workers_table.c.status).where(run_workers_table.c.worker_id == token.worker_id)).scalar_one()
        assert seat == token.worker_id
        assert status == "active"
    finally:
        for gate in release.values():
            gate.set()
        for thread in (beat, takeover):
            if thread.ident is not None:
                thread.join(20)
        event.remove(db.engine, "before_cursor_execute", before_sql)
        event.remove(db.engine, "after_cursor_execute", after_sql)
        db.close()


@pytest.mark.timeout(45)
@pytest.mark.parametrize("driver", ["psycopg", "psycopg2"])
def test_postgresql_blocked_heartbeat_returns_and_stop_completes(postgres_url: str, driver: str) -> None:
    """A real held seat lock cannot trap the heartbeat or its owner in join()."""
    from elspeth.engine.orchestrator.heartbeat import RunHeartbeatThread

    driver_url = make_url(postgres_url).set(drivername=f"postgresql+{driver}").render_as_string(hide_password=False)
    db = LandscapeDB.from_url(driver_url)
    repo = RunCoordinationRepository(db.engine)
    run_id = f"heartbeat-bounded-stop-{driver}"
    _seed_run(db, run_id=run_id, now=datetime.now(UTC))
    token = register_run_leader(repo, run_id=run_id, worker_id=mint_worker_id(run_id), window_seconds=80)
    beat_pid: list[int] = []

    def capture_pid(conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool) -> None:
        if (
            threading.current_thread().name.startswith("run-heartbeat:")
            and not beat_pid
            and not statement.startswith("SELECT pg_backend_pid")
        ):
            beat_pid.append(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())

    event.listen(db.engine, "before_cursor_execute", capture_pid)
    heartbeat = RunHeartbeatThread(repo, member_token=token.membership, heartbeat_seconds=0.01, degraded_threshold=1)
    stopped = threading.Event()
    errors: list[BaseException] = []

    def stop() -> None:
        try:
            heartbeat.stop(final_beat=False)
        except BaseException as exc:
            errors.append(exc)
        finally:
            stopped.set()

    stopper = threading.Thread(target=stop)
    try:
        with db.engine.begin() as holder:
            holder.execute(select(run_coordination_table).where(run_coordination_table.c.run_id == run_id).with_for_update())
            blocker = holder.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            heartbeat.start()
            deadline = time.monotonic() + 5
            while not beat_pid and time.monotonic() < deadline:
                time.sleep(0.01)
            assert beat_pid, "heartbeat never entered a database statement"
            _wait_for_blocker(db, waiter=beat_pid[0], blocker=blocker)
            stopper.start()
            assert stopped.wait(8), "heartbeat stop remained blocked behind a PostgreSQL row lock"
            assert errors == []
            heartbeat.check_and_raise()
            assert heartbeat._consecutive_busy == 1
            with db.engine.connect() as observer:
                recorded = (
                    observer.execute(
                        select(run_coordination_events_table.c.event_type).where(
                            run_coordination_events_table.c.run_id == run_id,
                            run_coordination_events_table.c.event_type == "heartbeat_degraded",
                        )
                    )
                    .scalars()
                    .all()
                )
            assert recorded == ["heartbeat_degraded"]
    finally:
        if stopper.ident is not None:
            stopper.join(10)
        heartbeat.stop(final_beat=False)
        event.remove(db.engine, "before_cursor_execute", capture_pid)
        db.close()


@pytest.mark.timeout(45)
def test_takeover_wins_before_old_heartbeat_and_refuses_the_evicted_member(postgres_url: str) -> None:
    db = LandscapeDB.from_url(postgres_url)
    repo = RunCoordinationRepository(db.engine)
    run_id = "takeover-before-heartbeat"
    _seed_run(db, run_id=run_id, now=datetime.now(UTC))
    token = register_run_leader(repo, run_id=run_id, worker_id=mint_worker_id(run_id), window_seconds=80)
    expire_leader_seat(db, run_id)
    locked = threading.Event()
    release = threading.Event()
    pids: dict[str, int] = {}
    outcomes: dict[str, object] = {}

    def before_sql(conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool) -> None:
        name = threading.current_thread().name
        if name in ("takeover-first", "heartbeat-second") and name not in pids and not statement.startswith("SELECT pg_backend_pid"):
            pids[name] = conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()

    def after_sql(_conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool) -> None:
        if threading.current_thread().name == "takeover-first" and statement.startswith("UPDATE run_coordination"):
            locked.set()
            assert release.wait(10)

    def invoke(name: str, operation: Callable[[], object]) -> None:
        try:
            outcomes[name] = operation()
        except BaseException as exc:
            outcomes[name] = exc

    takeover = threading.Thread(
        name="takeover-first",
        target=invoke,
        args=("takeover", lambda: repo.acquire_run_leadership(run_id=run_id, worker_id=mint_worker_id(run_id), window_seconds=80)),
    )
    beat = threading.Thread(
        name="heartbeat-second",
        target=invoke,
        args=("heartbeat", lambda: repo.worker_heartbeat(member_token=token.membership, window_seconds=80)),
    )
    event.listen(db.engine, "before_cursor_execute", before_sql)
    event.listen(db.engine, "after_cursor_execute", after_sql)
    try:
        takeover.start()
        assert locked.wait(10)
        beat.start()
        deadline = time.monotonic() + 5
        while "heartbeat-second" not in pids and time.monotonic() < deadline:
            time.sleep(0.01)
        _wait_for_blocker(db, waiter=pids["heartbeat-second"], blocker=pids["takeover-first"])
        release.set()
        takeover.join(10)
        beat.join(10)
        assert not takeover.is_alive() and not beat.is_alive()
        assert isinstance(outcomes["takeover"], CoordinationToken), outcomes
        assert outcomes["heartbeat"] == WorkerMembershipLost(member_token=token.membership)
        with db.engine.connect() as conn:
            seat = conn.execute(
                select(run_coordination_table.c.leader_worker_id).where(run_coordination_table.c.run_id == run_id)
            ).scalar_one()
            status = conn.execute(select(run_workers_table.c.status).where(run_workers_table.c.worker_id == token.worker_id)).scalar_one()
        assert seat == outcomes["takeover"].worker_id
        assert status == "evicted"
    finally:
        release.set()
        for thread in (takeover, beat):
            if thread.ident is not None:
                thread.join(15)
        event.remove(db.engine, "before_cursor_execute", before_sql)
        event.remove(db.engine, "after_cursor_execute", after_sql)
        db.close()


@pytest.mark.timeout(20)
def test_heartbeat_statement_timeout_without_lock_contention_remains_fatal(postgres_url: str) -> None:
    """The earlier lock budget must not turn arbitrary statement delays into BUSY."""
    from psycopg.errors import QueryCanceled

    from elspeth.engine.orchestrator.heartbeat import _is_lock_contention

    with LandscapeDB.from_url(postgres_url) as db:
        with pytest.raises(OperationalError) as raised, db.engine.begin() as conn:
            _bound_heartbeat_statement_waits(conn)
            assert (
                conn.exec_driver_sql(
                    "SELECT current_setting('lock_timeout')::interval < current_setting('statement_timeout')::interval"
                ).scalar_one()
                is True
            )
            conn.exec_driver_sql("SELECT pg_sleep(10)")
        assert isinstance(raised.value.orig, QueryCanceled)
        assert not _is_lock_contention(raised.value)
