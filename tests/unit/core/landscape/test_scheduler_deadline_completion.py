"""An outer scheduler composition must publish its exact, still-usable lease."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Connection, ExecutionContext

from elspeth.contracts.scheduler import SourceIngestSpec, TokenWorkItem
from elspeth.core.landscape.lease_deadlines import LeaseDeadlineExpiredError
from elspeth.core.landscape.scheduler.work_items import item_from_mapping
from elspeth.core.landscape.schema import node_states_table, rows_table, scheduler_events_table, token_work_items_table, tokens_table
from tests.fixtures.landscape import RecorderSetup, leader_coordination_token, make_recorder_with_run, register_test_node


@pytest.fixture
def setup() -> Iterator[RecorderSetup]:
    prepared = make_recorder_with_run()
    register_test_node(prepared.factory.data_flow, prepared.run_id, "normalize")
    try:
        yield prepared
    finally:
        prepared.db.close()


def _source(setup: RecorderSetup) -> SourceIngestSpec:
    return SourceIngestSpec(
        source_node_id=setup.source_node_id,
        row_index=0,
        source_row_index=0,
        ingest_sequence=0,
        row_id="outer-row",
        token_id="outer-token",
        data={"id": 1},
    )


def _ingest(setup: RecorderSetup) -> TokenWorkItem:
    authority = leader_coordination_token(setup.factory, setup.run_id)
    _, _, item = setup.factory.scheduler.ingest_row_with_initial_claim(
        coordination_token=authority,
        source=_source(setup),
        data_flow=setup.factory.data_flow,
        execution=setup.factory.execution,
        node_id="normalize",
        step_index=1,
        row_payload_json='{"id":1}',
        lease_owner=authority.worker_id,
        lease_seconds=1,
    )
    return item


def test_ingest_refuses_expired_outer_tail_and_rolls_back_all_payload(setup: RecorderSetup) -> None:
    leader_coordination_token(setup.factory, setup.run_id)
    delayed: list[bool] = []

    def delay_after_lease(
        conn: Connection, cursor: object, statement: str, parameters: object, context: ExecutionContext, executemany: bool
    ) -> None:
        if statement.upper().startswith("UPDATE TOKEN_WORK_ITEMS"):
            delayed.append(True)
            time.sleep(1.1)

    event.listen(setup.db.engine, "after_cursor_execute", delay_after_lease)
    try:
        with pytest.raises(LeaseDeadlineExpiredError):
            _ingest(setup)
    finally:
        event.remove(setup.db.engine, "after_cursor_execute", delay_after_lease)
    assert delayed == [True]
    with setup.db.engine.connect() as conn:
        for table in (rows_table, tokens_table, node_states_table, token_work_items_table, scheduler_events_table):
            assert conn.execute(select(table)).all() == []
    # The rejected transaction must not leave a pooled-connection obligation.
    committed = _ingest(setup)
    assert committed.token_id == "outer-token"


def test_ingest_snapshot_and_event_match_exact_persisted_deadline(setup: RecorderSetup) -> None:
    item = _ingest(setup)
    with setup.db.engine.connect() as conn:
        row = conn.execute(select(token_work_items_table)).mappings().one()
        event_expiry = conn.execute(
            select(scheduler_events_table.c.to_lease_expires_at).where(
                scheduler_events_table.c.event_type == "claim_ready",
            )
        ).scalar_one()
    assert item.lease_expires_at == item_from_mapping(row).lease_expires_at
    assert item.lease_expires_at is not None
    assert item.lease_expires_at.replace(tzinfo=None) == event_expiry.replace(tzinfo=None)


def test_connection_enqueue_helper_cannot_publish_after_outer_delay(setup: RecorderSetup) -> None:
    authority = leader_coordination_token(setup.factory, setup.run_id)
    source = _source(setup)
    snapshots: list[TokenWorkItem] = []
    with pytest.raises(LeaseDeadlineExpiredError), setup.db.engine.begin() as conn:
        setup.factory.data_flow.insert_row_with_token_on(
            conn,
            coordination_token=authority,
            source_node_id=source.source_node_id,
            row_index=source.row_index,
            data=source.data,
            source_row_index=source.source_row_index,
            ingest_sequence=source.ingest_sequence,
            row_id=source.row_id,
            token_id=source.token_id,
        )
        row = setup.factory.scheduler.queue.enqueue_ready_claimed_on(
            conn,
            run_id=setup.run_id,
            token_id=source.token_id,
            row_id=source.row_id,
            node_id="normalize",
            step_index=1,
            ingest_sequence=0,
            row_payload_json='{"id":1}',
            lease_owner=authority.worker_id,
            lease_seconds=1,
        )
        snapshots.append(item_from_mapping(row))
        time.sleep(1.1)
    assert len(snapshots) == 1
    with setup.db.engine.connect() as conn:
        assert conn.execute(select(token_work_items_table)).all() == []
        assert conn.execute(select(scheduler_events_table)).all() == []
