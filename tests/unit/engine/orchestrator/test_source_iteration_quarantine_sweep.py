# tests/unit/engine/orchestrator/test_source_iteration_quarantine_sweep.py
"""Timeout sweep coverage at quarantined-row boundaries.

The main loop's quarantine branch continues the loop before the per-row
sweeps, and a continuously ready quarantined stream keeps the source
non-idle (the idle pump only fires while the loop is blocked inside
``next()``). Without sweeps at the quarantine boundary, pending barrier
group deadlines and buffered aggregation timeouts starve until EOF and are
misclassified there — row_union: elspeth-c6d083d150; coalesce and
aggregation: elspeth-321f335ff2. These tests pin the loop contract only:
every row boundary — quarantined included — invokes each sweep. The timeout
SEMANTICS (what a stale group or batch does when swept) are pinned by the
executor/aggregation tests.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.plugin_protocols import SinkProtocol, SourceProtocol
from elspeth.contracts.types import CoalesceName, NodeID
from elspeth.core.events import EventBusProtocol
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import RunSourceLifecycleState
from elspeth.engine.coalesce_executor import CoalesceExecutor
from elspeth.engine.orchestrator import PipelineConfig
from elspeth.engine.orchestrator.ceremony import RunCeremony
from elspeth.engine.orchestrator.quarantine_router import QuarantineRouter
from elspeth.engine.orchestrator.run_state import LoopContext
from elspeth.engine.orchestrator.source_iteration import SourceIterationDriver
from elspeth.engine.orchestrator.source_lifecycle_recorder import SourceLifecycleRecorder
from elspeth.engine.orchestrator.types import AggregationFlushResult, ExecutionCounters
from elspeth.engine.processor import RowProcessor
from elspeth.engine.row_union_executor import RowUnionExecutor
from elspeth.engine.spans import SpanFactory
from elspeth.testing import make_source_row, make_source_row_quarantined


@contextmanager
def _null_track_operation(**_kwargs: Any):
    yield SimpleNamespace(operation=SimpleNamespace(operation_id="source-op-1"))


def _drive_quarantined_stream(
    *,
    row_union_executor: MagicMock | None = None,
    coalesce_executor: MagicMock | None = None,
    coalesce_node_map: dict[CoalesceName, NodeID] | None = None,
) -> SourceIterationDriver:
    """Drive one valid row + two quarantined rows through the main loop.

    The starvation scenario is a CONTINUOUSLY READY stream: the loop never
    blocks in ``next()``, so the idle pump cannot fire. The config carries no
    aggregation/coalesce timeout settings and any row_union executor passed
    here should report ``has_timeout_configured() is False``, so idle polling
    stays off (and the pump's worker thread stays out of the call counts).

    Returns the driver so callers can assert on the mocked QuarantineRouter.
    """
    driver = SourceIterationDriver(
        events=MagicMock(spec=EventBusProtocol),
        span_factory=MagicMock(spec=SpanFactory),
        ceremony=MagicMock(spec=RunCeremony),
    )
    driver._quarantine_router = MagicMock(spec=QuarantineRouter)
    lifecycle = MagicMock(spec=SourceLifecycleRecorder)
    lifecycle.record_field_resolution.return_value = ({}, None)
    driver._lifecycle_recorder = lifecycle

    processor = MagicMock(spec=RowProcessor)
    processor.process_row.return_value = []
    processor.row_union_executor = row_union_executor

    source = MagicMock(spec=SourceProtocol)
    source.name = "fake"
    source.on_success = "default"
    sink = MagicMock(spec=SinkProtocol)
    sink.name = "default"

    rows = [
        make_source_row({"amount": 1}, source_row_index=0),
        make_source_row_quarantined({"amount": "bad"}, source_row_index=1),
        make_source_row_quarantined({"amount": "worse"}, source_row_index=2),
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
        coalesce_node_map=coalesce_node_map if coalesce_node_map is not None else {},
    )

    with (
        patch("elspeth.engine.orchestrator.source_iteration.track_operation", _null_track_operation),
        patch("elspeth.engine.orchestrator.source_iteration.record_schema_contract", return_value=True),
    ):
        driver.run_main_processing_loop(
            loop_ctx,
            factory=MagicMock(spec=RecorderFactory),
            run_id="run-quarantine-sweep",
            source_id=NodeID("src"),
            edge_map={},
            active_source_name="fake",
            active_source=source,
            flush_end_of_input=False,
        )

    return driver


def test_quarantined_row_boundaries_sweep_row_union_timeouts() -> None:
    row_union_executor = MagicMock(spec=RowUnionExecutor)
    row_union_executor.get_registered_names.return_value = ["variant_union"]
    row_union_executor.check_timeouts.return_value = []
    row_union_executor.has_timeout_configured.return_value = False

    driver = _drive_quarantined_stream(row_union_executor=row_union_executor)

    assert driver._quarantine_router.route.call_count == 2
    # One sweep per ROW BOUNDARY: the valid row's normal-path sweep plus one
    # at EACH quarantined-row boundary. Before elspeth-c6d083d150 the
    # quarantine branch continued the loop without sweeping (count == 1).
    assert row_union_executor.check_timeouts.call_count == 3
    row_union_executor.check_timeouts.assert_called_with("variant_union")


def test_quarantined_row_boundaries_sweep_coalesce_timeouts() -> None:
    coalesce_executor = MagicMock(spec=CoalesceExecutor)
    coalesce_executor.get_registered_names.return_value = ["merge"]
    coalesce_executor.check_timeouts.return_value = []

    driver = _drive_quarantined_stream(
        coalesce_executor=coalesce_executor,
        coalesce_node_map={CoalesceName("merge"): NodeID("coalesce-node")},
    )

    assert driver._quarantine_router.route.call_count == 2
    # Same boundary contract as the row_union sweep (elspeth-321f335ff2):
    # the valid row's normal-path sweep plus one at EACH quarantined-row
    # boundary. Before the fix the quarantine branch swept row_union only.
    assert coalesce_executor.check_timeouts.call_count == 3
    coalesce_executor.check_timeouts.assert_called_with(coalesce_name="merge")


def test_quarantined_row_boundaries_check_aggregation_timeouts() -> None:
    with patch(
        "elspeth.engine.orchestrator.source_iteration.check_aggregation_timeouts",
        return_value=AggregationFlushResult(),
    ) as check_timeouts:
        driver = _drive_quarantined_stream()

    assert driver._quarantine_router.route.call_count == 2
    # The valid row's BEFORE-processing check plus one at EACH quarantined-row
    # boundary (elspeth-321f335ff2). Before the fix a continuously ready
    # quarantined stream starved buffered aggregation timeouts (count == 1).
    assert check_timeouts.call_count == 3


def test_shutdown_set_during_empty_first_fetch_records_interrupted_without_eof_flush() -> None:
    driver = SourceIterationDriver(
        events=MagicMock(spec=EventBusProtocol),
        span_factory=MagicMock(spec=SpanFactory),
        ceremony=MagicMock(spec=RunCeremony),
    )
    lifecycle = MagicMock(spec=SourceLifecycleRecorder)
    lifecycle.record_field_resolution.return_value = ({}, None)
    driver._lifecycle_recorder = lifecycle

    processor = MagicMock(spec=RowProcessor)
    processor.row_union_executor = None
    processor.collector_executor = None
    source = MagicMock(spec=SourceProtocol)
    source.name = "llm"
    source.on_success = "default"
    sink = MagicMock(spec=SinkProtocol)
    sink.name = "default"
    loop_ctx = LoopContext(
        counters=ExecutionCounters(),
        pending_tokens={"default": []},
        processor=processor,
        ctx=MagicMock(spec=PluginContext),
        config=PipelineConfig(sources={"llm": source}, transforms=(), sinks={"default": sink}),
        agg_transform_lookup={},
        coalesce_executor=None,
        coalesce_node_map={},
    )
    shutdown_event = threading.Event()

    def interrupt_during_first_fetch() -> Any:
        shutdown_event.set()
        return
        yield

    driver.load_source_with_events = lambda run_id, ctx, active_source: interrupt_during_first_fetch()  # type: ignore[method-assign]

    with (
        patch("elspeth.engine.orchestrator.source_iteration.track_operation", _null_track_operation),
        patch("elspeth.engine.orchestrator.source_iteration.record_schema_contract", return_value=True),
        patch("elspeth.engine.orchestrator.source_iteration.run_end_of_input_barrier_flush") as end_of_input_flush,
    ):
        result = driver.run_main_processing_loop(
            loop_ctx,
            factory=MagicMock(spec=RecorderFactory),
            run_id="run-pre-egress-shutdown",
            source_id=NodeID("src"),
            edge_map={},
            active_source_name="llm",
            active_source=source,
            shutdown_event=shutdown_event,
            flush_end_of_input=True,
        )

    assert result.interrupted is True
    states = [call.args[-1] for call in lifecycle.record_run_source_lifecycle.call_args_list]
    assert states == [RunSourceLifecycleState.LOADING, RunSourceLifecycleState.INTERRUPTED]
    assert RunSourceLifecycleState.EXHAUSTED not in states
    processor.process_row.assert_not_called()
    end_of_input_flush.assert_not_called()
