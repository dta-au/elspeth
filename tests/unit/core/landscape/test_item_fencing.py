"""Item audit authority rejects stale claims before committing payload writes."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from elspeth.contracts.coordination import WorkerMembershipToken
from elspeth.contracts.errors import AuditIntegrityError, RunMembershipLostError, SchedulerLeaseLostError
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.core.landscape.database import Tier1Engine
from elspeth.core.landscape.item_fencing import fenced_item_transaction
from elspeth.core.landscape.schema import nodes_table, run_workers_table, token_work_items_table
from tests.fixtures.landscape import make_recorder_with_run


@pytest.fixture
def claimed_item() -> Iterator[tuple[Tier1Engine, WorkerMembershipToken, TokenWorkItem]]:
    setup = make_recorder_with_run(leader_worker_id="item-writer")
    row, token = setup.factory.data_flow.create_row_with_token(
        setup.run_id,
        setup.source_node_id,
        0,
        {"value": 1},
        coordination_token=setup.coordination_token,
        source_row_index=0,
        ingest_sequence=0,
    )
    item = setup.factory.scheduler.enqueue_ready_claimed(
        run_id=setup.run_id,
        token_id=token.token_id,
        row_id=row.row_id,
        node_id=setup.source_node_id,
        step_index=0,
        ingest_sequence=0,
        row_payload_json="{}",
        lease_owner="item-writer",
        lease_seconds=60,
    )
    try:
        yield setup.db.engine, setup.coordination_token.membership, item
    finally:
        setup.db.close()


def _write(engine: Tier1Engine, member: WorkerMembershipToken, item: TokenWorkItem) -> None:
    with fenced_item_transaction(engine, member_token=member, work_item=item, verb="test-item-audit") as conn:
        conn.execute(
            update(nodes_table)
            .where(nodes_table.c.run_id == member.run_id, nodes_table.c.node_id == item.node_id)
            .values(plugin_version="2.0")
        )


def _version(engine: Tier1Engine, item: TokenWorkItem) -> str:
    with engine.connect() as conn:
        return str(conn.execute(select(nodes_table.c.plugin_version).where(nodes_table.c.run_id == item.run_id)).scalar_one())


def test_current_claim_commits_payload(claimed_item: tuple[Tier1Engine, WorkerMembershipToken, TokenWorkItem]) -> None:
    engine, member, item = claimed_item
    _write(engine, member, item)
    assert _version(engine, item) == "2.0"


@pytest.mark.parametrize("changed", ["attempt", "owner", "status"])
def test_changed_claim_refuses_payload(claimed_item: tuple[Tier1Engine, WorkerMembershipToken, TokenWorkItem], changed: str) -> None:
    engine, member, item = claimed_item
    with engine.begin() as conn:
        stmt = update(token_work_items_table).where(token_work_items_table.c.work_item_id == item.work_item_id)
        if changed == "attempt":
            stmt = stmt.values(attempt=item.attempt + 1)
        elif changed == "owner":
            stmt = stmt.values(lease_owner="successor")
        else:
            stmt = stmt.values(status="ready", lease_owner=None, lease_expires_at=None)
        conn.execute(stmt)
    with pytest.raises(SchedulerLeaseLostError):
        _write(engine, member, item)
    assert _version(engine, item) == "1.0"


def test_evicted_member_refuses_payload(claimed_item: tuple[Tier1Engine, WorkerMembershipToken, TokenWorkItem]) -> None:
    engine, member, item = claimed_item
    with engine.begin() as conn:
        conn.execute(
            update(run_workers_table)
            .where(run_workers_table.c.worker_id == member.worker_id)
            .values(status="evicted", evicted_at=datetime.now(UTC))
        )
    with pytest.raises(RunMembershipLostError):
        _write(engine, member, item)
    assert _version(engine, item) == "1.0"


def test_foreign_claim_is_a_contract_error(claimed_item: tuple[Tier1Engine, WorkerMembershipToken, TokenWorkItem]) -> None:
    engine, member, item = claimed_item
    with pytest.raises(AuditIntegrityError, match="admitted worker and run"):
        _write(engine, member, replace(item, run_id="foreign"))
    assert _version(engine, item) == "1.0"
