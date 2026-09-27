"""PostgreSQL proof that a barrier release runs in bounded statements (X1 fix round 2).

No statement in ``complete_barrier`` binds a list that grows with the batch:
the terminalize and passthrough hand-off UPDATEs each run as one executemany
of a fixed-size statement, and the tokens lock read and the duplicate-outcome
read run in budget-sized chunks. The SQLite unit tests in
``tests/unit/core/landscape/test_scheduler_repository_complete_barrier.py``
prove the bound against a lowered SQLITE_LIMIT_VARIABLE_NUMBER. This file runs
the same statements on PostgreSQL, where the lock read is a real
``SELECT ... FOR UPDATE`` taken chunk by chunk in ascending token order and
each executemany's rowcount is psycopg's sum over its rows, which the
completion's exact-count checks rely on.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select
from tests.fixtures.landscape import claim_test_work_item, leader_coordination_token, make_factory, register_test_node
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts import NodeType
from elspeth.contracts.enums import TerminalOutcome, TerminalPath
from elspeth.contracts.scheduler import BarrierEmission, BarrierTerminalOutcomeSpec, SchedulerEventType, TokenWorkStatus
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import scheduler_events_table, token_outcomes_table, token_work_items_table

pytestmark = pytest.mark.testcontainer

# Budget 30: 30 tokens per lock/duplicate read, so 40 consumed tokens span
# two chunks of each read.
_BUDGET = 30
_TOKENS = 40


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


def _block_tokens(factory: RecorderFactory, *, run_id: str, barrier: str, source: str, offset: int) -> list[tuple[str, str, str]]:
    """Create ``_TOKENS`` rows and BLOCK each token at ``barrier``; return (token_id, work_item_id, payload)."""
    coordination = leader_coordination_token(factory, run_id)
    blocked = []
    for index in range(offset, offset + _TOKENS):
        _row, token = factory.data_flow.create_row_with_token(
            source, index, {"id": index}, source_row_index=index, ingest_sequence=index, coordination_token=coordination
        )
        item = claim_test_work_item(factory, member_token=coordination.membership, token_id=token.token_id, node_id=barrier)
        factory.scheduler.mark_blocked(
            member_token=coordination.membership,
            work_item_id=item.work_item_id,
            row_payload_json=item.row_payload_json,
            queue_key=None,
            barrier_key=barrier,
            expected_lease_owner=coordination.worker_id,
        )
        blocked.append((token.token_id, item.work_item_id, item.row_payload_json))
    return blocked


@pytest.mark.timeout(180)
def test_chunked_barrier_release_on_postgres(postgres_db: LandscapeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.core.landscape.scheduler import barrier as barrier_module

    monkeypatch.setattr(barrier_module, "_BIND_BUDGET_PER_STATEMENT", _BUDGET)
    factory = make_factory(postgres_db)
    run = factory.run_lifecycle.begin_run(config={"case": "bind-budget"}, canonical_version="v1")
    coordination = leader_coordination_token(factory, run.run_id)
    source = register_test_node(factory.data_flow, run.run_id, "source", node_type=NodeType.SOURCE)
    passthrough_barrier = register_test_node(factory.data_flow, run.run_id, "rank", node_type=NodeType.AGGREGATION)
    consumed_barrier = register_test_node(factory.data_flow, run.run_id, "stats", node_type=NodeType.AGGREGATION)
    handed_off = _block_tokens(factory, run_id=run.run_id, barrier=passthrough_barrier, source=source, offset=0)
    consumed = _block_tokens(factory, run_id=run.run_id, barrier=consumed_barrier, source=source, offset=_TOKENS)

    emissions = [
        BarrierEmission(
            token_id=token_id,
            row_payload_json=payload,
            sink_name=f"failed-{index}",
            outcome=TerminalOutcome.FAILURE.value,
            path=TerminalPath.ON_ERROR_ROUTED.value,
            error_hash=f"{index:016x}",
            error_message=f"error {index}",
        )
        if index % 2
        else BarrierEmission(
            token_id=token_id,
            row_payload_json=payload,
            sink_name=f"out-{index}",
            outcome=TerminalOutcome.SUCCESS.value,
            path=TerminalPath.DEFAULT_FLOW.value,
        )
        for index, (token_id, _work_item_id, payload) in enumerate(handed_off)
    ]
    factory.scheduler.complete_barrier(
        barrier_key=passthrough_barrier,
        consumed_token_ids=(),
        emitted_pending_sink=emissions,
        emitted_ready=(),
        coordination_token=coordination,
    )
    consumed_ids = [token_id for token_id, _work_item_id, _payload in consumed]
    terminalized = factory.scheduler.complete_barrier(
        barrier_key=consumed_barrier,
        consumed_token_ids=consumed_ids,
        emitted_pending_sink=(),
        emitted_ready=(),
        intake_snapshot_token_ids=frozenset(consumed_ids),
        coordination_token=coordination,
        terminal_outcomes=tuple(
            BarrierTerminalOutcomeSpec(token_id=token_id, outcome=TerminalOutcome.SUCCESS, path=TerminalPath.FILTER_DROPPED)
            for token_id in consumed_ids
        ),
    )

    assert terminalized == _TOKENS
    with postgres_db.engine.connect() as conn:
        rows = {
            row["token_id"]: row
            for row in conn.execute(select(token_work_items_table).where(token_work_items_table.c.run_id == run.run_id)).mappings()
        }
        recorded = sorted(
            conn.execute(
                select(token_outcomes_table.c.token_id)
                .where(token_outcomes_table.c.run_id == run.run_id)
                .where(token_outcomes_table.c.completed == 1)
            ).scalars()
        )
        handoff_events = (
            conn.execute(
                select(scheduler_events_table.c.token_id)
                .where(scheduler_events_table.c.run_id == run.run_id)
                .where(scheduler_events_table.c.event_type == SchedulerEventType.MARK_PENDING_SINK.value)
                .order_by(scheduler_events_table.c.seq)
            )
            .scalars()
            .all()
        )
    for emission, (token_id, work_item_id, _payload) in zip(emissions, handed_off, strict=True):
        row = rows[token_id]
        assert row["work_item_id"] == work_item_id
        assert row["status"] == TokenWorkStatus.PENDING_SINK.value
        assert (
            row["pending_sink_name"],
            row["pending_outcome"],
            row["pending_path"],
            row["pending_error_hash"],
            row["pending_error_message"],
        ) == (
            emission.sink_name,
            emission.outcome,
            emission.path,
            emission.error_hash,
            emission.error_message,
        )
    assert handoff_events == [token_id for token_id, _work_item_id, _payload in handed_off]
    assert {rows[token_id]["status"] for token_id in consumed_ids} == {TokenWorkStatus.TERMINAL.value}
    assert recorded == sorted(consumed_ids)
