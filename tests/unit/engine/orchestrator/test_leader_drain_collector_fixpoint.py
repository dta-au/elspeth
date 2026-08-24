"""EOF fixpoint counts collector holds (WS3+WS4 integration item 16, spec §5).

A collector-only pipeline — no aggregations, no coalesce, no row_union —
used to skip ``run_end_of_input_barrier_flush``'s intake/flush loop entirely
at its early-return guard and strand every collector hold. There is no
collector flush ARM to add (a collector closes on end_of_group only; it has
nothing to force at EOF): the loop stays alive on ``has_blocked_barrier_work``
until intake settles each roster, and a roster that never settles is the
existing non-convergence raise, now naming the buffered collector count.

Port-shaped attribute-bag doubles, the pattern test_leader_drain_flush_bound.py
and test_finalize_source_iteration.py already use for this function.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.engine.orchestrator.leader_drain import run_end_of_input_barrier_flush
from elspeth.engine.orchestrator.types import ExecutionCounters
from tests.fixtures.factories import make_context


class _CollectorOnlyConfig:
    aggregation_settings: ClassVar[dict[str, Any]] = {}
    escalation_fixpoint_bound = 4


class _CollectorExecutorDouble:
    def __init__(self, *, buffered: int) -> None:
        self.buffered = buffered

    def buffered_member_count(self) -> int:
        return self.buffered


class _CollectorOnlyProcessor:
    """Each intake pass settles ``resolved_per_intake`` of the outstanding
    collector holds; has_blocked_barrier_work reports what remains."""

    run_id = "test-run"
    row_union_executor = None

    def __init__(self, *, collector_executor: _CollectorExecutorDouble | None, blocked_rows: int, resolved_per_intake: int) -> None:
        self.collector_executor = collector_executor
        self.blocked_rows = blocked_rows
        self.resolved_per_intake = resolved_per_intake
        self.intake_calls = 0

    def count_unquiesced_scheduler_work(self) -> int:
        return 0

    def summarize_unquiesced_scheduler_work(self) -> tuple[str, ...]:
        return ()

    def run_barrier_intake(self, ctx: Any) -> list[Any]:
        self.intake_calls += 1
        self.blocked_rows = max(0, self.blocked_rows - self.resolved_per_intake)
        return []

    def has_blocked_barrier_work(self) -> bool:
        return self.blocked_rows > 0

    def get_aggregation_buffer_count(self, node_id: Any) -> int:
        raise AssertionError("no aggregation in a collector-only pipeline")


def _run(processor: _CollectorOnlyProcessor) -> None:
    run_end_of_input_barrier_flush(
        config=_CollectorOnlyConfig(),  # type: ignore[arg-type]
        processor=processor,  # type: ignore[arg-type]
        ctx=make_context(),
        counters=ExecutionCounters(),
        pending_tokens={},
        coalesce_executor=None,
        coalesce_node_map={},
    )


def test_flush_loop_does_not_exit_while_collector_holds_remain() -> None:
    processor = _CollectorOnlyProcessor(collector_executor=_CollectorExecutorDouble(buffered=2), blocked_rows=2, resolved_per_intake=1)
    _run(processor)
    # Two holds settled one per intake; the second pass's convergence check
    # saw no BLOCKED work and the loop returned — it never early-returned at
    # zero (the guard mutant makes this 0).
    assert processor.intake_calls == 2


def test_flush_loop_raises_on_nonconverging_collector_holds_naming_the_buffered_count() -> None:
    processor = _CollectorOnlyProcessor(collector_executor=_CollectorExecutorDouble(buffered=5), blocked_rows=1, resolved_per_intake=0)
    with pytest.raises(
        OrchestrationInvariantError, match=r"did not converge within 4 intake/flush rounds.*Collector members still buffered in memory: 5"
    ):
        _run(processor)
    assert processor.intake_calls == 4


def test_pipeline_with_no_barriers_at_all_still_early_returns() -> None:
    """The control for the guard extension: with every executor absent the
    loop is skipped exactly as before, so an unrelated pipeline pays nothing."""
    processor = _CollectorOnlyProcessor(collector_executor=None, blocked_rows=1, resolved_per_intake=0)
    _run(processor)
    assert processor.intake_calls == 0
