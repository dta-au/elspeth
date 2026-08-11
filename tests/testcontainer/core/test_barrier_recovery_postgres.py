"""PostgreSQL 16 backend support for committed barrier-result recovery.

This is local backend qualification only.  It deliberately does not invoke
the deployment-profile reporter or claim the maintained AWS composition.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]
from tests.integration.pipeline.test_barrier_intake_dispositions import (
    _T0,
    _arrive_via_intake,
    _branch_token,
    _coalesce_processor,
    _real_coalesce_executor,
    _usurp_seat,
    _work_item_row,
)

from elspeth.contracts import NodeType
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import coalesce_effects_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.clock import MockClock
from elspeth.engine.processor import BarrierJournalRestoreContext, RowProcessor

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as postgres:
        yield postgres.get_connection_url()


@pytest.mark.timeout(120)
def test_postgres_recovers_completed_coalesce_effect_without_remerge(
    postgres_url: str,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = LandscapeDB.from_url(postgres_url)
    factory = RecorderFactory(db, payload_store=FilesystemPayloadStore(tmp_path / "payloads"))
    factory.run_lifecycle.begin_run(
        config={},
        canonical_version="v1",
        run_id="test-run",
        leader_worker_id="seeder",
    )
    factory.data_flow.register_node(
        run_id="test-run",
        plugin_name="test-source",
        node_type=NodeType.SOURCE,
        plugin_version="1.0",
        config={},
        node_id="source-0",
        schema_config=SchemaConfig.from_dict({"mode": "observed"}),
    )
    clock = MockClock(start=_T0)
    executor_a = _real_coalesce_executor(factory, clock, policy="require_all")

    def crash_before_barrier_completion(self: RowProcessor, **kwargs: Any) -> None:
        del self, kwargs
        raise RuntimeError("injected crash before coalesce barrier completion")

    real_complete = RowProcessor._complete_coalesce_fire
    monkeypatch.setattr(RowProcessor, "_complete_coalesce_fire", crash_before_barrier_completion)
    processor_a = _coalesce_processor(factory, executor_a, clock)
    assert _arrive_via_intake(factory, processor_a, _branch_token("a")) == []
    with pytest.raises(RuntimeError, match="injected crash before coalesce barrier completion"):
        _arrive_via_intake(factory, processor_a, _branch_token("b"), ingest_sequence=1)
    monkeypatch.setattr(RowProcessor, "_complete_coalesce_fire", real_complete)

    with db.connection() as conn:
        effect = conn.execute(select(coalesce_effects_table)).mappings().one()
    assert effect["status"] == "completed"
    assert _work_item_row(db, "tok-branch-a")["status"] == TokenWorkStatus.BLOCKED.value
    assert _work_item_row(db, "tok-branch-b")["status"] == TokenWorkStatus.BLOCKED.value

    _usurp_seat(db, clock)
    executor_b = _real_coalesce_executor(factory, clock, policy="require_all")
    _coalesce_processor(
        factory,
        executor_b,
        clock,
        barrier_restore=BarrierJournalRestoreContext(
            resume_checkpoint_id="ckpt-postgres",
            barrier_scalars=None,
            batch_id_remap={},
        ),
        stamp_blocked_rows_adopted=False,
    )

    assert executor_b._pending == {}
    assert _work_item_row(db, "tok-branch-a")["status"] == TokenWorkStatus.TERMINAL.value
    assert _work_item_row(db, "tok-branch-b")["status"] == TokenWorkStatus.TERMINAL.value
    merged_work = _work_item_row(db, str(effect["result_token_id"]))
    assert merged_work["status"] == TokenWorkStatus.PENDING_SINK.value
