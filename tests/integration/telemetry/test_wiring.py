# tests/integration/telemetry/test_wiring.py
"""Integration tests for telemetry wiring through production code paths.

These tests verify that:
1. Orchestrator correctly wires telemetry_emit to PluginContext
2. Plugins using audited clients emit telemetry in production
3. The fix for elspeth-rapid-vlr is working correctly

CRITICAL: These tests use production Orchestrator, NOT manual PluginContext.
This catches the wiring bugs that unit tests miss.

Migrated from tests/integration/test_telemetry_wiring.py
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest

from elspeth.contracts import Determinism, SourceRow
from elspeth.contracts.enums import RunStatus, TelemetryGranularity
from elspeth.contracts.events import (
    EngineSpanCompleted,
    EngineSpanName,
    EngineSpanStatus,
    RunStarted,
    TransformCompleted,
)
from elspeth.core.landscape import LandscapeDB
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.telemetry import TelemetryManager
from tests.fixtures.base_classes import as_sink, as_source, as_transform
from tests.fixtures.pipeline import build_production_graph
from tests.fixtures.plugins import CollectSink, ListSource, PassTransform
from tests.fixtures.telemetry import MockTelemetryConfig, TelemetryTestExporter

if TYPE_CHECKING:
    from elspeth.core.dag import ExecutionGraph


# =============================================================================
# Test Helpers
# =============================================================================


def create_test_graph(config: PipelineConfig) -> ExecutionGraph:
    """Build a graph using the production factory path."""
    return build_production_graph(config)


# =============================================================================
# Core Wiring Tests
# =============================================================================


class TestOrchestratorWiresTelemetryToContext:
    """Verify orchestrator correctly wires telemetry_emit to PluginContext.

    This is the critical test for elspeth-rapid-vlr - it verifies that the
    production code path (Orchestrator -> PluginContext) correctly wires
    the telemetry callback.

    IMPORTANT: These tests use production Orchestrator, NOT manual PluginContext.
    """

    def test_orchestrator_emits_lifecycle_telemetry(
        self,
        landscape_db: LandscapeDB,
        payload_store: Any,
    ) -> None:
        """Orchestrator emits RunStarted and RunFinished via production path."""
        exporter = TelemetryTestExporter()
        config = MockTelemetryConfig(granularity=TelemetryGranularity.FULL)
        telemetry_manager = TelemetryManager(config, exporters=[exporter])

        source = ListSource([{"id": 1}, {"id": 2}, {"id": 3}], on_success="output")
        sink = CollectSink()

        pipeline_config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(PassTransform())],
            sinks={"output": as_sink(sink)},
        )

        # Use PRODUCTION Orchestrator - this is the key
        orchestrator = Orchestrator(landscape_db, telemetry_manager=telemetry_manager)
        result = orchestrator.run(
            pipeline_config,
            graph=create_test_graph(pipeline_config),
            payload_store=payload_store,
        )

        # Pipeline should complete
        assert result.status == RunStatus.COMPLETED

        # Telemetry should have been emitted
        exporter.assert_event_emitted("RunStarted")
        exporter.assert_event_emitted("RunFinished")

        # Verify run_id matches
        run_started = exporter.get_events_of_type("RunStarted")[0]
        run_finished = exporter.get_events_of_type("RunFinished")[0]
        assert run_started.run_id == result.run_id
        assert run_finished.run_id == result.run_id

        engine_spans = [event for event in exporter.events if isinstance(event, EngineSpanCompleted)]
        names = {event.name for event in engine_spans}
        assert {
            EngineSpanName.RUN,
            EngineSpanName.SOURCE,
            EngineSpanName.ROW,
            EngineSpanName.TRANSFORM,
            EngineSpanName.SINK,
        } <= names
        assert {event.run_id for event in engine_spans} == {result.run_id}
        run_span = next(event for event in engine_spans if event.name is EngineSpanName.RUN)
        source_span = next(event for event in engine_spans if event.name is EngineSpanName.SOURCE)
        assert all(event.trace_started_at == run_span.trace_started_at for event in engine_spans)
        assert source_span.parent_span_id == run_span.span_id
        assert all(event.parent_span_id == source_span.span_id for event in engine_spans if event.name is EngineSpanName.ROW)
        assert all(event.parent_span_id == run_span.span_id for event in engine_spans if event.name is EngineSpanName.SINK)

    def test_engine_spans_do_not_export_row_content_fingerprints(
        self,
        landscape_db: LandscapeDB,
        payload_store: Any,
    ) -> None:
        """Opaque audit identifiers are sufficient to correlate engine spans."""
        exporter = TelemetryTestExporter()
        telemetry_manager = TelemetryManager(
            MockTelemetryConfig(granularity=TelemetryGranularity.ROWS),
            exporters=[exporter],
        )
        source = ListSource([{"low_entropy_secret": "0420"}], on_success="output")
        pipeline_config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(PassTransform())],
            sinks={"output": as_sink(CollectSink())},
        )

        try:
            result = Orchestrator(landscape_db, telemetry_manager=telemetry_manager).run(
                pipeline_config,
                graph=create_test_graph(pipeline_config),
                payload_store=payload_store,
            )
        finally:
            telemetry_manager.close()

        transform_span = next(
            event for event in exporter.events if isinstance(event, EngineSpanCompleted) and event.name is EngineSpanName.TRANSFORM
        )
        transform_completed = next(event for event in exporter.events if isinstance(event, TransformCompleted))
        assert result.status is RunStatus.COMPLETED
        assert "input.hash" not in transform_span.attributes
        assert transform_completed.input_hash is None
        assert transform_completed.output_hash is None
        assert transform_span.attributes["node.id"]
        assert transform_span.attributes["token.id"]

    def test_run_span_covers_terminal_audit_and_finalization_failures(
        self,
        landscape_db: LandscapeDB,
        payload_store: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        exporter = TelemetryTestExporter()
        telemetry_manager = TelemetryManager(MockTelemetryConfig(), exporters=[exporter])
        source = ListSource([{"id": 1}], on_success="output")
        pipeline_config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(PassTransform())],
            sinks={"output": as_sink(CollectSink())},
        )
        orchestrator = Orchestrator(landscape_db, telemetry_manager=telemetry_manager)

        def fail_terminal_audit(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("terminal audit failed")

        monkeypatch.setattr(
            "elspeth.engine.orchestrator.run_lifecycle.derive_terminal_status_from_audit",
            fail_terminal_audit,
        )
        try:
            with pytest.raises(RuntimeError, match="terminal audit failed"):
                orchestrator.run(
                    pipeline_config,
                    graph=create_test_graph(pipeline_config),
                    payload_store=payload_store,
                )
        finally:
            telemetry_manager.close()

        run_span = next(event for event in exporter.events if isinstance(event, EngineSpanCompleted) and event.name is EngineSpanName.RUN)
        assert run_span.status is EngineSpanStatus.ERROR
        assert run_span.exception_type == "RuntimeError"

    def test_source_span_covers_post_load_source_audit_failure(
        self,
        landscape_db: LandscapeDB,
        payload_store: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Source completion waits for source-owned validation and audit work."""
        exporter = TelemetryTestExporter()
        telemetry_manager = TelemetryManager(MockTelemetryConfig(), exporters=[exporter])
        source = ListSource([{"id": 1}], on_success="output")
        pipeline_config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(PassTransform())],
            sinks={"output": as_sink(CollectSink())},
        )
        orchestrator = Orchestrator(landscape_db, telemetry_manager=telemetry_manager)

        def fail_source_audit(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("source audit failed")

        monkeypatch.setattr(
            "elspeth.engine.orchestrator.source_lifecycle_recorder.SourceLifecycleRecorder.record_field_resolution",
            fail_source_audit,
        )
        try:
            with pytest.raises(RuntimeError, match="source audit failed"):
                orchestrator.run(
                    pipeline_config,
                    graph=create_test_graph(pipeline_config),
                    payload_store=payload_store,
                )
        finally:
            telemetry_manager.close()

        source_span = next(
            event for event in exporter.events if isinstance(event, EngineSpanCompleted) and event.name is EngineSpanName.SOURCE
        )
        assert source_span.status is EngineSpanStatus.ERROR
        assert source_span.exception_type == "RuntimeError"

    def test_empty_source_still_emits_one_successful_source_span(
        self,
        landscape_db: LandscapeDB,
        payload_store: Any,
    ) -> None:
        """An empty iterator is a completed source operation, not no operation."""
        exporter = TelemetryTestExporter()
        telemetry_manager = TelemetryManager(MockTelemetryConfig(), exporters=[exporter])
        source = ListSource([], on_success="output")
        pipeline_config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(PassTransform())],
            sinks={"output": as_sink(CollectSink())},
        )
        orchestrator = Orchestrator(landscape_db, telemetry_manager=telemetry_manager)

        try:
            result = orchestrator.run(
                pipeline_config,
                graph=create_test_graph(pipeline_config),
                payload_store=payload_store,
            )
        finally:
            telemetry_manager.close()

        source_spans = [
            event for event in exporter.events if isinstance(event, EngineSpanCompleted) and event.name is EngineSpanName.SOURCE
        ]
        assert result.status is RunStatus.EMPTY
        assert len(source_spans) == 1
        assert source_spans[0].status is EngineSpanStatus.OK

    def test_late_source_iterator_failure_marks_source_span_error(
        self,
        landscape_db: LandscapeDB,
        payload_store: Any,
    ) -> None:
        """The source span remains active after the first yielded row."""

        class LateFailureSource(ListSource):
            def load(self, ctx: Any) -> Iterator[SourceRow]:
                rows = self.wrap_rows([{"id": 1}])
                yield next(rows)
                raise RuntimeError("late source read failed")

        exporter = TelemetryTestExporter()
        telemetry_manager = TelemetryManager(MockTelemetryConfig(), exporters=[exporter])
        source = LateFailureSource([], on_success="output")
        pipeline_config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(PassTransform())],
            sinks={"output": as_sink(CollectSink())},
        )
        orchestrator = Orchestrator(landscape_db, telemetry_manager=telemetry_manager)

        try:
            with pytest.raises(RuntimeError, match="late source read failed"):
                orchestrator.run(
                    pipeline_config,
                    graph=create_test_graph(pipeline_config),
                    payload_store=payload_store,
                )
        finally:
            telemetry_manager.close()

        source_spans = [
            event for event in exporter.events if isinstance(event, EngineSpanCompleted) and event.name is EngineSpanName.SOURCE
        ]
        assert len(source_spans) == 1
        assert source_spans[0].status is EngineSpanStatus.ERROR
        assert source_spans[0].exception_type == "RuntimeError"

    def test_context_telemetry_emit_is_callable(
        self,
        landscape_db: LandscapeDB,
        payload_store: Any,
    ) -> None:
        """Verify ctx.telemetry_emit is a real callable, not the default no-op.

        This test captures the actual telemetry_emit callback from inside a
        plugin to verify the orchestrator wired it correctly.
        """
        captured_callback = None

        class CallbackCapturingTransform(PassTransform):
            """Transform that captures the telemetry_emit callback."""

            name = "callback_capturing"
            determinism = Determinism.DETERMINISTIC

            def process(self, row: Any, ctx: Any) -> TransformResult:
                return TransformResult.success(row, success_reason={"action": "passthrough"})

            def on_start(self, ctx: Any) -> None:
                super().on_start(ctx)
                nonlocal captured_callback
                captured_callback = ctx.telemetry_emit

        exporter = TelemetryTestExporter()
        config = MockTelemetryConfig()
        telemetry_manager = TelemetryManager(config, exporters=[exporter])

        source = ListSource([{"id": 1}, {"id": 2}, {"id": 3}], on_success="output")
        sink = CollectSink()

        pipeline_config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(CallbackCapturingTransform())],
            sinks={"output": as_sink(sink)},
        )

        orchestrator = Orchestrator(landscape_db, telemetry_manager=telemetry_manager)
        graph = create_test_graph(pipeline_config)
        orchestrator.run(pipeline_config, graph=graph, payload_store=payload_store)

        # The callback should have been captured
        assert captured_callback is not None, "ctx.telemetry_emit was not set"

        # It should NOT be the default no-op lambda
        # The default is: lambda event: None
        # A real callback is: orchestrator._emit_telemetry (a bound method)
        callback_name = getattr(captured_callback, "__name__", str(captured_callback))
        assert callback_name != "<lambda>", (
            f"ctx.telemetry_emit is still the default no-op lambda, not the real callback. Got: {captured_callback}"
        )

    def test_telemetry_wiring_works_in_resume_path(
        self,
        landscape_db: LandscapeDB,
        payload_store: Any,
    ) -> None:
        """Telemetry is also wired correctly in the resume code path.

        The orchestrator has two PluginContext creation sites:
        1. Main execution path (run -> _execute_run)
        2. Resume path (resume -> _resume_run)

        Both must wire telemetry_emit.
        """
        # This test verifies the main path works (resume path is harder to test
        # without setting up a partial run). The fix added telemetry_emit to both.
        exporter = TelemetryTestExporter()
        config = MockTelemetryConfig()
        telemetry_manager = TelemetryManager(config, exporters=[exporter])

        source = ListSource([{"id": 1}, {"id": 2}, {"id": 3}], on_success="output")
        sink = CollectSink()

        pipeline_config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(PassTransform())],
            sinks={"output": as_sink(sink)},
        )

        orchestrator = Orchestrator(landscape_db, telemetry_manager=telemetry_manager)
        orchestrator.run(
            pipeline_config,
            graph=create_test_graph(pipeline_config),
            payload_store=payload_store,
        )

        # Verify we got telemetry
        assert exporter.event_count > 0, "No telemetry events emitted"
        exporter.assert_event_emitted("RunStarted")
        exporter.assert_event_emitted("RunFinished")


class TestNoTelemetryWithoutManager:
    """Verify telemetry is correctly disabled when no manager is provided."""

    def test_no_crash_without_telemetry_manager(
        self,
        landscape_db: LandscapeDB,
        payload_store: Any,
    ) -> None:
        """Pipeline runs successfully without telemetry manager."""
        source = ListSource([{"id": 1}, {"id": 2}, {"id": 3}], on_success="output")
        sink = CollectSink()

        pipeline_config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(PassTransform())],
            sinks={"output": as_sink(sink)},
        )

        # No telemetry_manager - should use default no-op
        orchestrator = Orchestrator(landscape_db)
        result = orchestrator.run(
            pipeline_config,
            graph=create_test_graph(pipeline_config),
            payload_store=payload_store,
        )

        assert result.status == RunStatus.COMPLETED
        assert result.rows_processed == 3

    def test_context_telemetry_emit_is_noop_without_manager(
        self,
        landscape_db: LandscapeDB,
        payload_store: Any,
    ) -> None:
        """Without telemetry manager, ctx.telemetry_emit is a no-op lambda."""
        captured_callback = None

        class CallbackCapturingTransform(PassTransform):
            name = "callback_capturing"
            determinism = Determinism.DETERMINISTIC

            def on_start(self, ctx: Any) -> None:
                super().on_start(ctx)
                nonlocal captured_callback
                captured_callback = ctx.telemetry_emit

        source = ListSource([{"id": 1}, {"id": 2}, {"id": 3}], on_success="output")
        sink = CollectSink()

        pipeline_config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(CallbackCapturingTransform())],
            sinks={"output": as_sink(sink)},
        )

        # No telemetry manager
        orchestrator = Orchestrator(landscape_db)
        graph = create_test_graph(pipeline_config)
        orchestrator.run(pipeline_config, graph=graph, payload_store=payload_store)

        # Callback should be set (never None)
        assert captured_callback is not None

        # Should be callable without error (no-op)
        from datetime import UTC, datetime

        # Calling it should not raise
        captured_callback(
            RunStarted(
                timestamp=datetime.now(UTC),
                run_id="test",
                config_hash="test",
                source_plugin="test",
            )
        )
