"""A recovered item generation refuses its old audit writer under PostgreSQL contention."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from psycopg import Error as PsycopgError
from sqlalchemy import event, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from tests.fixtures.landscape import expire_lease, leader_coordination_token, make_factory, register_test_node
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts import NodeType
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.errors import SchedulerLeaseLostError
from elspeth.contracts.scheduler import SchedulerEventType, TokenWorkStatus
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import (
    node_states_table,
    rows_table,
    run_workers_table,
    scheduler_events_table,
    token_outcomes_table,
    token_work_items_table,
    tokens_table,
    transform_errors_table,
)

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    # The target seam supplies this module its own database/container.
    with postgres_test_target(driver="psycopg") as url:
        yield url


def _audit_snapshot(db: LandscapeDB, run_id: str) -> dict[str, tuple[tuple[object, ...], ...]]:
    tables = (rows_table, tokens_table, transform_errors_table, node_states_table, token_outcomes_table)
    with db.read_only_connection() as conn:
        return {table.name: tuple(tuple(row) for row in conn.execute(select(table).where(table.c.run_id == run_id))) for table in tables}


@pytest.mark.parametrize("repetition", range(8))
@pytest.mark.timeout(60)
def test_recovery_generation_blocks_then_refuses_stale_item_audit(postgres_url: str, repetition: int) -> None:
    """The old member stays active, so refusal must come from its item CAS.

    Recovery holds owner and item locks while its generation rotation is
    uncommitted. The old writer now blocks at its earlier MEMBER verify-UPDATE;
    this test does not claim a direct item-lock wait. Observe PostgreSQL's
    exact blocker and the member UPDATE's worker identity before releasing
    recovery, then require the still-active writer's item-generation refusal.
    A run-only item verify-UPDATE mutation still blocks earlier, but after recovery it
    violates PostgreSQL's leased-owner CHECK instead of refusing the stale
    generation; a DBAPI error from either actor always fails this test.
    """
    recovery_db = LandscapeDB.from_url(postgres_url)
    writer_db = LandscapeDB.from_url(postgres_url)
    recovery = make_factory(recovery_db)
    writer = make_factory(writer_db)
    holder_ready = threading.Event()
    release_holder = threading.Event()
    writer_at_item = threading.Event()
    writer_at_member = threading.Event()
    pids: dict[str, int] = {}

    def hold_rotated_item(conn: Connection, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        if " ".join(statement.upper().split()).startswith("UPDATE TOKEN_WORK_ITEMS"):
            pids["recovery"] = int(conn.connection.driver_connection.info.backend_pid)
            holder_ready.set()
            assert release_holder.wait(timeout=15), "recovery's held item was not released"

    def observe_item_attempt(conn: Connection, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        normalized = " ".join(statement.upper().split())
        if normalized.startswith("UPDATE RUN_WORKERS"):
            assert _parameters["worker_id_1"] == member.worker_id
            assert _parameters["run_id_1"] == run.run_id
            pids["writer"] = int(conn.connection.driver_connection.info.backend_pid)
            writer_at_member.set()
        if normalized.startswith("UPDATE TOKEN_WORK_ITEMS"):
            writer_at_item.set()

    try:
        run = recovery.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id=f"item-generation-race-{repetition}")
        leader = leader_coordination_token(recovery, run.run_id)
        source_id = f"source-{repetition}"
        transform_id = f"transform-{repetition}"
        register_test_node(recovery.data_flow, run.run_id, source_id, node_type=NodeType.SOURCE)
        register_test_node(recovery.data_flow, run.run_id, transform_id)
        row, token = recovery.data_flow.create_row_with_token(
            source_id, 0, {"value": 1}, source_row_index=0, ingest_sequence=0, coordination_token=leader
        )
        member = recovery.run_coordination.admit_follower(
            run_id=run.run_id, worker_id=f"follower-item-{repetition}", config_hash=run.config_hash, window_seconds=3600
        )
        item = writer.scheduler.enqueue_ready_claimed(
            member_token=member,
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=transform_id,
            step_index=1,
            ingest_sequence=0,
            row_payload_json=writer.scheduler.serialize_row_payload(
                PipelineRow({"value": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
            ),
            lease_owner=member.worker_id,
            lease_seconds=300,
        )

        def record_error() -> str:
            return writer.data_flow.record_transform_error(
                TokenRef(token.token_id, run.run_id),
                transform_id,
                {"value": 1},
                {"reason": "invalid_input"},
                "discard",
                member_token=member,
                work_item=item,
            )

        # A live claim can write through this same production caller. The
        # later refusal cannot pass merely because the payload is invalid.
        live_error_id = record_error()
        before = _audit_snapshot(recovery_db, run.run_id)
        assert len(before["transform_errors"]) == 1
        expire_lease(recovery_db.engine, item.work_item_id, seconds_ago=70)
        event.listen(recovery_db.engine, "after_cursor_execute", hold_rotated_item)
        event.listen(writer_db.engine, "before_cursor_execute", observe_item_attempt)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                recovering = pool.submit(recovery.scheduler.recover_expired_leases, coordination_token=leader, stall_budget_seconds=60)
                try:
                    assert holder_ready.wait(timeout=10), "recovery never rotated the leased item"
                    writing = pool.submit(record_error)
                    assert writer_at_member.wait(timeout=10), "old writer never reached its member verify-UPDATE"
                    assert pids["writer"] != pids["recovery"]
                    deadline = time.monotonic() + 10
                    with recovery_db.engine.connect() as observer:
                        while True:
                            blockers = observer.execute(select(func.pg_blocking_pids(pids["writer"]))).scalar_one()
                            if pids["recovery"] in blockers:
                                break
                            if writing.done():
                                writing.result()
                                pytest.fail("old writer passed membership admission before recovery released its owner lock")
                            assert time.monotonic() < deadline, "PostgreSQL did not report the exact recovery/member-writer blocker"
                            time.sleep(0.01)
                    assert not recovering.done()
                    assert not writer_at_item.is_set(), "writer crossed the member lock before recovery committed"
                    release_holder.set()
                    assert recovering.result(timeout=10) == 1
                    try:
                        with pytest.raises(SchedulerLeaseLostError) as refused:
                            writing.result(timeout=10)
                    except DBAPIError as exc:
                        assert isinstance(exc.orig, PsycopgError)
                        pytest.fail(
                            f"repetition {repetition}: stale item writer raised PostgreSQL sqlstate={exc.orig.sqlstate}: {exc.orig}"
                        )
                    assert refused.value.work_item_id == item.work_item_id
                    assert refused.value.lease_owner == member.worker_id
                    assert writer_at_item.is_set(), "stale generation was not checked after membership admission"
                finally:
                    release_holder.set()
        finally:
            event.remove(writer_db.engine, "before_cursor_execute", observe_item_attempt)
            event.remove(recovery_db.engine, "after_cursor_execute", hold_rotated_item)

        assert _audit_snapshot(recovery_db, run.run_id) == before
        with recovery_db.read_only_connection() as conn:
            current = conn.execute(select(token_work_items_table).where(token_work_items_table.c.token_id == token.token_id)).one()
            status = conn.execute(select(run_workers_table.c.status).where(run_workers_table.c.worker_id == member.worker_id)).scalar_one()
            error_ids = (
                conn.execute(select(transform_errors_table.c.error_id).where(transform_errors_table.c.run_id == run.run_id)).scalars().all()
            )
            recoveries = conn.execute(
                select(scheduler_events_table.c.from_attempt, scheduler_events_table.c.to_attempt)
                .where(scheduler_events_table.c.run_id == run.run_id)
                .where(scheduler_events_table.c.event_type == SchedulerEventType.RECOVER_EXPIRED_LEASE.value)
            ).all()
        assert status == "active", "membership loss must not mask the stale item generation"
        assert current.work_item_id != item.work_item_id
        assert current.attempt == item.attempt + 1
        assert current.status == TokenWorkStatus.READY.value
        assert current.lease_owner is None
        assert error_ids == [live_error_id]
        assert recoveries == [(item.attempt, item.attempt + 1)]
    finally:
        release_holder.set()
        writer_db.close()
        recovery_db.close()
