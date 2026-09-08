"""PostgreSQL witness that enqueue serializes with worker eviction."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select, update
from tests.fixtures.landscape import leader_coordination_token, make_factory, register_test_node
from tests.helpers.postgres_target import postgres_test_target
from tests.testcontainer.core.test_scheduler_lease_eviction_postgres import (
    GRACE,
    WINDOW,
    _await_done_or_lock_wait,
    _pause_evictor_after_registry_update,
    _record_thread_pid,
)

from elspeth.contracts import NodeType
from elspeth.contracts.errors import RunMembershipLostError
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    rows_table,
    run_workers_table,
    scheduler_events_table,
    token_work_items_table,
    tokens_table,
)

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as url:
        yield url


@pytest.mark.timeout(120)
@pytest.mark.parametrize("repetition", range(8))
def test_enqueue_blocks_on_member_eviction_and_refuses_without_queue_writes(
    postgres_url: str,
    repetition: int,
    record_property: Callable[[str, object], None],
) -> None:
    """Eight fresh runs prove the member UPDATE lock, then zero queue writes."""
    db = LandscapeDB.from_url(postgres_url)
    try:
        factory = make_factory(db)
        run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
        leader = leader_coordination_token(factory, run.run_id)
        source = register_test_node(factory.data_flow, run.run_id, "source", node_type=NodeType.SOURCE)
        node = register_test_node(factory.data_flow, run.run_id, "transform")
        row, token = factory.data_flow.create_row_with_token(
            source, 0, {"id": repetition}, source_row_index=0, ingest_sequence=0, coordination_token=leader
        )
        coordination = RunCoordinationRepository(db.engine)
        member = coordination.admit_follower(
            run_id=run.run_id,
            worker_id=f"enqueue-follower-{run.run_id}",
            config_hash=run.config_hash,
            window_seconds=WINDOW,
        )
        with db.engine.begin() as conn:
            conn.execute(
                update(run_workers_table)
                .where(run_workers_table.c.run_id == run.run_id, run_workers_table.c.worker_id == member.worker_id)
                .values(heartbeat_expires_at=func.current_timestamp() - timedelta(seconds=GRACE + 10))
            )
            source_before = tuple(conn.execute(select(rows_table).where(rows_table.c.run_id == run.run_id)).all())
            tokens_before = tuple(conn.execute(select(tokens_table).where(tokens_table.c.run_id == run.run_id)).all())

        scheduler = TokenSchedulerRepository(db.engine)
        payload = scheduler.serialize_row_payload(PipelineRow({"id": repetition}, SchemaContract(mode="OBSERVED", fields=(), locked=True)))
        pids: dict[str, int] = {}
        results: dict[str, object] = {}
        paused = threading.Event()
        release = threading.Event()
        enqueuer_done = threading.Event()
        pause_hook = _pause_evictor_after_registry_update(pids, paused, release)
        pid_hook = _record_thread_pid(pids, "enqueuer", "enqueuer")

        def evict() -> None:
            try:
                results["evict"] = coordination.evict_worker(
                    token=leader, target_worker_id=member.worker_id, grace_seconds=GRACE, window_seconds=WINDOW
                )
            except BaseException as exc:
                results["evict"] = exc

        def enqueue() -> None:
            try:
                results["enqueue"] = scheduler.enqueue_ready(
                    member_token=member,
                    token_id=token.token_id,
                    row_id=row.row_id,
                    node_id=node,
                    step_index=1,
                    ingest_sequence=0,
                    row_payload_json=payload,
                )
            except BaseException as exc:
                results["enqueue"] = exc
            finally:
                enqueuer_done.set()

        evictor = threading.Thread(target=evict, name="evictor")
        enqueuer = threading.Thread(target=enqueue, name="enqueuer")
        event.listen(db.engine, "after_cursor_execute", pause_hook)
        event.listen(db.engine, "before_cursor_execute", pid_hook)
        try:
            evictor.start()
            assert paused.wait(timeout=30), "eviction never reached its registry UPDATE"
            enqueuer.start()
            state = _await_done_or_lock_wait(db, done=enqueuer_done, pid_holder=pids, pid_key="enqueuer")
            assert state == "lock_wait", f"enqueue bypassed in-flight eviction: {state}, {results.get('enqueue')!r}"
            with db.engine.connect() as observer:
                blockers, statement = observer.exec_driver_sql(
                    "SELECT pg_blocking_pids(pid), query FROM pg_stat_activity WHERE pid = %(pid)s",
                    {"pid": pids["enqueuer"]},
                ).one()
            assert pids["evictor"] in blockers, "the member eviction must be the enqueue backend's blocker"
            assert " ".join(statement.upper().split()).startswith("UPDATE RUN_WORKERS"), statement
            record_property("blocking_statement", statement)
            record_property("blocking_backend", pids["evictor"])
            record_property("blocked_backend", pids["enqueuer"])
            release.set()
        finally:
            release.set()
            if evictor.ident is not None:
                evictor.join(timeout=30)
            if enqueuer.ident is not None:
                enqueuer.join(timeout=30)
            event.remove(db.engine, "before_cursor_execute", pid_hook)
            event.remove(db.engine, "after_cursor_execute", pause_hook)
        assert not evictor.is_alive() and not enqueuer.is_alive(), "race threads wedged"
        assert results["evict"] is True, results["evict"]
        refusal = results["enqueue"]
        assert isinstance(refusal, RunMembershipLostError), refusal
        assert refusal.run_id == run.run_id
        assert refusal.worker_id == member.worker_id
        with db.read_only_connection() as conn:
            assert (
                conn.execute(
                    select(run_workers_table.c.status).where(
                        run_workers_table.c.run_id == run.run_id, run_workers_table.c.worker_id == member.worker_id
                    )
                ).scalar_one()
                == "evicted"
            )
            assert conn.execute(select(token_work_items_table).where(token_work_items_table.c.run_id == run.run_id)).all() == []
            assert conn.execute(select(scheduler_events_table).where(scheduler_events_table.c.run_id == run.run_id)).all() == []
            assert tuple(conn.execute(select(rows_table).where(rows_table.c.run_id == run.run_id)).all()) == source_before
            assert tuple(conn.execute(select(tokens_table).where(tokens_table.c.run_id == run.run_id)).all()) == tokens_before
    finally:
        db.close()
