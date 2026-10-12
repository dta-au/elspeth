# tests/unit/engine/orchestrator/test_cleanup_failure_ceremony.py
"""Plugin-cleanup failures must not mask an in-flight run failure.

``cleanup_plugins`` runs in the orchestrator's ``finally`` blocks. Raising a
fresh ``RuntimeError`` there while an exception is already propagating
REPLACES that exception: a ``_RunFailedWithPartialResultError`` carrying real
partial counters degrades to the generic failure ceremony (zeroed counters)
and the caller sees a cleanup error instead of the actual run failure.

Invariants proven here:
- during exception propagation, cleanup failures are recorded (structured
  log) and the original exception continues unchanged;
- with no in-flight exception, cleanup failures still raise ``RuntimeError``
  (the success path must not swallow them);
- end-to-end through ``Orchestrator.run``: a mid-iteration source failure
  followed by a failing ``sink.close()`` surfaces the source's error and the
  failed-ceremony ``RunSummary`` keeps the partial counters.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FunctionType
from typing import Any, cast

import pytest
import structlog.testing

import elspeth.core.logging as logging_module
import elspeth.engine.orchestrator.cleanup as cleanup_module
from elspeth.contracts import Determinism, SinkProtocol, SourceProtocol
from elspeth.contracts.events import RunSummary
from elspeth.contracts.plugin_context import PluginContext
from elspeth.core.config import SourceSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.logging import configure_logging
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.engine.orchestrator.cleanup import _safe_cleanup_error_text, cleanup_plugins
from tests.fixtures.base_classes import as_sink, as_source
from tests.fixtures.factories import make_context
from tests.fixtures.plugins import CollectSink, ListSource
from tests.fixtures.stores import MockPayloadStore


class FailingCloseSink(CollectSink):
    """Sink that writes normally but raises on close()."""

    def close(self) -> None:
        raise RuntimeError("sink close failure")


SENSITIVE_CLEANUP_MESSAGE = "cleanup failed for sk-1234567890abcdef1234567890abcdef password=hunter2 https://user:pass@example.test/path"


class SensitiveFailingCloseSink(CollectSink):
    """Sink that raises a secret-bearing cleanup error."""

    def close(self) -> None:
        raise RuntimeError(SENSITIVE_CLEANUP_MESSAGE)


class MidIterationFailingSource(ListSource):
    """Source that yields its rows, then raises mid-iteration."""

    determinism = Determinism.IO_READ

    def load(self, ctx: Any) -> Any:
        yield from self.wrap_rows(self._data)
        raise ValueError("source exploded mid-iteration")


class RecordingEventBus:
    """Event bus that records every emitted event."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def subscribe(self, event_type: type, handler: Any) -> None:
        pass

    def emit(self, event: Any) -> None:
        self.events.append(event)


def _config_with_failing_close_sink() -> PipelineConfig:
    sink = FailingCloseSink("default")
    return PipelineConfig(
        sources={"primary": as_source(ListSource([], name="src"))},
        transforms=[],
        sinks={"default": as_sink(sink)},
    )


def _config_with_sensitive_failing_close_sink() -> PipelineConfig:
    sink = SensitiveFailingCloseSink("default")
    return PipelineConfig(
        sources={"primary": as_source(ListSource([], name="src"))},
        transforms=[],
        sinks={"default": as_sink(sink)},
    )


class TestCleanupDoesNotMaskPendingException:
    """Direct cleanup_plugins contract: pending exception wins."""

    def test_exception_formatting_preserves_tier1_failure(self) -> None:
        from elspeth.contracts.errors import AuditIntegrityError

        failure = AuditIntegrityError("formatter detected corruption")

        class BrokenException(Exception):
            def __str__(self) -> str:
                raise failure

        with pytest.raises(AuditIntegrityError) as raised:
            _safe_cleanup_error_text(BrokenException())
        assert raised.value is failure

    def test_unrepresentable_exception_records_an_explicit_marker(self) -> None:
        class BrokenException(Exception):
            def __str__(self) -> str:
                raise ValueError("bad formatter")

        text, digest, length = _safe_cleanup_error_text(BrokenException())
        assert text == "<unrepresentable BrokenException: ValueError>"
        assert length is None
        assert digest is None

    def test_handled_exception_does_not_suppress_cleanup_failure_when_caller_declares_no_pending_exception(
        self,
    ) -> None:
        """Ambient interpreter state is not cleanup-policy authority."""
        config = _config_with_failing_close_sink()
        ctx = PluginContext(run_id="test", config={}, landscape=None)

        try:
            raise ValueError("handled before cleanup")
        except ValueError:
            with pytest.raises(RuntimeError, match="Plugin cleanup failed"):
                cleanup_plugins(config, ctx, include_source=True, pending_exc=None)

    def test_cleanup_failure_during_exception_propagation_preserves_original(self) -> None:
        config = _config_with_failing_close_sink()
        ctx = PluginContext(run_id="test", config={}, landscape=None)
        pending_exc = ValueError("primary run failure")

        with pytest.raises(ValueError, match="primary run failure") as exc_info:
            try:
                raise pending_exc
            finally:
                cleanup_plugins(config, ctx, include_source=True, pending_exc=pending_exc)

        assert exc_info.value is pending_exc

    def test_cleanup_failure_during_exception_propagation_is_logged(self) -> None:
        config = _config_with_failing_close_sink()
        ctx = PluginContext(run_id="test", config={}, landscape=None)
        pending_exc = ValueError("primary run failure")

        with structlog.testing.capture_logs() as captured, pytest.raises(ValueError, match="primary run failure"):
            try:
                raise pending_exc
            finally:
                cleanup_plugins(config, ctx, include_source=True, pending_exc=pending_exc)

        events = [entry["event"] for entry in captured]
        assert "Plugin cleanup failed during exception propagation; original error preserved" in events
        aggregate_log = next(
            entry for entry in captured if entry["event"] == "Plugin cleanup failed during exception propagation; original error preserved"
        )
        assert aggregate_log["run_id"] == "test"

    def test_cleanup_failure_without_pending_exception_still_raises(self) -> None:
        """The success path must surface cleanup failures, not swallow them."""
        config = _config_with_failing_close_sink()
        ctx = PluginContext(run_id="test", config={}, landscape=None)

        with pytest.raises(RuntimeError, match="Plugin cleanup failed"):
            cleanup_plugins(config, ctx, include_source=True, pending_exc=None)

    def test_cleanup_failure_preserves_pending_control_flow_base_exception(self) -> None:
        """Ordinary cleanup failures cannot replace KeyboardInterrupt/SystemExit-class flow."""
        config = _config_with_failing_close_sink()
        ctx = PluginContext(run_id="test", config={}, landscape=None)
        pending_exc = KeyboardInterrupt("operator interrupted")

        with pytest.raises(KeyboardInterrupt, match="operator interrupted") as exc_info:
            try:
                raise pending_exc
            finally:
                cleanup_plugins(config, ctx, include_source=True, pending_exc=pending_exc)

        assert exc_info.value is pending_exc

    def test_cleanup_failure_public_surfaces_preserve_benign_error_text(self) -> None:
        """Benign cleanup diagnostics keep a bounded message preview for operators."""
        config = _config_with_failing_close_sink()
        ctx = PluginContext(run_id="test", config={}, landscape=None)

        with structlog.testing.capture_logs() as captured, pytest.raises(RuntimeError) as exc_info:
            cleanup_plugins(config, ctx, include_source=True, pending_exc=None)

        public_error = str(exc_info.value)
        assert "sink.close(default)" in public_error
        assert "RuntimeError" in public_error
        assert "sink close failure" in public_error
        assert "<redacted-plugin-error>" not in public_error

        hook_log = next(entry for entry in captured if entry["event"] == "Plugin cleanup hook failed")
        assert hook_log["run_id"] == "test"
        assert hook_log["error"] == "sink close failure"

    def test_cleanup_failure_public_surfaces_scrub_plugin_error_text(self) -> None:
        """Cleanup diagnostics keep location/type metadata without leaking plugin exception text."""
        config = _config_with_sensitive_failing_close_sink()
        ctx = PluginContext(run_id="test", config={}, landscape=None)

        with structlog.testing.capture_logs() as captured, pytest.raises(RuntimeError) as exc_info:
            cleanup_plugins(config, ctx, include_source=True, pending_exc=None)

        public_error = str(exc_info.value)
        captured_text = repr(captured)

        for leaked in (
            "sk-1234567890abcdef1234567890abcdef",
            "password=hunter2",
            "https://user:pass@example.test/path",
        ):
            assert leaked not in public_error
            assert leaked not in captured_text

        assert "sink.close(default)" in public_error
        assert "RuntimeError" in public_error
        assert "<redacted-secret>" in public_error

        hook_log = next(entry for entry in captured if entry["event"] == "Plugin cleanup hook failed")
        assert hook_log["run_id"] == "test"
        assert hook_log["hook"] == "sink.close"
        assert hook_log["plugin"] == "default"
        assert hook_log["error_type"] == "RuntimeError"
        assert hook_log["error"] == "<redacted-secret>"
        assert "exc_info" not in hook_log


class ChainedFailingCloseSink(CollectSink):
    """Same implementation may appear at multiple graph nodes."""

    def close(self) -> None:
        try:
            raise ValueError(SENSITIVE_CLEANUP_MESSAGE)
        except ValueError as cause:
            raise RuntimeError("sink close failure") from cause


class TestCleanupDiagnosticAttribution:
    def test_json_records_and_aggregate_distinguish_same_plugin_nodes(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = PipelineConfig(
            sources={"primary": as_source(ListSource([]))},
            transforms=[],
            sinks={name: as_sink(ChainedFailingCloseSink("same-plugin", node_id=name)) for name in ("node-a", "node-b")},
        )
        ctx = make_context(node_id="outer")
        logging_config = structlog.get_config()
        root = logging.getLogger()
        monkeypatch.setattr(root, "handlers", list(root.handlers))
        monkeypatch.setattr(root, "level", root.level)
        monkeypatch.setattr(logging_module, "_elspeth_handler_ids", set(logging_module._elspeth_handler_ids))
        monkeypatch.setattr(logging_module, "_elspeth_stream_handler", logging_module._elspeth_stream_handler)
        for logger_name in logging_module._NOISY_LOGGERS:
            logger = logging.getLogger(logger_name)
            monkeypatch.setattr(logger, "level", logger.level)
        try:
            configure_logging(json_output=True)
            with structlog.contextvars.bound_contextvars(request_id="known-positive"), pytest.raises(RuntimeError) as raised:
                cleanup_plugins(config, ctx, pending_exc=None)
            rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        finally:
            structlog.configure(**logging_config)
        hooks = [row for row in rows if row["event"] == "Plugin cleanup hook failed"]
        assert len(hooks) == 2
        assert [row["request_id"] for row in hooks] == ["known-positive"] * 2
        assert [row["node_id"] for row in hooks] == ["node-a", "node-b"]
        assert all(row["cause_types"] == ["ValueError"] for row in hooks)
        assert all(row["frames"] for row in hooks)
        assert all("elspeth/engine/orchestrator/cleanup.py:" in frame for row in hooks for frame in row["frames"])
        assert all("node_id=" + name in str(raised.value) for name in ("node-a", "node-b"))
        assert "ValueError" in str(raised.value)
        assert "sk-1234567890abcdef1234567890abcdef" not in repr(rows) + str(raised.value)
        assert "hunter2" not in repr(rows) + str(raised.value)
        assert "exception" not in hooks[0]
        assert ctx.node_id == "outer"

    def test_pending_aggregate_retains_node_and_cause(self) -> None:
        config = PipelineConfig(
            sources={"primary": as_source(ListSource([]))},
            transforms=[],
            sinks={"out": as_sink(ChainedFailingCloseSink("same-plugin", node_id="node-a"))},
        )
        primary = ValueError("primary")
        with structlog.testing.capture_logs() as rows, pytest.raises(ValueError) as raised:
            try:
                raise primary
            finally:
                cleanup_plugins(config, make_context(), pending_exc=primary)
        assert raised.value is primary
        aggregate = next(row for row in rows if row["event"].startswith("Plugin cleanup failed during"))
        assert "node_id=node-a" in aggregate["cleanup_errors"][0]
        assert "ValueError" in aggregate["cleanup_errors"][0]

    @pytest.mark.parametrize("cycle", [False, True])
    def test_causal_walk_is_bounded_and_reports_cycles(self, cycle: bool) -> None:
        failure = RuntimeError("outer")
        current: BaseException = failure
        for _ in range(20 if not cycle else 2):
            cause = ValueError(SENSITIVE_CLEANUP_MESSAGE)
            current.__cause__ = cause
            current = cause
        if cycle:
            current.__cause__ = failure

        def fail() -> None:
            raise failure

        record, summary = self._capture_failure(fail)
        assert list(record["cause_types"]) == ["ValueError"] * (2 if cycle else 7)
        assert record["cause_chain_truncated"] is True
        assert len(record["frames"]) <= 8
        assert "hunter2" not in repr(record) + summary

    @pytest.mark.parametrize("forged_path", [False, True])
    def test_untrusted_frames_do_not_emit_filenames_or_function_names(self, forged_path: bool) -> None:
        def fail() -> None:
            raise ValueError("ordinary")

        filename = cleanup_module.__file__ if forged_path else "/untrusted/password=hunter2.py"
        forged = FunctionType(fail.__code__.replace(co_filename=filename, co_name="password=hunter2", co_firstlineno=1), globals())
        record, summary = self._capture_failure(forged)
        assert record["frames_withheld"] >= 1
        assert record["frames"]
        assert all(frame.startswith("elspeth/engine/orchestrator/cleanup.py:") for frame in record["frames"])
        assert "hunter2" not in repr(record) + summary
        assert not any(frame.endswith(":2") for frame in record["frames"])

    def test_unavailable_source_withholds_frames_without_masking_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def unavailable(*args: Any, **kwargs: Any) -> Any:
            raise OSError("password=hunter2")

        def fail() -> None:
            raise ValueError("ordinary")

        cleanup_module._trusted_source_codes.cache_clear()
        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", unavailable)
            record, summary = self._capture_failure(fail)
        cleanup_module._trusted_source_codes.cache_clear()
        assert record["frames"] == ()
        assert record["node_id"] == "node-a"
        assert "ordinary" in summary
        assert "hunter2" not in repr(record) + summary

    def test_source_change_invalidates_frame_admission(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = tmp_path / "fixture.py"
        source = "def fail():\n    raise ValueError('ordinary')\n"
        path.write_text(source)
        code = compile(source, str(path), "exec", dont_inherit=True).co_consts[0]
        callback = FunctionType(code, {"ValueError": ValueError})
        monkeypatch.setattr(cleanup_module, "_PACKAGE_ROOT", tmp_path)
        record, _ = self._capture_failure(callback)
        assert record["frames"] == ("elspeth/fixture.py:2",)
        path.write_text("# This trusted file no longer defines the failing function.\n")
        record, _ = self._capture_failure(callback)
        assert record["frames"] == ()

    def test_python312_symlink_resolution_failure_withholds_coordinates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def unresolved(path: Path) -> Path:
            raise RuntimeError("Symlink loop from 'password=hunter2'")

        def fail() -> None:
            raise ValueError("ordinary")

        monkeypatch.setattr(Path, "resolve", unresolved)
        record, summary = self._capture_failure(fail)
        assert record["frames"] == ()
        assert record["node_id"] == "node-a"
        assert "ordinary" in summary
        assert "hunter2" not in repr(record) + summary

    def test_oversized_source_is_withheld(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = tmp_path / "fixture.py"
        source = "def fail():\n    raise ValueError('ordinary')\n"
        callback = FunctionType(compile(source, str(path), "exec", dont_inherit=True).co_consts[0], {"ValueError": ValueError})
        path.write_text(source + "#" * (1024 * 1024))
        monkeypatch.setattr(cleanup_module, "_PACKAGE_ROOT", tmp_path)
        record, summary = self._capture_failure(callback)
        assert record["frames"] == ()
        assert record["node_id"] == "node-a"
        assert "ordinary" in summary

    def test_frame_budget_is_shared_across_causes(self) -> None:
        def recurse(depth: int) -> None:
            if depth:
                recurse(depth - 1)
            else:
                raise ValueError("inner")

        def fail() -> None:
            try:
                recurse(20)
            except ValueError as cause:
                raise RuntimeError("outer") from cause

        record, _ = self._capture_failure(fail)
        assert len(record["frames"]) + record["frames_withheld"] == 8
        assert record["frames_truncated"] is True
        assert record["cause_types"] == ("ValueError",)

    def test_exception_chain_access_does_not_invoke_overridden_descriptors(self) -> None:
        class OverriddenCauseError(Exception):
            @property
            def __cause__(self) -> BaseException:
                raise AssertionError("custom descriptor invoked")

        def fail() -> None:
            raise OverriddenCauseError("ordinary") from ValueError("inner")

        record, summary = self._capture_failure(fail)
        assert record["cause_types"] == ("ValueError",)
        assert "ordinary" in summary

    def test_cause_name_does_not_invoke_metaclass_getter(self) -> None:
        class HostileName(type):
            def __getattribute__(cls, name: str) -> Any:
                if name == "__name__":
                    raise AssertionError("custom metaclass invoked")
                return super().__getattribute__(name)

            @property
            def __name__(cls) -> str:
                raise AssertionError("custom metaclass descriptor invoked")

        class CauseError(Exception, metaclass=HostileName):
            pass

        def fail() -> None:
            raise RuntimeError("ordinary") from CauseError("inner")

        record, summary = self._capture_failure(fail)
        assert record["cause_types"] == ("CauseError",)
        assert "ordinary" in summary

    def test_oversized_cause_class_name_uses_bounded_marker(self) -> None:
        cause_type = type("A" * 10000, (Exception,), {})

        def fail() -> None:
            raise RuntimeError("ordinary") from cause_type("inner")

        record, _ = self._capture_failure(fail)
        assert record["cause_types"] == ("<exception-class-name-too-long>",)

    def test_cause_class_name_subclass_is_not_executed(self) -> None:
        class HostileString(str):
            def __len__(self) -> int:
                raise AssertionError("custom length invoked")

        class CauseError(Exception):
            pass

        CauseError.__name__ = HostileString("CauseError")

        def fail() -> None:
            raise RuntimeError("ordinary") from CauseError("inner")

        record, _ = self._capture_failure(fail)
        assert record["cause_types"] == ("<invalid-exception-class>",)

    def test_generated_constants_never_execute_during_frame_admission(self) -> None:
        invoked: list[str] = []

        class HostileConstant:
            def __hash__(self) -> int:
                invoked.append("hash")
                raise AssertionError("hostile hash invoked")

            def __eq__(self, other: object) -> bool:
                invoked.append("eq")
                raise AssertionError("hostile equality invoked")

        def fail() -> None:
            raise ValueError("ordinary")

        forged = FunctionType(
            fail.__code__.replace(co_filename=cleanup_module.__file__, co_consts=(*fail.__code__.co_consts, HostileConstant())),
            globals(),
        )
        record, summary = self._capture_failure(forged)
        assert not invoked
        assert record["node_id"] == "node-a"
        assert "ordinary" in summary

    @pytest.mark.parametrize("field", ["co_name", "co_qualname", "co_filename", "nested_name"])
    def test_generated_code_header_never_executes_callbacks(self, field: str) -> None:
        invoked: list[str] = []

        class HostileString(str):
            def __hash__(self) -> int:
                invoked.append("hash")
                raise AssertionError("hostile hash invoked")

            def __eq__(self, other: object) -> bool:
                invoked.append("eq")
                raise AssertionError("hostile equality invoked")

        def fail() -> None:
            raise ValueError("ordinary")

        code = fail.__code__.replace(co_filename=cleanup_module.__file__)
        if field == "nested_name":
            code = code.replace(co_consts=(*code.co_consts, code.replace(co_name=HostileString("nested"))))
        elif field == "co_filename":
            code = code.replace(co_filename=HostileString(cleanup_module.__file__))
        elif field == "co_name":
            code = code.replace(co_name=HostileString("generated"))
        else:
            code = code.replace(co_qualname=HostileString("generated"))
        generated = FunctionType(code, globals())

        def invoke_generated_without_tracing() -> None:
            # Coverage indexes frame filenames before application code runs,
            # invoking a hostile str subclass itself. Only the synthetic frame
            # is untraced; restore the tracer before cleanup handles its error.
            tracer = sys.gettrace()
            sys.settrace(None)
            try:
                generated()
            finally:
                sys.settrace(tracer)

        original_tracer = sys.gettrace()
        record, summary = self._capture_failure(invoke_generated_without_tracing)
        assert sys.gettrace() is original_tracer
        assert not invoked
        assert record["node_id"] == "node-a"
        assert "ordinary" in summary

    @staticmethod
    def _capture_failure(callback: Callable[[], None]) -> tuple[dict[str, Any], str]:
        class CallbackSink(CollectSink):
            def close(self) -> None:
                callback()

        config = PipelineConfig(
            sources={"primary": as_source(ListSource([]))},
            transforms=[],
            sinks={"out": as_sink(CallbackSink("same-plugin", node_id="node-a"))},
        )
        with structlog.testing.capture_logs() as rows, pytest.raises(RuntimeError) as raised:
            cleanup_plugins(config, make_context(), pending_exc=None)
        return next(row for row in rows if row["event"] == "Plugin cleanup hook failed"), str(raised.value)


class TestPartialResultCeremonySurvivesCleanupFailure:
    """End-to-end: run failure + cleanup failure keeps the partial-result ceremony."""

    def _build_pipeline(self) -> tuple[MidIterationFailingSource, FailingCloseSink, PipelineConfig, ExecutionGraph]:
        source = MidIterationFailingSource([{"value": 1}], name="mid_fail_source", on_success="default")
        sink = FailingCloseSink("default")

        source_settings = SourceSettings(plugin=source.name, on_success="default", options={})
        graph = ExecutionGraph.from_plugin_instances(
            sources={"primary": cast(SourceProtocol, source)},
            source_settings_map={"primary": source_settings},
            transforms=[],
            sinks=cast("dict[str, SinkProtocol]", {"default": sink}),
            aggregations={},
            gates=[],
        )
        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[],
            sinks={"default": as_sink(sink)},
        )
        return source, sink, config, graph

    def test_original_error_and_partial_counters_survive_failing_cleanup(self) -> None:
        db = LandscapeDB.in_memory()
        event_bus = RecordingEventBus()
        _source, _sink, config, graph = self._build_pipeline()

        orchestrator = Orchestrator(db, event_bus=event_bus)

        # The source's ValueError must propagate — NOT the RuntimeError from
        # the sink's failing close() in the cleanup finally block.
        with pytest.raises(ValueError, match="source exploded mid-iteration"):
            orchestrator.run(
                config,
                graph=graph,
                payload_store=MockPayloadStore(),
                shutdown_event=threading.Event(),
            )

        summaries = [event for event in event_bus.events if isinstance(event, RunSummary)]
        assert len(summaries) == 1
        summary = summaries[0]
        assert summary.status.value == "failed"
        # Partial-result ceremony: the row ingested before the failure is
        # reported, not the generic ceremony's zeroed counters.
        assert summary.total_rows == 1

    def test_successful_fresh_run_inside_handled_exception_surfaces_cleanup_failure(self) -> None:
        """The fresh-run finally must not inherit a caller's already-handled exception."""
        db = LandscapeDB.in_memory()
        event_bus = RecordingEventBus()
        source = ListSource([{"value": 1}], name="source", on_success="default")
        sink = FailingCloseSink("default")
        source_settings = SourceSettings(plugin=source.name, on_success="default", options={})
        graph = ExecutionGraph.from_plugin_instances(
            sources={"primary": cast(SourceProtocol, source)},
            source_settings_map={"primary": source_settings},
            transforms=[],
            sinks=cast("dict[str, SinkProtocol]", {"default": sink}),
            aggregations={},
            gates=[],
        )
        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[],
            sinks={"default": as_sink(sink)},
        )
        orchestrator = Orchestrator(db, event_bus=event_bus)

        try:
            raise LookupError("handled by the caller")
        except LookupError:
            with pytest.raises(RuntimeError, match="Plugin cleanup failed"):
                orchestrator.run(
                    config,
                    graph=graph,
                    payload_store=MockPayloadStore(),
                    shutdown_event=threading.Event(),
                )

    def test_final_heartbeat_failure_reaches_caller_after_seat_release(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.contracts.errors import AuditIntegrityError
        from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository

        failure = AuditIntegrityError("final heartbeat corruption")
        releases: list[str] = []
        original_release = RunCoordinationRepository.release_seat

        def fail_heartbeat(self: RunCoordinationRepository, **kwargs: Any) -> None:
            raise failure

        def release(self: RunCoordinationRepository, **kwargs: Any) -> None:
            original_release(self, **kwargs)
            releases.append(kwargs["token"].run_id)

        monkeypatch.setattr(RunCoordinationRepository, "worker_heartbeat", fail_heartbeat)
        monkeypatch.setattr(RunCoordinationRepository, "release_seat", release)
        source = ListSource([{"value": 1}], name="source", on_success="default")
        sink = CollectSink("default")
        graph = ExecutionGraph.from_plugin_instances(
            sources={"primary": as_source(source)},
            source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="default", options={})},
            transforms=[],
            sinks={"default": as_sink(sink)},
            aggregations={},
            gates=[],
        )
        config = PipelineConfig(sources={"primary": as_source(source)}, transforms=[], sinks={"default": as_sink(sink)})
        event_bus = RecordingEventBus()
        orchestrator = Orchestrator(LandscapeDB.in_memory(), event_bus=event_bus)

        with pytest.raises(AuditIntegrityError) as raised:
            orchestrator.run(config, graph=graph, payload_store=MockPayloadStore(), shutdown_event=threading.Event())

        assert raised.value is failure
        assert releases
        summaries = [event for event in event_bus.events if isinstance(event, RunSummary)]
        assert all(summary.status.value != "completed" for summary in summaries)
