"""Offline actual SDK lifecycle controls for the acceptance metric emitter."""

from __future__ import annotations

import threading

import pytest
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult

from elspeth.web._aws_ecs_acceptance.operator_telemetry import AWSOperatorMetricEmitter
from elspeth.web.config import WebSettings
from elspeth.web.operator_telemetry import OwnedTestOperatorTelemetryFactories, bootstrap_operator_telemetry
from elspeth.web.operator_telemetry_custody import OperatorTelemetryCleanupOwner, TelemetryCompletionWitness, TelemetryCustodyUnresolved
from elspeth.web.operator_telemetry_installation import OwnedTestTelemetryInstallation
from elspeth.web.process_watchdog import ProcessWatchdogFailure
from elspeth.web.process_watchdog_codec import RecoveryReason
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog


def settings() -> WebSettings:
    return WebSettings(
        composer_timeout_seconds=85,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )


def owner() -> OperatorTelemetryCleanupOwner:
    return OperatorTelemetryCleanupOwner(installation=OwnedTestTelemetryInstallation())


def test_actual_reader_join_precedes_watchdog_complete_and_repeated_close_is_once() -> None:
    custody = owner()

    class Watching(OwnedTestProcessWatchdog):
        async def complete(self, witness) -> None:
            assert custody.witness is not None
            assert custody.installation.record.state == "closed"
            super_complete = super().complete
            await super_complete(witness)

    watchdog = Watching(threading.Event())
    emitter = AWSOperatorMetricEmitter(settings(), cleanup_owner_factory=lambda: custody, process_watchdog_factory=lambda _event: watchdog)
    assert emitter.emit_web_metric(1, acceptance_namespace="offline-test") is True
    emitter.close()
    witness = custody.witness
    emitter.close()
    assert custody.witness is witness
    assert watchdog.completed
    assert watchdog.reasons == [RecoveryReason.NORMAL_SHUTDOWN]
    with pytest.raises(ProcessWatchdogFailure):
        emitter.emit_web_metric(2, acceptance_namespace="offline-test")


def test_returned_runtime_then_constructor_fault_retains_original_and_failed_startup_arm() -> None:
    custody = owner()
    watchdog = OwnedTestProcessWatchdog(threading.Event())
    original = KeyboardInterrupt()

    def failing(settings, *, cleanup_owner):
        bootstrap_operator_telemetry(settings, cleanup_owner=cleanup_owner)
        raise original

    with pytest.raises(KeyboardInterrupt) as observed:
        AWSOperatorMetricEmitter(
            settings(), runtime_factory=failing, cleanup_owner_factory=lambda: custody, process_watchdog_factory=lambda _event: watchdog
        )
    assert observed.value is original
    assert custody.witness is not None
    assert watchdog.reasons == [RecoveryReason.FAILED_STARTUP]
    assert not watchdog.completed


def test_constructor_and_cleanup_failures_keep_both_original_objects() -> None:
    custody = owner()
    watchdog = OwnedTestProcessWatchdog(threading.Event())
    primary = ValueError()
    secondary = KeyboardInterrupt()

    class FailingOwner(OperatorTelemetryCleanupOwner):
        def shutdown_sync(self):
            raise secondary

    failing_owner = FailingOwner(installation=OwnedTestTelemetryInstallation())

    def failing(settings, *, cleanup_owner):
        assert cleanup_owner is failing_owner
        raise primary

    with pytest.raises(BaseExceptionGroup) as observed:
        AWSOperatorMetricEmitter(
            settings(),
            runtime_factory=failing,
            cleanup_owner_factory=lambda: failing_owner,
            process_watchdog_factory=lambda _event: watchdog,
        )
    assert observed.value.exceptions == (primary, secondary)
    assert not watchdog.completed
    assert custody.witness is None


def test_foreign_runtime_owner_is_refused_without_closing_foreign_provider() -> None:
    custody = owner()
    foreign = owner()
    runtime = bootstrap_operator_telemetry(settings(), cleanup_owner=foreign)
    watchdog = OwnedTestProcessWatchdog(threading.Event())

    def replacing(settings, *, cleanup_owner):
        assert cleanup_owner is custody
        return runtime

    try:
        with pytest.raises(TelemetryCustodyUnresolved):
            AWSOperatorMetricEmitter(
                settings(),
                runtime_factory=replacing,
                cleanup_owner_factory=lambda: custody,
                process_watchdog_factory=lambda _event: watchdog,
            )
        assert foreign.witness is None
        assert not watchdog.completed
    finally:
        foreign.shutdown_sync()


def test_shutdown_fault_retained_no_complete_and_no_retry_of_physical_cleanup() -> None:
    original = KeyboardInterrupt()

    class FailingOwner(OperatorTelemetryCleanupOwner):
        calls = 0

        def shutdown_sync(self):
            self.calls += 1
            super().shutdown_sync()
            raise original

    custody = FailingOwner(installation=OwnedTestTelemetryInstallation())
    watchdog = OwnedTestProcessWatchdog(threading.Event())
    emitter = AWSOperatorMetricEmitter(settings(), cleanup_owner_factory=lambda: custody, process_watchdog_factory=lambda _event: watchdog)
    for _attempt in range(2):
        with pytest.raises(KeyboardInterrupt) as observed:
            emitter.close()
        assert observed.value is original
    assert custody.calls == 1
    assert not watchdog.completed


def test_foreign_pid_refuses_close_before_watchdog_and_sdk_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    custody = owner()
    watchdog = OwnedTestProcessWatchdog(threading.Event())
    emitter = AWSOperatorMetricEmitter(settings(), cleanup_owner_factory=lambda: custody, process_watchdog_factory=lambda _event: watchdog)
    original_pid = custody.creator_pid
    monkeypatch.setattr(custody, "creator_pid", original_pid + 1)
    with pytest.raises(TelemetryCustodyUnresolved):
        emitter.close()
    assert watchdog.reasons == []
    assert custody.witness is None
    monkeypatch.setattr(custody, "creator_pid", original_pid)
    emitter.close()


def test_actual_periodic_thread_is_joined_before_completion() -> None:
    custody = owner()
    watchdog = OwnedTestProcessWatchdog(threading.Event())

    class Exporter(MetricExporter):
        shutdown_calls = 0

        def export(self, metrics_data, timeout_millis=10000, **kwargs):
            return MetricExportResult.SUCCESS

        def force_flush(self, timeout_millis=10000):
            return True

        def shutdown(self, timeout_millis=30000, **kwargs):
            self.shutdown_calls += 1

    exporter = Exporter()
    factories = OwnedTestOperatorTelemetryFactories(exporter_factory=lambda **_kwargs: exporter)

    def runtime_factory(settings, *, cleanup_owner):
        return bootstrap_operator_telemetry(settings, cleanup_owner=cleanup_owner, factories=factories)

    aws_settings = settings().model_copy(
        update={
            "deployment_target": "aws-ecs",
            "operator_telemetry": "aws-otlp",
            "operator_telemetry_service_name": "offline",
            "operator_telemetry_environment": "test",
            "operator_telemetry_release": "test",
            "operator_telemetry_ecs_cluster": "test",
            "operator_telemetry_ecs_service": "test",
            "operator_telemetry_task_definition_family": "test",
            "operator_telemetry_task_definition_revision": "1",
        }
    )
    emitter = AWSOperatorMetricEmitter(
        aws_settings,
        runtime_factory=runtime_factory,
        cleanup_owner_factory=lambda: custody,
        process_watchdog_factory=lambda _event: watchdog,
    )
    periodic = custody.readers[1]
    thread = periodic._daemon_thread
    assert thread.is_alive()
    emitter.close()
    assert not thread.is_alive()
    assert exporter.shutdown_calls == 1
    assert watchdog.completed


def test_replaced_completion_witness_never_disarms_watchdog() -> None:
    class ReplacingOwner(OperatorTelemetryCleanupOwner):
        def shutdown_sync(self):
            actual = super().shutdown_sync()
            return TelemetryCompletionWitness(self, actual.creator_pid)

    custody = ReplacingOwner(installation=OwnedTestTelemetryInstallation())
    watchdog = OwnedTestProcessWatchdog(threading.Event())
    emitter = AWSOperatorMetricEmitter(settings(), cleanup_owner_factory=lambda: custody, process_watchdog_factory=lambda _event: watchdog)
    with pytest.raises(TelemetryCustodyUnresolved):
        emitter.close()
    assert custody.witness is not None
    assert not watchdog.completed
