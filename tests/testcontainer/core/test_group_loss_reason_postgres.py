"""PostgreSQL regression for the group-loss reason column (elspeth-74b795208f).

Battery round 7 (cold install, Aurora PostgreSQL 16.13) killed a run inside
the branch-loss forensics INSERT: the quarantine arm inlined the full
reason-dict repr (``quarantined:{'reason': 'type_mismatch', ...}``, 150
chars) into the reason column (String(64), a category token). SQLite does
not enforce VARCHAR lengths, so the whole local suite was green while real
PostgreSQL raised StringDataRightTruncation — an audit write destroying the
run it existed to explain. Migrated from
``test_coalesce_branch_loss_reason_postgres.py`` onto the unified
``group_losses`` ledger (spec §6.2).

Three proofs, in dependency order:

1. The REAL producer (``_handle_transform_error_status`` quarantine arm)
   now emits the bare ``quarantined`` token for the exact battery payload,
   and that token records durably against real PostgreSQL.
2. ``record_group_loss`` refuses an over-wide reason with
   :class:`AuditIntegrityError` BEFORE the INSERT — dialect-uniform
   fail-closed, so SQLite tests can now catch this defect class locally.
3. A raw INSERT of a >64-char reason really is rejected by PostgreSQL —
   pinning that the column contract the guard mirrors is genuinely
   enforced by the dialect (the fact the whole defect hinged on).

Proof (1) depends on the settle-member seam (WS3 Task 5, out of THIS
session's scope) rewriting the processor staging sites onto full
``GroupLossSpec`` production; it stays skipped here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import DataError
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]
from tests.unit.engine.test_processor import _make_factory, _make_processor, _persist_token_for_scheduler

from elspeth.contracts import NodeType, TokenInfo, TransformResult
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.scheduler import GroupLossSpec
from elspeth.contracts.types import BranchName, CoalesceName, NodeID
from elspeth.core.landscape.database import LandscapeDB, begin_write
from elspeth.core.landscape.scheduler.group_losses import record_group_loss
from elspeth.core.landscape.schema import group_losses_table, nodes_table, rows_table, tokens_table
from elspeth.testing import make_row

pytestmark = pytest.mark.testcontainer

RUN_ID = "run-group-loss-reason"
NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)

# The observed battery-round-7 quarantine payload (dict repr is 138 chars;
# the pre-fix reason value was 150).
BATTERY_REASON = {
    "reason": "type_mismatch",
    "field": "price",
    "expected": "int",
    "actual": "float",
    "message": "float 29.99 has fractional part",
}


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as postgres:
        yield postgres.get_connection_url()


@pytest.fixture
def pg_db(postgres_url: str) -> Iterator[LandscapeDB]:
    db = LandscapeDB.from_url(postgres_url)
    try:
        yield db
    finally:
        db.close()


def _seed_run(db: LandscapeDB, run_id: str, tmp_path: Path) -> None:
    from elspeth.core.landscape.factory import RecorderFactory
    from elspeth.core.payload_store import FilesystemPayloadStore

    factory = RecorderFactory(db, payload_store=FilesystemPayloadStore(tmp_path / "payloads"))
    factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id=run_id)


def _seed_row_and_token(db: LandscapeDB, *, run_id: str, row_id: str, token_id: str) -> None:
    """Raw-SQL row+token seed: group_losses.token_id now carries an FK to
    tokens (unlike the retired coalesce_branch_losses), so a raw INSERT proof
    needs a genuine token row or the FK violation masks the intended DataError."""
    with db.engine.begin() as conn:
        conn.execute(
            insert(nodes_table).values(
                run_id=run_id,
                node_id="source-1",
                plugin_name="test-source",
                node_type=NodeType.SOURCE.value,
                plugin_version="1.0",
                determinism="deterministic",
                config_hash="cfg",
                config_json="{}",
                registered_at=NOW,
            )
        )
        conn.execute(
            insert(rows_table).values(
                row_id=row_id,
                run_id=run_id,
                source_node_id="source-1",
                row_index=0,
                source_row_index=0,
                ingest_sequence=0,
                source_data_hash=f"hash-{row_id}",
                created_at=NOW,
            )
        )
        conn.execute(insert(tokens_table).values(token_id=token_id, row_id=row_id, run_id=run_id, created_at=NOW))


def _produce_quarantine_group_loss_spec() -> GroupLossSpec:
    """Drive the REAL quarantine arm and return the GroupLossSpec it stages.

    The producer path is dialect-independent (it stages the spec in memory);
    what this module adds over the SQLite characterization test is proving
    the produced reason survives the PostgreSQL INSERT.
    """
    _db, factory = _make_factory()
    coalesce_name = CoalesceName("merge")
    processor = _make_processor(
        factory,
        coalesce_node_ids={coalesce_name: NodeID("coalesce::merge")},
        branch_to_coalesce={BranchName("path_a"): coalesce_name},
    )
    token = TokenInfo(
        row_id="row-1",
        token_id="tok-branch-a",
        row_data=make_row({"price": 29.99}),
        branch_name="path_a",
    )
    _persist_token_for_scheduler(factory, token)
    processor._handle_transform_error_status(
        transform_result=TransformResult.error(reason=BATTERY_REASON),
        current_token=token,
        error_sink="discard",
        child_items=[],
    )
    return processor._pending_group_losses.pop()


@pytest.mark.skip(reason="settle-member lands in WS3 Task 5")
@pytest.mark.timeout(120)
def test_battery_quarantine_payload_records_on_postgres(pg_db: LandscapeDB, tmp_path: Path) -> None:
    """The g03-s3 battery scenario: quarantine detail >64 chars, loss recorded."""
    _seed_run(pg_db, RUN_ID, tmp_path)
    spec = _produce_quarantine_group_loss_spec()
    assert spec.reason == "quarantined"

    with begin_write(pg_db.engine) as conn:
        assert record_group_loss(
            conn,
            run_id=RUN_ID,
            spec=spec,
            recorded_by="worker:test",
            now=NOW,
        )

    with pg_db.engine.connect() as conn:
        rows = conn.execute(select(group_losses_table).where(group_losses_table.c.run_id == RUN_ID)).mappings().all()
    assert [row["reason"] for row in rows] == ["quarantined"]


@pytest.mark.timeout(120)
def test_overlong_reason_fails_closed_before_postgres_sees_it(pg_db: LandscapeDB, tmp_path: Path) -> None:
    """The guard refuses pre-INSERT with the same bound PostgreSQL enforces."""
    run_id = f"{RUN_ID}-guard"
    _seed_run(pg_db, run_id, tmp_path)
    prefix_pre_fix_shape = "quarantined:" + str(BATTERY_REASON)

    with pytest.raises(AuditIntegrityError, match="category token"), begin_write(pg_db.engine) as conn:
        record_group_loss(
            conn,
            run_id=run_id,
            spec=GroupLossSpec(
                closer_name="merge",
                group_id="fg_001",
                member_key="path_b",
                token_id="tok-branch-b",
                reason=prefix_pre_fix_shape,
            ),
            recorded_by="worker:test",
            now=NOW,
        )

    with pg_db.engine.connect() as conn:
        count = conn.execute(select(group_losses_table).where(group_losses_table.c.run_id == run_id)).all()
    assert count == []


@pytest.mark.timeout(120)
def test_postgres_genuinely_rejects_overlong_reason_at_the_column(pg_db: LandscapeDB, tmp_path: Path) -> None:
    """Raw-INSERT proof that String(64) is enforced by the dialect.

    This pins the premise the guard mirrors: if the column were ever
    widened (the fix the issue explicitly rules out) or the dialect
    stopped enforcing, this test flags the contract change.
    """
    run_id = f"{RUN_ID}-raw"
    _seed_run(pg_db, run_id, tmp_path)
    _seed_row_and_token(pg_db, run_id=run_id, row_id="row-1", token_id="tok-branch-b")

    with pytest.raises(DataError), pg_db.engine.begin() as conn:
        conn.execute(
            insert(group_losses_table).values(
                loss_id="loss_raw_overlong",
                run_id=run_id,
                closer_name="merge",
                group_id="fg_001",
                member_key="path_b",
                token_id="tok-branch-b",
                reason="quarantined:" + str(BATTERY_REASON),
                recorded_by="worker:test",
                recorded_at=NOW,
                adopted_epoch=None,
            )
        )
