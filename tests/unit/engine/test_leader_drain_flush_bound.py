"""ONE fixpoint formula (2026-08-22 synthesis): WS2 owns
derive_escalation_fixpoint_bound(depth) = 1_000 + 8 * depth in
core/dag/bound_regions.py, and leader_drain iterates
PipelineConfig.escalation_fixpoint_bound (derived at build from
graph.get_max_bound_region_depth()). This suite pins WS3's CONSUMPTION:
no competing formula, and the loop honors the derived value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.core.dag.bound_regions import derive_escalation_fixpoint_bound
from elspeth.engine.orchestrator import leader_drain
from elspeth.engine.orchestrator.leader_drain import run_end_of_input_barrier_flush
from elspeth.engine.orchestrator.types import ExecutionCounters
from tests.fixtures.factories import make_context


def test_leader_drain_owns_no_competing_formula() -> None:
    assert not hasattr(leader_drain, "derive_end_of_input_flush_bound")
    # WS2 Task 5 (F4) DELETED the old constant outright — it must not
    # resurface as a second source of truth beside the derived bound.
    assert not hasattr(leader_drain, "MAX_END_OF_INPUT_FLUSH_ITERATIONS")


def test_depth0_derived_bound_equals_the_historical_constant() -> None:
    # Depth-0 behaviour is byte-identical to the pre-campaign loop. The
    # historical MAX_END_OF_INPUT_FLUSH_ITERATIONS = 1_000 no longer exists
    # as a name (deleted by WS2); the derived value is the only anchor.
    assert derive_escalation_fixpoint_bound(0) == 1_000


def test_depth5_derived_bound_is_1040() -> None:
    # The value Task 10's acceptance run flushes inside (consumed-contract pin).
    assert derive_escalation_fixpoint_bound(5) == 1_040


# ---------------------------------------------------------------------------
# Consumption harness: a durable BLOCKED barrier hold that NEVER releases
# (has_blocked_barrier_work() always True) drives run_end_of_input_barrier_flush
# to non-convergence. Plain attribute-bag doubles (the _ConfigDouble/
# _ProcessorDouble pattern already used in
# tests/unit/engine/orchestrator/test_finalize_source_iteration.py for this
# same function) — no isinstance check on `config`/`processor` runs at
# runtime, only attribute/method access, so a real PipelineConfig/RowProcessor
# is not required to pin the loop's own consumption of the derived bound.
# ---------------------------------------------------------------------------


class _NonConvergingConfig:
    def __init__(self, *, escalation_fixpoint_bound: int) -> None:
        self.aggregation_settings: dict[str, Any] = {}
        self.escalation_fixpoint_bound = escalation_fixpoint_bound


class _NonConvergingCoalesceExecutor:
    """Bypasses run_end_of_input_barrier_flush's `not aggregation_settings and
    coalesce_executor is None and row_union_executor is None` early return —
    a bare non-None sentinel is not enough, `flush_coalesce_pending` calls
    `flush_pending()` on it every iteration."""

    def flush_pending(self) -> list[Any]:
        return []


class _NonConvergingProcessor:
    run_id = "test-run"
    row_union_executor = None

    def count_unquiesced_scheduler_work(self) -> int:
        return 0

    def summarize_unquiesced_scheduler_work(self) -> tuple[str, ...]:
        return ()

    def run_barrier_intake(self, ctx: Any) -> list[Any]:
        return []

    def has_blocked_barrier_work(self) -> bool:
        # A durable BLOCKED barrier hold that never releases: the loop can
        # never observe convergence, so it always runs the full bound.
        return True

    def get_aggregation_buffer_count(self, node_id: Any) -> int:
        return 0


@dataclass
class _NonConvergingFlushHarness:
    def run(self, *, escalation_fixpoint_bound: int) -> None:
        run_end_of_input_barrier_flush(
            config=_NonConvergingConfig(escalation_fixpoint_bound=escalation_fixpoint_bound),  # type: ignore[arg-type]
            processor=_NonConvergingProcessor(),  # type: ignore[arg-type]
            ctx=make_context(),
            counters=ExecutionCounters(),
            pending_tokens={},
            coalesce_executor=_NonConvergingCoalesceExecutor(),  # type: ignore[arg-type]
            coalesce_node_map={},
        )


@pytest.fixture
def non_converging_flush_harness() -> _NonConvergingFlushHarness:
    return _NonConvergingFlushHarness()


def test_flush_loop_iterates_the_configured_bound_not_the_constant(non_converging_flush_harness: _NonConvergingFlushHarness) -> None:
    """Drive a never-converging flush with escalation_fixpoint_bound=3: the
    OrchestrationInvariantError names 3 — the loop reads the config field,
    not MAX_END_OF_INPUT_FLUSH_ITERATIONS."""
    with pytest.raises(OrchestrationInvariantError, match=r"within 3 intake/flush rounds"):
        non_converging_flush_harness.run(escalation_fixpoint_bound=3)
