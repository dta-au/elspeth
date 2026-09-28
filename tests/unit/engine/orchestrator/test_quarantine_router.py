# tests/unit/engine/orchestrator/test_quarantine_router.py
"""Unit tests for QuarantineRouter — the source-quarantine routing seam.

Extracted from ``SourceIterationDriver.handle_quarantine_row``
(elspeth-27d7bfc14b). These drive the collaborator in isolation: the reachable
plugin-bug branches (each refused BEFORE any durable write), the hand-off to
the processor's fenced quarantine ingest, RowCreated telemetry, and the
plugin-error length bound. The router neither moves counters nor touches the
pending-token buckets: it returns the sink-bound result for the shared
accumulator. The one-transaction audit record itself is pinned against a real
Landscape in tests/unit/core/landscape/test_quarantine_ingest.py, and
end-to-end behaviour in tests/integration/pipeline/orchestrator/test_quarantine_routing.py.

Mock discipline: the ceremony is a ``spec``-bound mock; the processor is a
recording fake of the one verb the router calls; source/ctx/config inputs are
plain ``SimpleNamespace`` fakes.
"""

from __future__ import annotations

from types import MappingProxyType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from elspeth.contracts import SourceRow
from elspeth.contracts.events import RowCreated
from elspeth.contracts.types import NodeID
from elspeth.engine.orchestrator.ceremony import RunCeremony
from elspeth.engine.orchestrator.quarantine_router import QUARANTINE_ERROR_MAX_CHARS, QuarantineRouter
from elspeth.engine.orchestrator.run_state import LoopContext
from elspeth.engine.orchestrator.types import ExecutionCounters, RouteValidationError

SOURCE_ID = NodeID("source-node")


class _RecordingProcessor:
    """Records the fenced-ingest call and returns a marker result."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = SimpleNamespace(token=SimpleNamespace(token_id="tok-1", row_id="row-1"))

    def ingest_quarantined_row(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.result


def _make_source(*, name: str = "quarantine_source", on_validation_failure: str = "quarantine") -> SimpleNamespace:
    return SimpleNamespace(name=name, _on_validation_failure=on_validation_failure)


def _make_loop_ctx(
    processor: _RecordingProcessor, *, sinks: tuple[str, ...] = ("quarantine",), validation_error_id: str | None = None
) -> LoopContext:
    ctx = SimpleNamespace(pop_pending_quarantine_validation_error_id=lambda row: validation_error_id)
    return LoopContext(
        counters=ExecutionCounters(),
        pending_tokens={name: [] for name in sinks},
        processor=processor,
        ctx=ctx,
        config=SimpleNamespace(sinks={name: object() for name in sinks}),
        agg_transform_lookup=MappingProxyType({}),
        coalesce_executor=None,
        coalesce_node_map=MappingProxyType({}),
    )


def _route(
    router: QuarantineRouter,
    loop_ctx: LoopContext,
    source_item: SourceRow,
    *,
    source: SimpleNamespace,
    edge_map: dict[tuple[NodeID, str], str] | None = None,
) -> Any:
    return router.route(
        "run-1",
        SOURCE_ID,
        source_item,
        0,
        source_item.source_row_index,
        7,
        edge_map if edge_map is not None else {(SOURCE_ID, "__quarantine__"): "edge-1"},
        loop_ctx,
        active_source=source,
    )


class TestQuarantineRouteValidation:
    """The reachable plugin-bug branches raise before any durable write."""

    def test_missing_destination_raises(self) -> None:
        processor = _RecordingProcessor()
        router = QuarantineRouter(ceremony=MagicMock(spec=RunCeremony))
        # Empty-string destination passes SourceRow.__post_init__ (not None) but
        # is falsy at the router — the "plugin forgot the destination" case.
        item = SourceRow(
            row={"bad": "data"},
            is_quarantined=True,
            quarantine_error="validation failed",
            quarantine_destination="",
            source_row_index=0,
        )
        with pytest.raises(RouteValidationError, match="missing quarantine_destination"):
            _route(router, _make_loop_ctx(processor), item, source=_make_source())
        assert processor.calls == []

    def test_invalid_destination_raises(self) -> None:
        processor = _RecordingProcessor()
        router = QuarantineRouter(ceremony=MagicMock(spec=RunCeremony))
        item = SourceRow.quarantined(
            row={"bad": "data"},
            error="validation failed",
            destination="nonexistent_sink",
            source_row_index=0,
        )
        with pytest.raises(RouteValidationError, match="invalid quarantine_destination='nonexistent_sink'"):
            _route(router, _make_loop_ctx(processor), item, source=_make_source(on_validation_failure="nonexistent_sink"))
        assert processor.calls == []

    def test_missing_quarantine_edge_raises(self) -> None:
        from elspeth.contracts.errors import OrchestrationInvariantError

        processor = _RecordingProcessor()
        router = QuarantineRouter(ceremony=MagicMock(spec=RunCeremony))
        item = SourceRow.quarantined(row={"a": 1}, error="bad", destination="quarantine", source_row_index=0)
        with pytest.raises(OrchestrationInvariantError, match="no __quarantine__"):
            _route(router, _make_loop_ctx(processor), item, source=_make_source(), edge_map={})
        assert processor.calls == []


class TestQuarantineHappyPath:
    """A valid quarantined row is handed to the fenced ingest and returned as a result."""

    def test_hands_the_row_to_the_fenced_ingest_and_returns_its_result(self) -> None:
        ceremony = MagicMock(spec=RunCeremony)
        router = QuarantineRouter(ceremony=ceremony)
        processor = _RecordingProcessor()
        loop_ctx = _make_loop_ctx(processor, validation_error_id="verr-1")
        item = SourceRow.quarantined(row={"amount": float("nan")}, error="bad value", destination="quarantine", source_row_index=3)

        result = _route(router, loop_ctx, item, source=_make_source())

        assert result is processor.result
        (call,) = processor.calls
        assert call == {
            "source_node_id": SOURCE_ID,
            "row_index": 0,
            "source_row_index": 3,
            "ingest_sequence": 7,
            # Tier-3 sanitisation happened before the durable write.
            "row": {"amount": None},
            "validation_error_id": "verr-1",
            "quarantine_sink": "quarantine",
            "quarantine_error": "bad value",
            "quarantine_edge_id": "edge-1",
        }
        # RowCreated telemetry names the ingested token.
        (event,), _ = ceremony.emit_telemetry.call_args
        assert isinstance(event, RowCreated)
        assert (event.row_id, event.token_id) == ("row-1", "tok-1")
        # The router neither moves counters nor appends to pending buckets:
        # the shared accumulator does both from the returned result.
        assert loop_ctx.pending_tokens == {"quarantine": []}
        assert loop_ctx.counters.rows_quarantined == 0
        assert loop_ctx.counters.rows_failed == 0

    def test_overlong_error_is_bounded_before_the_ingest(self) -> None:
        router = QuarantineRouter(ceremony=MagicMock(spec=RunCeremony))
        processor = _RecordingProcessor()
        long_error = "x" * (QUARANTINE_ERROR_MAX_CHARS + 5000)
        item = SourceRow.quarantined(row={"a": 1}, error=long_error, destination="quarantine", source_row_index=0)

        _route(router, _make_loop_ctx(processor), item, source=_make_source())

        # ONE bounded text feeds the node_state error, the DIVERT reason, the
        # pending-sink message and the error hash (all derived in the ingest).
        bounded = processor.calls[0]["quarantine_error"]
        assert len(bounded) <= QUARANTINE_ERROR_MAX_CHARS + 200
        assert bounded.startswith("x" * QUARANTINE_ERROR_MAX_CHARS)
        assert bounded.endswith("chars]")
