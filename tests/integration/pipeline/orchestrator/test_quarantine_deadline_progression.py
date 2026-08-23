# tests/integration/pipeline/orchestrator/test_quarantine_deadline_progression.py
"""Quarantined-row consumption must advance ALL time-based deadlines.

A valid consumed row advances every deadline the main loop owns: the
aggregation trigger, the coalesce barrier deadline, and the row_union group
deadline. A CONSUMED QUARANTINED ROW is the same unit of progress and must
advance them identically (row_union: elspeth-c6d083d150; aggregation and
coalesce: elspeth-321f335ff2).

The starvation shape these tests pin is a CONTINUOUSLY READY quarantined
stream. Two properties make it dangerous and both are reproduced here:

* the quarantine branch ``continue``s before the valid path's per-row sweeps,
  so a missing sweep at that boundary defers the deadline to EOF; and
* the loop never blocks inside ``next()``, so the ``IdleTimeoutPump`` — the
  other mechanism that could fire a deadline — gets no idle window.

``tests/unit/engine/orchestrator/test_source_iteration_quarantine_sweep.py``
pins the loop CONTRACT for all three sweeps (every row boundary invokes each
one) against mocked executors. These tests pin the OBSERVABLE CONSEQUENCE for
the two buffering barriers end-to-end through a production ``Orchestrator``:
with real executors and a ``MockClock``, the buffered work actually flushes
DURING the quarantined stream rather than being deferred to the end-of-input
barrier flush. row_union keeps contract-level coverage only — its deadline
semantics are pinned by ``tests/unit/engine/test_row_union_executor.py``.

Distinguishing a deadline flush from an EOF flush
-------------------------------------------------
Sinks are written after the run body, so sink contents cannot tell the two
apart. Each test therefore observes at a TRANSFORM on the flush's output edge
and records, at invocation time, whether the source generator had reached
exhaustion. An observation with ``source_exhausted=False`` can only have come
from a deadline fired at a quarantined-row boundary.

Ruling out the idle pump
------------------------
The pump is the only other component that could fire these deadlines, and it
runs on its own worker thread. Both tests (a) stretch the driver's idle poll
interval so the pump cannot complete a poll interval during the run — the pump
guarantees "one full poll interval elapses before the first flush" — and
(b) assert the observation was made on the orchestrator thread. Either alone
would be suggestive; together they leave the quarantine branch as the only
possible origin.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from elspeth.contracts import Determinism, PipelineRow, RunStatus, SourceRow, TokenInfo
from elspeth.contracts.enums import FrameKind, OutputMode
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.plugin_protocols import SinkProtocol, SourceProtocol
from elspeth.contracts.schema_contract import FieldContract, SchemaContract
from elspeth.contracts.types import AggregationName, CoalesceName, NodeID
from elspeth.core.config import (
    AggregationSettings,
    CoalesceSettings,
    ElspethSettings,
    GateSettings,
    SourceSettings,
    TriggerConfig,
)
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.models import GraphValidationError
from elspeth.core.events import EventBusProtocol
from elspeth.core.landscape.data_flow_repository import DataFlowRepository
from elspeth.core.landscape.execution_repository import ExecutionRepository
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.clock import MockClock
from elspeth.engine.coalesce_executor import CoalesceExecutor
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.engine.orchestrator.ceremony import RunCeremony
from elspeth.engine.orchestrator.quarantine_router import QuarantineRouter
from elspeth.engine.orchestrator.run_state import LoopContext
from elspeth.engine.orchestrator.source_iteration import SourceIterationDriver
from elspeth.engine.orchestrator.source_lifecycle_recorder import SourceLifecycleRecorder
from elspeth.engine.orchestrator.types import ExecutionCounters
from elspeth.engine.processor import RowProcessor
from elspeth.engine.spans import SpanFactory
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.testing import make_pipeline_row, make_source_row, make_source_row_quarantined
from tests.fixtures.base_classes import _TestSchema, _TestSourceBase, as_sink, as_source, as_transform
from tests.fixtures.factories import wire_transforms
from tests.fixtures.landscape import make_landscape_db
from tests.fixtures.plugins import CollectSink


@contextmanager
def _null_track_operation(**_kwargs: Any):
    yield SimpleNamespace(operation=SimpleNamespace(operation_id="source-op-1"))


class _TokenManagerDouble:
    """Minimal double for the coalesce-merge witness's TokenManager dependency.

    Mirrors ``tests/unit/engine/test_coalesce_executor.py``'s
    ``_TokenManagerDouble`` — only ``coalesce_tokens`` is exercised by a
    ``best_effort`` timeout merge.
    """

    def coalesce_tokens(
        self,
        parents: list[TokenInfo],
        merged_data: PipelineRow,
        node_id: NodeID,
        run_id: str,
        **_kwargs: Any,
    ) -> tuple[TokenInfo, str]:
        merged = TokenInfo(
            row_id=parents[0].row_id,
            token_id=f"merged-{uuid4().hex[:8]}",
            row_data=merged_data,
        )
        return merged, f"join-{uuid4().hex[:8]}"


class _SpyCoalesceExecutor(CoalesceExecutor):
    """Records, per ``check_timeouts`` call, whether it resolved anything.

    A subclass override — not a runtime method assignment — per the
    project's masquerade-gate discipline (AGENTS.md: whole-tree AST gates
    pin dynamic-attribute sites, tests included).
    """

    def __init__(self, *args: Any, resolutions: list[bool], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._resolutions = resolutions

    def check_timeouts(self, coalesce_name: str) -> list[Any]:
        results = super().check_timeouts(coalesce_name)
        self._resolutions.append(bool(results))
        return results


# Deadline budget for the buffered work, and the jump the source applies once
# the first (valid) row has been consumed. The jump is far larger than the
# budget so the very next sweep — whichever one runs first — sees the deadline
# as expired; nothing here depends on wall-clock timing.
_DEADLINE_SECONDS = 1.0
_CLOCK_JUMP_SECONDS = 100.0

# Long enough that the pump cannot complete one poll interval during the run,
# so every flush observed below must come from an orchestrator-thread sweep.
# Read at pump build time, so a per-instance override is honoured.
_PUMP_DISABLING_POLL_INTERVAL_SECONDS = 3600.0

_QUARANTINED_ROW_COUNT = 3


@dataclass(frozen=True)
class _FlushObservation:
    """One transform invocation on a flushed deadline's output edge."""

    source_exhausted: bool
    thread: threading.Thread


class _QuarantineStreamSource(_TestSourceBase):
    """One valid row, then a continuously ready stream of quarantined rows.

    The clock jump happens after the valid row's yield resumes — i.e. once the
    engine has fully consumed it and latched the deadline anchor for whatever
    buffered work it opened. Every row after that is quarantined, so any
    deadline that fires before ``exhausted`` is set was advanced by
    quarantined-row consumption alone.
    """

    name = "quarantine_deadline_source"
    output_schema = _TestSchema
    _on_validation_failure = "quarantine"

    def __init__(self, *, clock: MockClock, on_success: str) -> None:
        super().__init__()
        self._clock = clock
        self.on_success = on_success
        self.exhausted = False

    def load(self, ctx: Any) -> Iterator[SourceRow]:
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="value",
                    original_name="value",
                    python_type=int,
                    required=False,
                    source="inferred",
                ),
            ),
            locked=True,
        )
        self._schema_contract = contract
        yield SourceRow.valid({"value": 1}, contract=contract, source_row_index=0)

        self._clock.advance(_CLOCK_JUMP_SECONDS)

        for source_row_index in range(1, _QUARANTINED_ROW_COUNT + 1):
            yield SourceRow.quarantined(
                row={"value": "not-an-int"},
                error="validation_failed",
                destination="quarantine",
                source_row_index=source_row_index,
            )

        self.exhausted = True


class _DeadlineFlushObserver(BaseTransform):
    """Records, per invocation, whether the source had reached exhaustion.

    Sits on the output edge of the flushed deadline (the aggregation's own
    batch transform, or a transform downstream of the coalesce), which is the
    earliest in-run point where a flush becomes observable.
    """

    determinism = Determinism.DETERMINISTIC
    input_schema = _TestSchema
    output_schema = _TestSchema
    on_error = "discard"

    def __init__(self, *, name: str, on_success: str, batch_aware: bool, source: _QuarantineStreamSource) -> None:
        super().__init__({"schema": {"mode": "observed"}})
        self.name = name
        self.on_success = on_success
        self.is_batch_aware = batch_aware
        self._source = source
        self.observations: list[_FlushObservation] = []

    def process(self, row: PipelineRow | list[PipelineRow], ctx: Any) -> TransformResult:
        self.observations.append(
            _FlushObservation(
                source_exhausted=self._source.exhausted,
                thread=threading.current_thread(),
            )
        )
        merged = len(row) if isinstance(row, list) else 1
        return TransformResult.success(
            make_pipeline_row({"value": merged}),
            success_reason={"action": "test"},
        )


def _run(
    config: PipelineConfig,
    graph: ExecutionGraph,
    clock: MockClock,
    payload_store: Any,
    settings: ElspethSettings | None = None,
) -> Any:
    """Run a pipeline with the idle pump throttled out of contention."""
    orchestrator = Orchestrator(make_landscape_db(), clock=clock)
    orchestrator._source_driver._SOURCE_IDLE_POLL_INTERVAL_SECONDS = _PUMP_DISABLING_POLL_INTERVAL_SECONDS
    return orchestrator.run(config, graph=graph, settings=settings, payload_store=payload_store)


def _assert_fired_before_end_of_input(observer: _DeadlineFlushObserver, *, deadline: str) -> None:
    """Pin the flush to a quarantined-row boundary, not the EOF barrier flush.

    Call from the thread that invoked ``_run``: ``Orchestrator.run`` is
    synchronous, so the caller's thread IS the orchestrator thread, and any
    flush recorded on a different one came from the idle pump's worker.
    """
    assert observer.observations, f"the {deadline} deadline never fired at all — the test is vacuous"

    orchestrator_thread = threading.current_thread()
    pre_eof = [obs for obs in observer.observations if not obs.source_exhausted]
    assert pre_eof, (
        f"the {deadline} deadline only fired after the source was exhausted: {observer.observations!r}. "
        "A continuously ready quarantined stream starved it until the end-of-input barrier flush."
    )
    off_thread = [obs for obs in pre_eof if obs.thread is not orchestrator_thread]
    assert not off_thread, (
        f"the pre-EOF {deadline} flush ran on {off_thread!r}, not the orchestrator thread — "
        "the idle pump fired it, so this run does not exercise the quarantine-branch sweep."
    )


class TestQuarantinedRowsAdvanceAggregationDeadlines:
    """Aggregation timeouts must fire from quarantined-row progression."""

    def test_aggregation_timeout_fires_during_a_quarantined_stream(self, payload_store) -> None:
        clock = MockClock(start=1_750_000_000.0)
        source = _QuarantineStreamSource(clock=clock, on_success="agg_in")
        observer = _DeadlineFlushObserver(
            name="aggregation_deadline_observer",
            on_success="output",
            batch_aware=True,
            source=source,
        )
        output_sink = CollectSink("output")
        quarantine_sink = CollectSink("quarantine")

        # count=100 never fires for a single buffered row, so the timeout is
        # the ONLY trigger that can flush this batch before end-of-input.
        agg_settings = AggregationSettings(
            name="hold_until_deadline",
            plugin=observer.name,
            input="agg_in",
            on_success="output",
            on_error="discard",
            trigger=TriggerConfig(count=100, timeout_seconds=_DEADLINE_SECONDS),
            output_mode=OutputMode.TRANSFORM,
        )
        graph = ExecutionGraph.from_plugin_instances(
            sources={"primary": as_source(source)},
            source_settings_map={
                "primary": SourceSettings(plugin=source.name, on_success="agg_in", options={}),
            },
            transforms=[],
            sinks={"output": as_sink(output_sink), "quarantine": as_sink(quarantine_sink)},
            aggregations={"hold_until_deadline": (as_transform(observer), agg_settings)},
            gates=[],
        )
        agg_node_id = graph.get_aggregation_id_map()[AggregationName("hold_until_deadline")]
        observer.node_id = agg_node_id

        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(observer)],
            sinks={"output": as_sink(output_sink), "quarantine": as_sink(quarantine_sink)},
            aggregation_settings={agg_node_id: agg_settings},
        )

        result = _run(config, graph, clock, payload_store)

        # Quarantined rows are a failure lifecycle, so the run terminates
        # COMPLETED_WITH_FAILURES; the assertion pins that it terminated
        # cleanly rather than crashing mid-sweep.
        assert result.status == RunStatus.COMPLETED_WITH_FAILURES
        assert result.rows_quarantined == _QUARANTINED_ROW_COUNT
        _assert_fired_before_end_of_input(observer, deadline="aggregation")


class TestQuarantinedRowsAdvanceCoalesceDeadlines:
    """Coalesce barrier timeouts must fire from quarantined-row progression."""

    def test_hold_branch_aggregation_config_is_rejected_by_rule_6(self, payload_store) -> None:
        """Reclassified under ruling 25 (spec §7 rule 6, Task 9, WS2 controller ruling 2026-08-23).

        This config used to build the pending barrier by parking
        ``held_branch``'s forked token inside an aggregation whose
        ``count=100`` trigger never fires — an in-region aggregation,
        which `validate_no_aggregations_in_regions` (core/dag/
        bound_regions.py) now rejects at build time regardless of
        ``output_mode``. The engine behavior this config used to prove
        end-to-end — a pending ``best_effort`` barrier resolving from a
        quarantined-row sweep, not deferred to end-of-input — is rebuilt
        at the engine level in
        ``test_coalesce_timeout_resolves_a_pending_barrier_at_a_
        quarantine_boundary_not_eof`` below, which hand-accepts a
        genuinely overdue barrier directly against a real
        ``CoalesceExecutor`` instead of requiring a live run to produce it
        (WS4 restoration tracked as elspeth-ad0c4980fd, sibling of
        elspeth-c648d4f832).
        """
        clock = MockClock(start=1_750_000_000.0)
        source = _QuarantineStreamSource(clock=clock, on_success="fork_input")

        held_branch = _DeadlineFlushObserver(
            name="held_branch_batch",
            on_success="held_ready",
            batch_aware=True,
            source=source,
        )
        observer = _DeadlineFlushObserver(
            name="coalesce_deadline_observer",
            on_success="output",
            batch_aware=False,
            source=source,
        )
        output_sink = CollectSink("output")
        quarantine_sink = CollectSink("quarantine")

        fork_gate = GateSettings(
            name="fork_gate",
            input="fork_input",
            condition="True",
            routes={"true": "fork", "false": "fork"},
            fork_to=["held_branch", "direct_branch"],
        )
        coalesce = CoalesceSettings(
            name="merge_paths",
            branches={"held_branch": "held_ready", "direct_branch": "direct_branch"},
            policy="best_effort",
            timeout_seconds=_DEADLINE_SECONDS,
            merge="nested",
        )
        held_settings = AggregationSettings(
            name="hold_branch",
            plugin=held_branch.name,
            input="held_branch",
            on_success="held_ready",
            on_error="discard",
            trigger=TriggerConfig(count=100),
            output_mode=OutputMode.TRANSFORM,
        )
        wired_observer = wire_transforms(
            [as_transform(observer)],
            source_connection=coalesce.name,
            final_sink="output",
            names=["post_merge_observer"],
        )

        with pytest.raises(GraphValidationError, match=r"Aggregation .* inside bound region"):
            ExecutionGraph.from_plugin_instances(
                sources={"primary": as_source(source)},
                source_settings_map={
                    "primary": SourceSettings(plugin=source.name, on_success="fork_input", options={}),
                },
                transforms=wired_observer,
                sinks={"output": as_sink(output_sink), "quarantine": as_sink(quarantine_sink)},
                aggregations={"hold_branch": (as_transform(held_branch), held_settings)},
                gates=[fork_gate],
                coalesce_settings=[coalesce],
            )

    def test_coalesce_timeout_resolves_a_pending_barrier_at_a_quarantine_boundary_not_eof(self) -> None:
        """Engine-level rebuild (maintainer ruling 2026-08-23, WS2 controller Task 9).

        The claim this test pins does not depend on HOW a coalesce barrier
        became pending — only on WHEN the engine notices and resolves an
        overdue one. It is rebuilt by driving the REAL
        ``SourceIterationDriver.run_main_processing_loop`` — the same loop
        ``tests/unit/engine/orchestrator/test_source_iteration_quarantine_
        sweep.py`` pins the call-count CONTRACT for, against a mocked
        executor — but here against a REAL ``CoalesceExecutor`` carrying a
        hand-accepted, genuinely overdue pending barrier: one branch
        ("direct_branch") arrives, the other ("held_branch") never does,
        mirroring the config-level scenario's shape without needing an
        in-region aggregation to produce it. ``RowProcessor`` stays a mock,
        exactly as it already does in that contract test — it is not the
        subject here; ``CoalesceExecutor.accept()`` and
        ``.check_timeouts()`` run as real, unmocked production code.

        Distinguishing shape: the clock only advances once the loop moves
        past the valid row (via a side effect on the quarantine router's
        ``route()``, mirroring the original source's clock jump timing —
        elspeth-321f335ff2's fix added the sweep call INSIDE the
        quarantine branch, so resolution must happen on a quarantined-row
        boundary call, not the valid row's own). A spy on
        ``check_timeouts`` records, per call, whether it resolved anything:
        call 1 (valid row's own boundary, deadline not yet due) must NOT
        resolve; call 2 (first quarantined-row boundary, after the clock
        jump) MUST resolve; call 3 (second quarantined-row boundary) sees
        nothing left pending. ``flush_end_of_input=False`` here rules out
        an end-of-input flush entirely, so resolution can ONLY have come
        from a per-row sweep.
        """
        clock = MockClock(start=1_750_000_000.0)

        execution = MagicMock(spec=ExecutionRepository)
        execution.begin_node_state.side_effect = lambda **kw: SimpleNamespace(state_id=f"cs-{uuid4().hex[:8]}")
        execution.get_completed_row_ids_for_nodes.return_value = set()
        execution.has_completed_row_for_node.return_value = False
        data_flow = MagicMock(spec=DataFlowRepository)

        resolutions: list[bool] = []
        coalesce_node_id = NodeID("coalesce-merge_paths")
        coalesce_executor = _SpyCoalesceExecutor(
            execution,
            SimpleNamespace(),
            _TokenManagerDouble(),
            "run-quarantine-coalesce",
            step_resolver=lambda node_id: 5,
            clock=clock,
            data_flow=data_flow,
            barrier_restore_reads=SimpleNamespace(
                get_completed_row_ids_for_nodes=execution.get_completed_row_ids_for_nodes,
                has_completed_row_for_node=execution.has_completed_row_for_node,
            ),
            resolutions=resolutions,
        )
        coalesce_executor.register_coalesce(
            CoalesceSettings(
                name="merge_paths",
                branches=["held_branch", "direct_branch"],
                policy="best_effort",
                timeout_seconds=_DEADLINE_SECONDS,
                merge="nested",
            ),
            coalesce_node_id,
        )

        def _process_valid_row(**_kwargs: Any) -> list[Any]:
            # What a real RowProcessor.process_row would have done for this
            # row: deliver the direct_branch fork child to the coalesce
            # point. held_branch's child NEVER arrives — same shape as the
            # config-level scenario, where its aggregation's count=100
            # trigger never fired.
            token = TokenInfo(
                row_id="row-1",
                token_id="tok-direct",
                row_data=make_pipeline_row({"value": 1}),
                lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="direct_branch"),),
            )
            coalesce_executor.accept(token, "merge_paths", arrival_time=clock.monotonic())
            return []

        processor = MagicMock(spec=RowProcessor)
        processor.process_row.side_effect = _process_valid_row
        processor.complete_coalesce_merge.return_value = []
        processor.row_union_executor = None

        driver = SourceIterationDriver(
            events=MagicMock(spec=EventBusProtocol),
            span_factory=MagicMock(spec=SpanFactory),
            ceremony=MagicMock(spec=RunCeremony),
        )
        driver._quarantine_router = MagicMock(spec=QuarantineRouter)
        # The clock only jumps once the loop moves past the valid row —
        # i.e. once a quarantined row is on deck — mirroring the original
        # source's clock-jump timing. The jump is far larger than the
        # timeout budget so the very next sweep sees the deadline expired.
        driver._quarantine_router.route.side_effect = lambda *a, **kw: clock.advance(_CLOCK_JUMP_SECONDS)
        lifecycle = MagicMock(spec=SourceLifecycleRecorder)
        lifecycle.record_field_resolution.return_value = ({}, None)
        driver._lifecycle_recorder = lifecycle

        source = MagicMock(spec=SourceProtocol)
        source.name = "fake"
        source.on_success = "default"
        sink = MagicMock(spec=SinkProtocol)
        sink.name = "default"

        rows = [
            make_source_row({"value": 1}, source_row_index=0),
            make_source_row_quarantined({"value": "bad"}, source_row_index=1),
            make_source_row_quarantined({"value": "worse"}, source_row_index=2),
        ]
        driver.load_source_with_events = lambda run_id, ctx, active_source: iter(rows)  # type: ignore[method-assign]

        config = PipelineConfig(sources={"fake": source}, transforms=(), sinks={"default": sink})
        loop_ctx = LoopContext(
            counters=ExecutionCounters(),
            pending_tokens={"default": []},
            processor=processor,
            ctx=MagicMock(spec=PluginContext),
            config=config,
            agg_transform_lookup={},
            coalesce_executor=coalesce_executor,
            coalesce_node_map={CoalesceName("merge_paths"): coalesce_node_id},
        )

        with (
            patch("elspeth.engine.orchestrator.source_iteration.track_operation", _null_track_operation),
            patch("elspeth.engine.orchestrator.source_iteration.record_schema_contract", return_value=True),
        ):
            driver.run_main_processing_loop(
                loop_ctx,
                factory=MagicMock(spec=RecorderFactory),
                run_id="run-quarantine-coalesce",
                source_id=NodeID("src"),
                edge_map={},
                active_source_name="fake",
                active_source=source,
                flush_end_of_input=False,
            )

        assert resolutions == [False, True, False], (
            f"expected the barrier to resolve on the FIRST quarantined-row boundary sweep "
            f"(call #2 of 3) — not row 1's own boundary (call #1, deadline not yet due) and "
            f"not deferred past the loop (flush_end_of_input=False rules out EOF entirely) — "
            f"got {resolutions!r}"
        )


if __name__ == "__main__":  # pragma: no cover - convenience only
    pytest.main([__file__])
