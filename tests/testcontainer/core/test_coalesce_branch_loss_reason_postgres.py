"""PostgreSQL regression for the coalesce branch-loss reason column (elspeth-74b795208f).

Battery round 7 (cold install, Aurora PostgreSQL 16.13) killed a run inside
the branch-loss forensics INSERT: the quarantine arm inlined the full
reason-dict repr (``quarantined:{'reason': 'type_mismatch', ...}``, 150
chars) into ``coalesce_branch_losses.reason`` (String(64), a category
token). SQLite does not enforce VARCHAR lengths, so the whole local suite
was green while real PostgreSQL raised StringDataRightTruncation — an
audit write destroying the run it existed to explain.

Three proofs, in dependency order:

1. The REAL producer (``_handle_transform_error_status`` quarantine arm)
   now emits the bare ``quarantined`` token for the exact battery payload,
   and that token records durably against real PostgreSQL.
2. ``record_coalesce_branch_loss`` refuses an over-wide reason with
   :class:`AuditIntegrityError` BEFORE the INSERT — dialect-uniform
   fail-closed, so SQLite tests can now catch this defect class locally.
3. A raw INSERT of a >64-char reason really is rejected by PostgreSQL —
   pinning that the column contract the guard mirrors is genuinely
   enforced by the dialect (the fact the whole defect hinged on).
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

from elspeth.contracts import TokenInfo, TransformResult
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.types import BranchName, CoalesceName, NodeID
from elspeth.core.landscape.database import LandscapeDB, begin_write
from elspeth.core.landscape.scheduler.branch_losses import record_coalesce_branch_loss
from elspeth.core.landscape.schema import coalesce_branch_losses_table
from elspeth.testing import make_row

pytestmark = pytest.mark.testcontainer

RUN_ID = "run-branch-loss-reason"
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


def _produce_quarantine_branch_loss_spec():  # type: ignore[no-untyped-def]
    """Drive the REAL quarantine arm and return the BranchLossSpec it stages.

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
    return processor._pending_branch_losses.pop()


@pytest.mark.timeout(120)
def test_battery_quarantine_payload_records_on_postgres(pg_db: LandscapeDB, tmp_path: Path) -> None:
    """The g03-s3 battery scenario: quarantine detail >64 chars, loss recorded."""
    _seed_run(pg_db, RUN_ID, tmp_path)
    spec = _produce_quarantine_branch_loss_spec()
    assert spec.reason == "quarantined"

    with begin_write(pg_db.engine) as conn:
        assert record_coalesce_branch_loss(
            conn,
            run_id=RUN_ID,
            coalesce_name=spec.coalesce_name,
            row_id=spec.row_id,
            branch_name=spec.branch_name,
            token_id=spec.token_id,
            reason=spec.reason,
            recorded_by=spec.recorded_by,
            now=NOW,
        )

    with pg_db.engine.connect() as conn:
        rows = conn.execute(select(coalesce_branch_losses_table).where(coalesce_branch_losses_table.c.run_id == RUN_ID)).mappings().all()
    assert [row["reason"] for row in rows] == ["quarantined"]


@pytest.mark.timeout(120)
def test_overlong_reason_fails_closed_before_postgres_sees_it(pg_db: LandscapeDB, tmp_path: Path) -> None:
    """The guard refuses pre-INSERT with the same bound PostgreSQL enforces."""
    run_id = f"{RUN_ID}-guard"
    _seed_run(pg_db, run_id, tmp_path)
    prefix_pre_fix_shape = "quarantined:" + str(BATTERY_REASON)

    with pytest.raises(AuditIntegrityError, match="category token"), begin_write(pg_db.engine) as conn:
        record_coalesce_branch_loss(
            conn,
            run_id=run_id,
            coalesce_name="merge",
            row_id="row-1",
            branch_name="path_b",
            token_id="tok-branch-b",
            reason=prefix_pre_fix_shape,
            recorded_by="worker:test",
            now=NOW,
        )

    with pg_db.engine.connect() as conn:
        count = conn.execute(select(coalesce_branch_losses_table).where(coalesce_branch_losses_table.c.run_id == run_id)).all()
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

    with pytest.raises(DataError), pg_db.engine.begin() as conn:
        conn.execute(
            insert(coalesce_branch_losses_table).values(
                loss_id="loss_raw_overlong",
                run_id=run_id,
                coalesce_name="merge",
                row_id="row-1",
                branch_name="path_b",
                token_id="tok-branch-b",
                reason="quarantined:" + str(BATTERY_REASON),
                recorded_by="worker:test",
                recorded_at=NOW,
                adopted_epoch=None,
            )
        )
