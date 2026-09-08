"""A deposed restore cannot erase its successor's barrier adoption (ea34bb81e6).

The successor uses the real takeover, reset and adoption verbs. It pauses with
its adoption transaction open while the former leader resumes its stale reset.
An observer verifies the successor's seat lock through PostgreSQL's NOWAIT
error. The old epoch may refuse immediately: a WHERE predicate that already
rejects it need not wait on that lock. Removing the reset's leader fence instead
reaches the payload lock and then clears the successor's marker.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from psycopg.errors import LockNotAvailable
from sqlalchemy import Connection, event, func, select, update
from sqlalchemy.exc import DBAPIError
from tests.fixtures.landscape import claim_test_work_item, leader_coordination_token, make_factory, register_test_node
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts import NodeType
from elspeth.contracts.errors import RunLeadershipLostError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import run_coordination_events_table, run_coordination_table, token_work_items_table

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as url:
        yield url


@pytest.fixture
def postgres_db(postgres_url: str) -> Iterator[LandscapeDB]:
    db = LandscapeDB(postgres_url)
    try:
        yield db
    finally:
        db.close()


@pytest.mark.parametrize("repetition", range(8))
@pytest.mark.timeout(120)
def test_stale_restore_reset_preserves_successor_adoption(postgres_db: LandscapeDB, repetition: int) -> None:
    db = postgres_db
    factory = make_factory(db)
    run = factory.run_lifecycle.begin_run(config={"repetition": repetition}, canonical_version="v1")
    former = leader_coordination_token(factory, run.run_id)
    source = register_test_node(factory.data_flow, run.run_id, "source", node_type=NodeType.SOURCE)
    barrier = register_test_node(factory.data_flow, run.run_id, "coalesce", node_type=NodeType.COALESCE)
    _row, token = factory.data_flow.create_row_with_token(
        source, 0, {"id": repetition}, source_row_index=0, ingest_sequence=0, coordination_token=former
    )
    item = claim_test_work_item(factory, member_token=former.membership, token_id=token.token_id, node_id=barrier)
    factory.scheduler.mark_blocked(
        member_token=former.membership,
        work_item_id=item.work_item_id,
        queue_key=None,
        barrier_key=barrier,
        expected_lease_owner=former.worker_id,
    )
    adopted = factory.scheduler.adopt_blocked_barrier_item(
        work_item_id=item.work_item_id,
        token_id=token.token_id,
        barrier_key=barrier,
        membership=None,
        buffered_outcome=None,
        coordination_token=former,
    )
    assert adopted.adopted and adopted.barrier_adopted_epoch == former.leader_epoch

    # The former restore stalls outside a transaction after its adoption has
    # committed. Expire only the seat deadline; the production takeover creates
    # the successor's membership and evicts the former leader itself.
    with db.engine.begin() as conn:
        conn.execute(
            update(run_coordination_table)
            .where(run_coordination_table.c.run_id == run.run_id)
            .values(leader_heartbeat_expires_at=func.current_timestamp() - timedelta(seconds=1))
        )
    successor = factory.run_coordination.acquire_run_leadership(run_id=run.run_id, worker_id=f"successor-{run.run_id}", window_seconds=80)
    assert successor.leader_epoch == former.leader_epoch + 1
    assert factory.scheduler.reset_adoption_marker_to_pending(work_item_ids=[item.work_item_id], coordination_token=successor) == 1

    successor_paused = threading.Event()
    release_successor = threading.Event()
    stale_finished = threading.Event()
    pids: dict[str, int] = {}
    adopted_image: list[tuple[object, ...]] = []

    def record_backend(conn: Connection, _cursor, _statement, _parameters, _context, _executemany) -> None:
        role = threading.current_thread().name
        if role in {"adopting-successor", "stale-restorer"}:
            pids.setdefault(role, int(conn.connection.driver_connection.info.backend_pid))

    def pause_after_adoption(conn: Connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        if threading.current_thread().name != "adopting-successor":
            return
        if not statement.startswith("UPDATE token_work_items SET barrier_adopted_epoch="):
            return
        row = conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id == item.work_item_id)).one()
        assert row.barrier_adopted_epoch == successor.leader_epoch
        adopted_image.append(tuple(row))
        successor_paused.set()
        if not release_successor.wait(timeout=40):
            raise TimeoutError("observer never released the successor's adoption transaction")

    def adopt_as_successor():
        threading.current_thread().name = "adopting-successor"
        return factory.scheduler.adopt_blocked_barrier_item(
            work_item_id=item.work_item_id,
            token_id=token.token_id,
            barrier_key=barrier,
            membership=None,
            buffered_outcome=None,
            coordination_token=successor,
        )

    def reset_as_former():
        threading.current_thread().name = "stale-restorer"
        try:
            return factory.scheduler.reset_adoption_marker_to_pending(work_item_ids=[item.work_item_id], coordination_token=former)
        except RunLeadershipLostError as exc:
            return exc
        finally:
            stale_finished.set()

    event.listen(db.engine, "before_cursor_execute", record_backend)
    event.listen(db.engine, "after_cursor_execute", pause_after_adoption)
    observed_blockers: list[int] = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            adopting = pool.submit(adopt_as_successor)
            resetting = None
            try:
                assert successor_paused.wait(timeout=30), "successor never reached its actual adoption UPDATE"
                # A server-generated 55P03 on THIS run's seat proves the hook is
                # still inside a real fenced transaction, not after its commit.
                with db.engine.connect() as observer, pytest.raises(DBAPIError) as locked:
                    observer.execute(
                        select(run_coordination_table.c.run_id)
                        .where(run_coordination_table.c.run_id == run.run_id)
                        .with_for_update(nowait=True)
                    )
                assert isinstance(locked.value.orig, LockNotAvailable)
                assert locked.value.orig.sqlstate == "55P03"

                resetting = pool.submit(reset_as_former)
                deadline = time.monotonic() + 20
                with db.engine.connect() as observer:
                    while not stale_finished.is_set() and time.monotonic() < deadline:
                        stale_pid = pids.get("stale-restorer")
                        if stale_pid is not None:
                            observed_blockers = observer.exec_driver_sql(
                                "SELECT pg_blocking_pids(%(pid)s)", {"pid": stale_pid}
                            ).scalar_one()
                            if observed_blockers:
                                break
                        stale_finished.wait(timeout=0.01)
            finally:
                release_successor.set()
                # Future.result propagates either actor's unexpected exception,
                # including the original DBAPIError type and server SQLSTATE.
                successor_result = adopting.result(timeout=30)
                stale_result = None if resetting is None else resetting.result(timeout=30)
    finally:
        event.remove(db.engine, "before_cursor_execute", record_backend)
        event.remove(db.engine, "after_cursor_execute", pause_after_adoption)

    assert pids["adopting-successor"] != pids["stale-restorer"]
    assert successor_result.adopted and successor_result.barrier_adopted_epoch == successor.leader_epoch
    with db.read_only_connection() as conn:
        final_row = conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id == item.work_item_id)).one()
        refusals = conn.execute(
            select(run_coordination_events_table).where(
                run_coordination_events_table.c.run_id == run.run_id,
                run_coordination_events_table.c.event_type == "fence_refusal",
            )
        ).all()

    assert isinstance(stale_result, RunLeadershipLostError), (
        f"stale reset returned {stale_result!r}; PostgreSQL blockers={observed_blockers!r}; "
        f"successor pid={pids['adopting-successor']}; marker after reset={final_row.barrier_adopted_epoch!r}"
    )
    assert observed_blockers == [], "a rejected old epoch reached a payload lock instead of refusing at the leader fence"
    assert len(adopted_image) == 1
    assert tuple(final_row) == adopted_image[0], "the stale restore changed its successor's durable adoption row"
    assert len(refusals) == 1
    assert refusals[0].worker_id == former.worker_id
    assert refusals[0].leader_epoch == former.leader_epoch
    assert json.loads(refusals[0].context_json)["verb"] == "reset_adoption_marker_to_pending"
