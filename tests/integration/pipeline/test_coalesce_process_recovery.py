"""Committed coalesce-effect recovery at the scheduler completion seam."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.core.landscape.schema import coalesce_effects_table, token_work_items_table
from elspeth.engine.clock import MockClock
from elspeth.engine.processor import BarrierJournalRestoreContext, RowProcessor
from tests.integration.pipeline.test_barrier_intake_dispositions import (
    _T0,
    _arrive_via_intake,
    _branch_token,
    _coalesce_processor,
    _real_coalesce_executor,
    _usurp_seat,
    _work_item_row,
)
from tests.unit.engine.test_processor import _make_factory


@pytest.mark.timeout(120)
def test_completed_coalesce_effect_before_barrier_completion_resumes_without_remerge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh leader publishes the durable merged token exactly once."""
    clock = MockClock(start=_T0)
    db, factory = _make_factory()
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
        result_work = conn.execute(
            select(token_work_items_table.c.work_item_id).where(token_work_items_table.c.token_id == effect["result_token_id"])
        ).all()
    assert effect["status"] == "completed"
    assert result_work == []
    assert _work_item_row(db, "tok-branch-a")["status"] == TokenWorkStatus.BLOCKED.value
    assert _work_item_row(db, "tok-branch-b")["status"] == TokenWorkStatus.BLOCKED.value

    _usurp_seat(db, clock)
    executor_b = _real_coalesce_executor(factory, clock, policy="require_all")
    processor_b = _coalesce_processor(
        factory,
        executor_b,
        clock,
        barrier_restore=BarrierJournalRestoreContext(
            resume_checkpoint_id="ckpt-takeover",
            barrier_scalars=None,
            batch_id_remap={},
        ),
        stamp_blocked_rows_adopted=False,
    )

    assert executor_b._pending == {}, "the completed merge must not be rebuilt or executed again"
    assert _work_item_row(db, "tok-branch-a")["status"] == TokenWorkStatus.TERMINAL.value
    assert _work_item_row(db, "tok-branch-b")["status"] == TokenWorkStatus.TERMINAL.value
    merged_work = _work_item_row(db, str(effect["result_token_id"]))
    assert merged_work["status"] == TokenWorkStatus.PENDING_SINK.value
    assert merged_work["pending_sink_name"] == "out"
    with db.connection() as conn:
        assert conn.execute(select(coalesce_effects_table.c.effect_id)).all() == [(effect["effect_id"],)]
    assert processor_b.has_blocked_barrier_work() is False


@pytest.mark.timeout(120)
def test_completed_coalesce_effect_with_partial_blocked_members_refuses_without_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proper subset is corruption, not a set of discardable late arrivals."""
    clock = MockClock(start=_T0)
    db, factory = _make_factory()
    executor_a = _real_coalesce_executor(factory, clock, policy="require_all")

    def crash_before_barrier_completion(self: RowProcessor, **kwargs: Any) -> None:
        del self, kwargs
        raise RuntimeError("injected crash before coalesce barrier completion")

    real_complete = RowProcessor._complete_coalesce_fire
    monkeypatch.setattr(RowProcessor, "_complete_coalesce_fire", crash_before_barrier_completion)
    processor_a = _coalesce_processor(factory, executor_a, clock)
    assert _arrive_via_intake(factory, processor_a, _branch_token("a")) == []
    with pytest.raises(RuntimeError, match="injected crash"):
        _arrive_via_intake(factory, processor_a, _branch_token("b"), ingest_sequence=1)
    monkeypatch.setattr(RowProcessor, "_complete_coalesce_fire", real_complete)

    with db.write_connection() as conn:
        effect = conn.execute(select(coalesce_effects_table)).mappings().one()
        conn.execute(
            update(token_work_items_table)
            .where(token_work_items_table.c.token_id == "tok-branch-b")
            .values(status=TokenWorkStatus.TERMINAL.value)
        )

    _usurp_seat(db, clock)
    with pytest.raises(AuditIntegrityError, match="partial BLOCKED member set"):
        _coalesce_processor(
            factory,
            _real_coalesce_executor(factory, clock, policy="require_all"),
            clock,
            barrier_restore=BarrierJournalRestoreContext(
                resume_checkpoint_id="ckpt-partial",
                barrier_scalars=None,
                batch_id_remap={},
            ),
            stamp_blocked_rows_adopted=False,
        )

    assert _work_item_row(db, "tok-branch-a")["status"] == TokenWorkStatus.BLOCKED.value
    with db.connection() as conn:
        result_work = conn.execute(
            select(token_work_items_table.c.work_item_id).where(token_work_items_table.c.token_id == effect["result_token_id"])
        ).all()
    assert result_work == []
