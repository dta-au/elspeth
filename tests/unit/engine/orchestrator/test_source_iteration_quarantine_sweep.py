# tests/unit/engine/orchestrator/test_source_iteration_quarantine_sweep.py
"""Row_union timeout sweep coverage at quarantined-row boundaries.

elspeth-c6d083d150: the main loop's quarantine branch continues the loop
before the per-row barrier sweeps, and a continuously ready quarantined
stream keeps the source non-idle (the idle pump only fires while the loop is
blocked inside ``next()``). Without a sweep at the quarantine boundary, a
pending row_union group's deadline starves until EOF and is misclassified
there. This pins the loop contract only: every row boundary — quarantined
included — invokes the row_union timeout sweep. The timeout SEMANTICS (a
stale group failing closed with row_union_timeout) are pinned by the
executor tests in tests/unit/engine/test_row_union_executor.py.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.plugin_protocols import SinkProtocol, SourceProtocol
from elspeth.contracts.types import NodeID
from elspeth.core.events import EventBusProtocol
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.orchestrator import PipelineConfig
from elspeth.engine.orchestrator.ceremony import RunCeremony
from elspeth.engine.orchestrator.quarantine_router import QuarantineRouter
from elspeth.engine.orchestrator.run_state import LoopContext
from elspeth.engine.orchestrator.source_iteration import SourceIterationDriver
from elspeth.engine.orchestrator.source_lifecycle_recorder import SourceLifecycleRecorder
from elspeth.engine.orchestrator.types import ExecutionCounters
from elspeth.engine.processor import RowProcessor
from elspeth.engine.row_union_executor import RowUnionExecutor
from elspeth.engine.spans import SpanFactory
from elspeth.testing import make_source_row, make_source_row_quarantined


@contextmanager
def _null_track_operation(**_kwargs: Any):
    yield SimpleNamespace(operation=SimpleNamespace(operation_id="source-op-1"))


def test_quarantined_row_boundaries_sweep_row_union_timeouts() -> None:
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
    row_union_executor = MagicMock(spec=RowUnionExecutor)
    row_union_executor.get_registered_names.return_value = ["variant_union"]
    row_union_executor.check_timeouts.return_value = []
    # The starvation scenario is a CONTINUOUSLY READY stream: the loop never
    # blocks in next(), so the idle pump cannot fire. Disable idle polling to
    # model that (and keep the pump's worker thread out of the call counts).
    row_union_executor.has_timeout_configured.return_value = False
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
        coalesce_executor=None,
        coalesce_node_map={},
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

    assert driver._quarantine_router.route.call_count == 2
    # One sweep per ROW BOUNDARY: the valid row's normal-path sweep plus one
    # at EACH quarantined-row boundary. Before elspeth-c6d083d150 the
    # quarantine branch continued the loop without sweeping (count == 1).
    assert row_union_executor.check_timeouts.call_count == 3
    row_union_executor.check_timeouts.assert_called_with("variant_union")
