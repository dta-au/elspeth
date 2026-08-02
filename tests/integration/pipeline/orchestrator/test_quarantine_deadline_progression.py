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
from dataclasses import dataclass
from typing import Any

import pytest

from elspeth.contracts import Determinism, PipelineRow, RunStatus, SourceRow
from elspeth.contracts.enums import OutputMode
from elspeth.contracts.schema_contract import FieldContract, SchemaContract
from elspeth.contracts.types import AggregationName
from elspeth.core.config import (
    AggregationSettings,
    CoalesceSettings,
    ElspethSettings,
    GateSettings,
    SourceSettings,
    TriggerConfig,
)
from elspeth.core.dag import ExecutionGraph
from elspeth.engine.clock import MockClock
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.testing import make_pipeline_row
from tests.fixtures.base_classes import _TestSchema, _TestSourceBase, as_sink, as_source, as_transform
from tests.fixtures.factories import wire_transforms
from tests.fixtures.landscape import make_landscape_db
from tests.fixtures.plugins import CollectSink

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

    def test_coalesce_timeout_fires_during_a_quarantined_stream(self, payload_store) -> None:
        clock = MockClock(start=1_750_000_000.0)
        source = _QuarantineStreamSource(clock=clock, on_success="fork_input")

        # The held branch buffers its token in an aggregation whose count=100
        # trigger never fires, so the valid row's barrier stays pending at
        # 1-of-2 arrived — a durable pending group that only its timeout can
        # resolve. best_effort merges what arrived, giving the post-coalesce
        # transform a row to observe.
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
            # on_success stays None so the coalesce is NON-TERMINAL: it
            # publishes its own name as a connection that the observing
            # transform below consumes, instead of writing straight to a sink.
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
        # The observation point is a POST-COALESCE transform, not a sink: sinks
        # are written after the run body, so a sink row cannot distinguish a
        # deadline flush from an end-of-input flush.
        wired_observer = wire_transforms(
            [as_transform(observer)],
            source_connection=coalesce.name,
            final_sink="output",
            names=["post_merge_observer"],
        )

        graph = ExecutionGraph.from_plugin_instances(
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
        held_node_id = graph.get_aggregation_id_map()[AggregationName("hold_branch")]
        held_branch.node_id = held_node_id

        # Wired transforms are resolved to graph nodes BY POSITION, so the
        # observer must occupy the same index here as in ``wired_observer``;
        # the aggregation's transform is resolved by ``node_id`` instead and
        # follows.
        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(observer), as_transform(held_branch)],
            sinks={"output": as_sink(output_sink), "quarantine": as_sink(quarantine_sink)},
            aggregation_settings={held_node_id: held_settings},
            gates=[fork_gate],
            coalesce_settings=[coalesce],
        )
        settings = ElspethSettings(
            sources={"primary": {"plugin": source.name, "on_success": "fork_input", "options": {}}},
            sinks={
                "output": {"plugin": "test", "on_write_failure": "discard"},
                "quarantine": {"plugin": "test", "on_write_failure": "discard"},
            },
            gates=[fork_gate],
            coalesce=[coalesce],
        )

        result = _run(config, graph, clock, payload_store, settings)

        # Quarantined rows are a failure lifecycle, so the run terminates
        # COMPLETED_WITH_FAILURES; the assertion pins that it terminated
        # cleanly rather than crashing mid-sweep.
        assert result.status == RunStatus.COMPLETED_WITH_FAILURES
        assert result.rows_quarantined == _QUARANTINED_ROW_COUNT
        _assert_fired_before_end_of_input(observer, deadline="coalesce")


if __name__ == "__main__":  # pragma: no cover - convenience only
    pytest.main([__file__])
