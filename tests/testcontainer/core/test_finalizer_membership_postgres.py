"""Follower admission and finalization serialize their shared worker roster."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine import Connection
from tests.fixtures.landscape import leader_coordination_token
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts import RunStatus
from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS
from elspeth.contracts.errors import JoinRefusedError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import run_coordination_events_table, run_workers_table

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as url:
        yield url


def _backend_pid(conn: Connection) -> int:
    return int(conn.connection.driver_connection.info.backend_pid)


@pytest.mark.parametrize("first_operation", ("finalize", "admit"))
@pytest.mark.timeout(60)
def test_follower_admission_cannot_escape_finalizer_roster(postgres_url: str, first_operation: str) -> None:
    finalizer_db = LandscapeDB.from_url(postgres_url)
    admission_db = LandscapeDB.from_url(postgres_url)
    finalizer = RecorderFactory(finalizer_db)
    admission = RecorderFactory(admission_db)
    run = finalizer.run_lifecycle.begin_run(config={}, canonical_version="v1", leader_worker_id=f"leader:{first_operation}")
    authority = leader_coordination_token(finalizer, run.run_id)
    follower_id = f"follower:{first_operation}"
    holder_ready = threading.Event()
    release_holder = threading.Event()
    waiter_started = threading.Event()
    backend_pids: dict[str, int] = {}

    def hold_finalizer_roster(conn: Connection, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        normalized = " ".join(statement.upper().split())
        if normalized.startswith("SELECT RUN_WORKERS.WORKER_ID") and "FOR UPDATE" in normalized:
            backend_pids["holder"] = _backend_pid(conn)
            holder_ready.set()
            assert release_holder.wait(timeout=10), "finalizer roster holder was not released"

    def hold_admission_commit(conn: Connection) -> None:
        backend_pids["holder"] = _backend_pid(conn)
        holder_ready.set()
        assert release_holder.wait(timeout=10), "admission holder was not released"

    def record_waiter(conn: Connection) -> None:
        if threading.current_thread() is threading.main_thread():
            return
        backend_pids["waiter"] = _backend_pid(conn)
        waiter_started.set()

    def finalize() -> None:
        finalizer.run_lifecycle.complete_run(RunStatus.FAILED, coordination_token=authority)

    def admit() -> None:
        admission.run_coordination.admit_follower(
            run_id=run.run_id,
            worker_id=follower_id,
            config_hash=run.config_hash,
            window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
        )

    if first_operation == "finalize":
        holder_engine, holder_event, holder_callback = finalizer_db.engine, "after_cursor_execute", hold_finalizer_roster
        waiter_engine = admission_db.engine
        first, second = finalize, admit
    else:
        holder_engine, holder_event, holder_callback = admission_db.engine, "commit", hold_admission_commit
        waiter_engine = finalizer_db.engine
        first, second = admit, finalize
    event.listen(holder_engine, holder_event, holder_callback)
    event.listen(waiter_engine, "begin", record_waiter)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future: Future[None] = pool.submit(first)
            try:
                assert holder_ready.wait(timeout=10), "first operation never reached its held transaction"
                second_future = pool.submit(second)
                assert waiter_started.wait(timeout=10), "second operation never opened its transaction"
                assert backend_pids["holder"] != backend_pids["waiter"]
                deadline = time.monotonic() + 10
                with finalizer_db.engine.connect() as observer:
                    while True:
                        blockers = observer.execute(select(func.pg_blocking_pids(backend_pids["waiter"]))).scalar_one()
                        if backend_pids["holder"] in blockers:
                            break
                        if second_future.done():
                            second_future.result()
                            pytest.fail("second operation bypassed the roster's seat lock")
                        assert time.monotonic() < deadline, "PostgreSQL did not report the expected seat-lock waiter"
                        time.sleep(0.01)
                release_holder.set()
                first_future.result(timeout=10)
                if first_operation == "finalize":
                    with pytest.raises(JoinRefusedError, match="run status is 'failed'"):
                        second_future.result(timeout=10)
                else:
                    second_future.result(timeout=10)
            finally:
                release_holder.set()

        with finalizer_db.engine.connect() as conn:
            worker_status = conn.execute(
                select(run_workers_table.c.status).where(run_workers_table.c.worker_id == follower_id)
            ).scalar_one_or_none()
            follower_events = (
                conn.execute(
                    select(run_coordination_events_table.c.event_type)
                    .where(run_coordination_events_table.c.run_id == run.run_id)
                    .where(run_coordination_events_table.c.worker_id == follower_id)
                    .order_by(run_coordination_events_table.c.seq)
                )
                .scalars()
                .all()
            )
        if first_operation == "finalize":
            assert worker_status is None
            assert follower_events == []
        else:
            assert worker_status == "departed"
            assert follower_events == ["worker_register", "worker_depart"]
        completed = finalizer.run_lifecycle.get_run(run.run_id)
        assert completed is not None and completed.status is RunStatus.FAILED
    finally:
        release_holder.set()
        event.remove(holder_engine, holder_event, holder_callback)
        event.remove(waiter_engine, "begin", record_waiter)
        admission_db.close()
        finalizer_db.close()
