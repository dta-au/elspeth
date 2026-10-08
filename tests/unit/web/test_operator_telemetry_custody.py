"""Offline controls for actual SDK custody and isolated installation identity."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from opentelemetry.sdk.metrics import _internal as sdk_metrics
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, MetricsData
from opentelemetry.sdk.resources import Resource

from elspeth.web.operator_telemetry_custody import (
    OperatorTelemetryCleanupOwner,
    OwnedMetricExporter,
    OwnedProviderFactory,
    RawMetricExporterCustodian,
    TelemetryCustodyUnresolved,
)
from elspeth.web.operator_telemetry_installation import (
    InstallationStage,
    OwnedTestTelemetryInstallation,
)


class RecordingExporter(MetricExporter):
    def __init__(self) -> None:
        super().__init__()
        self.shutdown_timeouts: list[float] = []
        self.shutdown_error: BaseException | None = None
        self.exports: list[MetricsData] = []

    def export(self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: object) -> MetricExportResult:
        self.exports.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        assert not kwargs
        self.shutdown_timeouts.append(timeout_millis)
        if self.shutdown_error is not None:
            raise self.shutdown_error


def owner() -> OperatorTelemetryCleanupOwner:
    return OperatorTelemetryCleanupOwner(installation=OwnedTestTelemetryInstallation())


def test_actual_provider_has_no_sdk_atexit_and_removes_only_owned_registry_member(monkeypatch: pytest.MonkeyPatch) -> None:
    registrations: list[object] = []
    monkeypatch.setattr(sdk_metrics, "register", registrations.append)
    custody = owner()
    installation = custody.installation
    reader = custody.acquire_prometheus(installation.registry)
    foreign = custody.acquire_prometheus(installation.registry)

    # Both are owned and must be removed; an independent collector survives.
    class Collector:
        def collect(self) -> tuple[object, ...]:
            return ()

    unrelated = Collector()
    installation.registry.register(unrelated)
    provider = custody.acquire_provider(OwnedProviderFactory(), resource=Resource({}), views=())
    assert provider.false_forwarded is True
    assert provider._atexit_handler is None
    assert registrations == []
    assert reader.registered() and foreign.registered()
    witness = custody.shutdown_sync()
    assert witness.owner is custody
    assert custody.shutdown_sync() is witness
    assert not reader.registered() and not foreign.registered()
    assert unrelated in installation.registry._collector_to_names
    installation.registry.unregister(unrelated)


@pytest.mark.parametrize("argument", [True, None])
def test_provider_false_argument_is_required(argument: object) -> None:
    custody = owner()
    custody.acquire_prometheus(custody.installation.registry)

    class IgnoringFactory(OwnedProviderFactory):
        def construct_into(self, provider, *, resource, views, shutdown_on_exit) -> None:
            assert shutdown_on_exit is False
            provider.initialize_sdk(resource=resource, views=views, shutdown_on_exit=argument)

    with pytest.raises(TelemetryCustodyUnresolved):
        custody.acquire_provider(IgnoringFactory(), resource=Resource({}), views=())
    with pytest.raises((TelemetryCustodyUnresolved, BaseExceptionGroup)):
        custody.shutdown_sync()
    assert custody.witness is None


def test_ignoring_nominal_factory_refuses_witness_but_removes_collector() -> None:
    custody = owner()
    reader = custody.acquire_prometheus(custody.installation.registry)

    class IgnoringFactory(OwnedProviderFactory):
        def construct_into(self, provider, *, resource, views, shutdown_on_exit) -> None:
            assert shutdown_on_exit is False

    with pytest.raises(TelemetryCustodyUnresolved):
        custody.acquire_provider(IgnoringFactory(), resource=Resource({}), views=())
    with pytest.raises((TelemetryCustodyUnresolved, BaseExceptionGroup)):
        custody.shutdown_sync()
    assert not reader.registered()
    assert custody.witness is None


@pytest.mark.parametrize("mutation", ["missing", "handler"])
def test_no_handler_proof_mutations_are_red(mutation: str) -> None:
    custody = owner()
    custody.acquire_prometheus(custody.installation.registry)
    provider = custody.acquire_provider(OwnedProviderFactory(), resource=Resource({}), views=())
    if mutation == "missing":
        del provider._atexit_handler
    else:
        provider._atexit_handler = object()
    with pytest.raises((TelemetryCustodyUnresolved, BaseExceptionGroup)):
        custody.shutdown_sync()
    assert custody.witness is None
    assert all(not reader.registered() for reader in custody.readers)


def test_periodic_sdk_timeout_keyword_is_translated_and_raw_exporter_closed_once() -> None:
    custody = owner()
    raw = RecordingExporter()
    adapter = OwnedMetricExporter(custody.retain_exporter(raw))
    reader = custody.acquire_periodic(adapter, interval_millis=60_000, timeout_millis=5_000)
    provider = custody.acquire_provider(OwnedProviderFactory(), resource=Resource({}), views=())
    provider.get_meter("custody").create_counter("custody.final").add(7)
    custody.shutdown_sync()
    assert len(raw.shutdown_timeouts) == 1
    assert 0 <= raw.shutdown_timeouts[0] <= 5_000
    assert reader._daemon_thread is not None and not reader._daemon_thread.is_alive()
    assert raw.exports


@pytest.mark.parametrize("arguments", [{"timeout": -1}, {"timeout": float("inf")}, {"timeout": 1, "timeout_millis": 2}])
def test_timeout_adapter_rejects_invalid_or_ambiguous_abi(arguments: dict[str, float]) -> None:
    raw = RecordingExporter()
    adapter = OwnedMetricExporter(RawMetricExporterCustodian(raw))
    with pytest.raises(TelemetryCustodyUnresolved):
        adapter.shutdown(**arguments)
    assert raw.shutdown_timeouts == []


def test_raw_shutdown_original_is_retained_and_never_reinvoked() -> None:
    raw = RecordingExporter()
    original = KeyboardInterrupt()
    raw.shutdown_error = original
    adapter = OwnedMetricExporter(RawMetricExporterCustodian(raw))
    for _ in range(2):
        with pytest.raises(KeyboardInterrupt) as failure:
            adapter.shutdown(timeout=42)
        assert failure.value is original
    assert raw.shutdown_timeouts == [42]


def test_first_reader_base_exception_does_not_skip_later_reader_or_exporter() -> None:
    custody = owner()
    first = custody.acquire_prometheus(custody.installation.registry)
    raw = RecordingExporter()
    later = custody.acquire_periodic(OwnedMetricExporter(custody.retain_exporter(raw)), interval_millis=60_000, timeout_millis=5_000)
    custody.acquire_provider(OwnedProviderFactory(), resource=Resource({}), views=())
    original = SystemExit()
    unregister = custody.installation.registry.unregister
    calls = 0

    def fail_once(collector: object) -> None:
        nonlocal calls
        calls += 1
        unregister(collector)
        raise original

    with patch.object(custody.installation.registry, "unregister", fail_once), pytest.raises(SystemExit) as failure:
        custody.shutdown_sync()
    assert failure.value is original
    assert calls == 1
    assert not first.registered()
    assert not later._daemon_thread.is_alive()
    assert len(raw.shutdown_timeouts) == 1
    assert custody.witness is None


def test_running_cleanup_invocation_cannot_authorize_completion() -> None:
    custody = owner()
    reader = custody.acquire_prometheus(custody.installation.registry)
    assert reader.cleanup.begin()
    with pytest.raises(TelemetryCustodyUnresolved):
        custody.shutdown_sync()
    assert custody.witness is None
    # Test-owned reconciliation; this does not manufacture a success receipt.
    custody.installation.registry.unregister(reader.retained_collector)


def test_foreign_process_is_refused_before_inherited_lock_acquisition() -> None:
    custody = owner()
    custody.creator_pid -= 1
    with patch.object(custody, "lock", object()), pytest.raises(TelemetryCustodyUnresolved):
        custody.shutdown_sync()


def test_foreign_owner_is_refused_before_any_new_reader_allocation() -> None:
    installation = OwnedTestTelemetryInstallation()
    first = OperatorTelemetryCleanupOwner(installation=installation)
    second = OperatorTelemetryCleanupOwner(installation=installation)
    assert installation.reserve(first) is None
    with pytest.raises(TelemetryCustodyUnresolved):
        installation.reserve(second)
    assert second.readers == [] and second.provider is None
    second.shutdown_sync()
    assert installation.record is not None and installation.record.owner is first
    first.shutdown_sync()


def test_setter_install_then_raise_retains_exact_stage_and_original() -> None:
    original = RuntimeError("installation failed")

    class Installation(OwnedTestTelemetryInstallation):
        def set_provider(self, provider) -> None:
            super().set_provider(provider)
            raise original

    installation = Installation()
    custody = OperatorTelemetryCleanupOwner(installation=installation)
    installation.reserve(custody)
    custody.acquire_prometheus(installation.registry)
    provider = custody.acquire_provider(OwnedProviderFactory(), resource=Resource({}), views=())
    with pytest.raises(RuntimeError) as failure:
        installation.install(custody, provider)
    assert failure.value is original
    assert installation.record.stage is InstallationStage.INSTALLED_EXACT
    assert installation.record.state == "closed"
    custody.shutdown_sync()
    with pytest.raises(TelemetryCustodyUnresolved):
        installation.reserve(OperatorTelemetryCleanupOwner(installation=installation))


def test_getter_failure_remains_unresolved_after_actual_cleanup() -> None:
    original = KeyboardInterrupt()

    class Installation(OwnedTestTelemetryInstallation):
        def get_provider(self) -> object:
            raise original

    installation = Installation()
    custody = OperatorTelemetryCleanupOwner(installation=installation)
    installation.reserve(custody)
    custody.acquire_prometheus(installation.registry)
    provider = custody.acquire_provider(OwnedProviderFactory(), resource=Resource({}), views=())
    with pytest.raises(KeyboardInterrupt) as failure:
        installation.install(custody, provider)
    assert failure.value is original
    custody.shutdown_sync()
    assert installation.record.state == "unresolved"
    with pytest.raises(TelemetryCustodyUnresolved):
        installation.reset(custody, custody.witness, installation.replaceability_token)


@pytest.mark.parametrize("field", ["_all_metric_readers", "_all_metric_readers_lock"])
def test_changed_sdk_registration_authority_refuses_witness(field: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from opentelemetry.sdk.metrics import MeterProvider

    custody = owner()
    reader = custody.acquire_prometheus(custody.installation.registry)
    provider = custody.acquire_provider(OwnedProviderFactory(), resource=Resource({}), views=())
    retained_set = provider.registration_set
    retained_lock = provider.registration_lock
    monkeypatch.setattr(MeterProvider, field, object())
    with pytest.raises(BaseExceptionGroup):
        custody.shutdown_sync()
    assert custody.witness is None
    assert not reader.registered()
    # Restore the mutated global authority before disposing this deliberate
    # unresolved negative's exact retained registration, without a witness.
    monkeypatch.undo()
    with retained_lock:
        assert any(item is reader for item in retained_set)
        retained_set.remove(reader)


def test_failed_installation_cleanup_never_publishes_a_witness() -> None:
    original = KeyboardInterrupt()

    class Installation(OwnedTestTelemetryInstallation):
        def finish_cleanup(self, owner, *, successful: bool) -> None:
            raise original

    custody = OperatorTelemetryCleanupOwner(installation=Installation())
    custody.acquire_prometheus(custody.installation.registry)
    custody.acquire_provider(OwnedProviderFactory(), resource=Resource({}), views=())
    for _ in range(2):
        with pytest.raises(KeyboardInterrupt) as failure:
            custody.shutdown_sync()
        assert failure.value is original
        assert custody.witness is None
        assert custody.state == "failed"


def test_actual_ticker_survives_sdk_timed_join_until_physical_release() -> None:
    import threading

    class BlockingExporter(RecordingExporter):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def export(self, metrics_data, timeout_millis=10_000, **kwargs):
            self.entered.set()
            assert self.release.wait(5)
            return super().export(metrics_data, timeout_millis=timeout_millis, **kwargs)

    custody = owner()
    raw = BlockingExporter()
    reader = custody.acquire_periodic(OwnedMetricExporter(custody.retain_exporter(raw)), interval_millis=60_000, timeout_millis=5_000)
    provider = custody.acquire_provider(OwnedProviderFactory(), resource=Resource({}), views=())
    provider.get_meter("physical").create_counter("physical.final").add(1)
    errors: list[BaseException] = []

    def shutdown() -> None:
        try:
            reader.shutdown(timeout_millis=10)
        except BaseException as error:
            errors.append(error)

    shutdown_thread = threading.Thread(target=shutdown)
    shutdown_thread.start()
    try:
        assert raw.entered.wait(1)
        shutdown_thread.join(timeout=0.05)
        assert shutdown_thread.is_alive()
        assert reader._daemon_thread.is_alive()
        assert not reader.cleanup.finished
        assert custody.witness is None
    finally:
        raw.release.set()
        shutdown_thread.join(timeout=5)
    assert not shutdown_thread.is_alive()
    assert not reader._daemon_thread.is_alive()
    assert reader.cleanup.finished
    # The SDK sends a negative remaining deadline once its timed join expires;
    # the strict adapter retains that ABI fault while physical completion joins.
    assert len(errors) == 1 and isinstance(errors[0], TelemetryCustodyUnresolved)
    with pytest.raises(TelemetryCustodyUnresolved) as failed:
        custody.shutdown_sync()
    assert failed.value is errors[0]
    assert len(raw.shutdown_timeouts) == 1
    assert custody.witness is None


def test_periodic_constructor_failure_after_start_keeps_actual_thread_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    original = RuntimeError("at-fork registration failed")

    def fail_registration(**kwargs: object) -> None:
        raise original

    custody = owner()
    raw = RecordingExporter()
    adapter = OwnedMetricExporter(custody.retain_exporter(raw))
    monkeypatch.setattr(os, "register_at_fork", fail_registration)
    with pytest.raises(RuntimeError) as failure:
        custody.acquire_periodic(adapter, interval_millis=60_000, timeout_millis=5_000)
    assert failure.value is original
    [reader] = custody.readers
    assert reader._daemon_thread.is_alive()
    witness = custody.shutdown_sync()
    assert not reader._daemon_thread.is_alive()
    assert len(raw.shutdown_timeouts) == 1
    assert witness is custody.witness


def test_constructor_fault_and_missing_sdk_guard_preserve_both_originals(monkeypatch: pytest.MonkeyPatch) -> None:
    original = KeyboardInterrupt()

    def fail_constructor(self, **kwargs: object) -> None:
        raise original

    custody = owner()
    reader = custody.acquire_prometheus(custody.installation.registry)
    monkeypatch.setattr(sdk_metrics.MeterProvider, "__init__", fail_constructor)
    with pytest.raises(BaseExceptionGroup) as failure:
        custody.acquire_provider(OwnedProviderFactory(), resource=Resource({}), views=())
    assert failure.value.exceptions[0] is original
    assert all(isinstance(error, TelemetryCustodyUnresolved) for error in failure.value.exceptions[1:])
    with pytest.raises(BaseExceptionGroup):
        custody.shutdown_sync()
    assert not reader.registered()
    assert custody.witness is None


def test_unexposed_exporter_constructor_never_authorizes_a_witness() -> None:
    custody = owner()
    original = RuntimeError("exporter constructor failed")
    custody.exporter_acquisition_unknown(original)
    with pytest.raises(TelemetryCustodyUnresolved) as failure:
        custody.shutdown_sync()
    assert failure.value.__cause__ is original
    assert custody.witness is None


@pytest.mark.parametrize("method", ["__eq__", "__hash__"])
def test_changed_reader_identity_semantics_never_authorize_set_reconciliation(method: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.operator_telemetry_custody import OwnedPrometheusMetricReader

    custody = owner()
    reader = custody.acquire_prometheus(custody.installation.registry)
    provider = custody.acquire_provider(OwnedProviderFactory(), resource=Resource({}), views=())
    replacement = (lambda self, other: True) if method == "__eq__" else (lambda self: 17)
    monkeypatch.setattr(OwnedPrometheusMetricReader, method, replacement)
    with pytest.raises(BaseExceptionGroup):
        custody.shutdown_sync()
    assert custody.witness is None
    monkeypatch.undo()
    assert not reader.registered()
    with provider.registration_lock:
        assert any(value is reader for value in provider.registration_set)
        provider.registration_set.remove(reader)


def test_foreign_collect_callback_is_never_overwritten() -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    custody = owner()
    reader = custody.acquire_prometheus(custody.installation.registry)
    provider = custody.acquire_provider(OwnedProviderFactory(), resource=Resource({}), views=())
    foreign_reader = InMemoryMetricReader()
    foreign_provider = MeterProvider(metric_readers=(foreign_reader,), shutdown_on_exit=False)
    foreign_callback = foreign_reader._collect
    object.__setattr__(reader, "_collect", foreign_callback)
    try:
        with pytest.raises(BaseExceptionGroup):
            custody.shutdown_sync()
        assert reader._collect is foreign_callback
        assert custody.witness is None
    finally:
        with provider.registration_lock:
            provider.registration_set.remove(reader)
        foreign_provider.shutdown()
        with MeterProvider._all_metric_readers_lock:
            MeterProvider._all_metric_readers.remove(foreign_reader)


def test_owned_provider_shadow_registration_set_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    custody = owner()
    reader = custody.acquire_prometheus(custody.installation.registry)
    provider = custody.acquire_provider(OwnedProviderFactory(), resource=Resource({}), views=())
    monkeypatch.setattr(provider, "_all_metric_readers", object())
    with pytest.raises(BaseExceptionGroup):
        custody.shutdown_sync()
    assert custody.witness is None
    assert not reader.registered()
    monkeypatch.undo()
    with provider.registration_lock:
        provider.registration_set.remove(reader)


def test_resource_acquisition_after_successful_cleanup_is_refused_before_allocation() -> None:
    custody = owner()
    witness = custody.shutdown_sync()
    with pytest.raises(TelemetryCustodyUnresolved):
        custody.acquire_prometheus(custody.installation.registry)
    assert custody.readers == []
    assert custody.shutdown_sync() is witness


def test_failed_app_constructor_arms_before_lexical_telemetry_cleanup(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import elspeth.web.app as app_module
    from elspeth.web.config import WebSettings
    from elspeth.web.process_watchdog_codec import RecoveryReason
    from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog

    watchdogs: list[OwnedTestProcessWatchdog] = []
    original = KeyboardInterrupt()
    observed: list[str] = []

    class ObservingOwner(OperatorTelemetryCleanupOwner):
        __slots__ = ()

        def shutdown_sync(self):
            assert watchdogs[0].reasons == [RecoveryReason.FAILED_STARTUP]
            assert not watchdogs[0].completed
            observed.append("telemetry")
            return super().shutdown_sync()

    custody = ObservingOwner(installation=OwnedTestTelemetryInstallation())

    def factory(event):
        watchdog = OwnedTestProcessWatchdog(event)
        watchdogs.append(watchdog)
        return watchdog

    def fail_catalog():
        raise original

    monkeypatch.setattr(OperatorTelemetryCleanupOwner, "create_for_application", classmethod(lambda cls: custody))
    monkeypatch.setattr(app_module, "create_catalog_service", fail_catalog)
    with pytest.raises(KeyboardInterrupt) as failed:
        app_module.create_app(
            WebSettings(
                data_dir=tmp_path,
                composer_boot_probe_enabled=False,
                composer_max_composition_turns=15,
                composer_max_discovery_turns=10,
                composer_timeout_seconds=85,
                composer_rate_limit_per_minute=100,
                shareable_link_signing_key=b"\x00" * 32,
            ),
            process_watchdog_factory=factory,
        )
    assert failed.value is original
    assert observed == ["telemetry"]
    assert custody.witness is not None
    assert not watchdogs[0].completed
    assert all(not reader.registered() for reader in custody.readers)


def test_production_closed_record_and_reset_refusal_are_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    from opentelemetry import metrics

    from elspeth.web import operator_telemetry
    from elspeth.web.config import WebSettings
    from elspeth.web.operator_telemetry_installation import TelemetryInstallation

    installation = TelemetryInstallation()
    custody = OperatorTelemetryCleanupOwner(installation=installation)
    installed: list[object] = []
    monkeypatch.setattr(metrics, "set_meter_provider", installed.append)
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: installed[-1])
    monkeypatch.setattr(operator_telemetry, "_runtime", None)
    runtime = operator_telemetry.bootstrap_operator_telemetry(
        WebSettings(
            composer_max_composition_turns=15,
            composer_max_discovery_turns=10,
            composer_timeout_seconds=85,
            composer_rate_limit_per_minute=100,
            shareable_link_signing_key=b"\x00" * 32,
        ),
        cleanup_owner=custody,
        factories=operator_telemetry.OperatorTelemetryFactories(),
    )
    with pytest.raises(TelemetryCustodyUnresolved, match="Production"):
        operator_telemetry.reset_operator_telemetry_for_tests()
    assert not custody.provider.cleanup.started
    assert installation.record.state == "active"
    runtime.shutdown_sync()
    assert installation.record.state == "closed"
    replacement = OperatorTelemetryCleanupOwner(installation=installation)
    with pytest.raises(TelemetryCustodyUnresolved):
        installation.reserve(replacement)
    assert replacement.readers == []
    assert replacement.provider is None
    assert installation.record.owner is custody


def test_actual_postbootstrap_fork_creates_no_reader_and_refuses_inherited_owner() -> None:
    import json
    import subprocess
    import sys

    script = """
import json, os, threading
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult
from opentelemetry.sdk.resources import Resource
from elspeth.web.operator_telemetry_custody import OperatorTelemetryCleanupOwner, OwnedMetricExporter, OwnedProviderFactory, TelemetryCustodyUnresolved
from elspeth.web.operator_telemetry_installation import OwnedTestTelemetryInstallation
class Exporter(MetricExporter):
    def export(self, *args, **kwargs): return MetricExportResult.SUCCESS
    def force_flush(self, *args, **kwargs): return True
    def shutdown(self, *args, **kwargs): return None
owner = OperatorTelemetryCleanupOwner(installation=OwnedTestTelemetryInstallation())
reader = owner.acquire_periodic(OwnedMetricExporter(owner.retain_exporter(Exporter())), interval_millis=60000, timeout_millis=5000)
owner.acquire_provider(OwnedProviderFactory(), resource=Resource({}), views=())
read_fd, write_fd = os.pipe()
pid = os.fork()
if pid == 0:
    os.close(read_fd)
    refused = False
    try: owner.shutdown_sync()
    except TelemetryCustodyUnresolved: refused = True
    result = {"refused": refused, "fork_invalid": reader.fork_invalid, "new_reader_threads": sum(t.name == "OtelPeriodicExportingMetricReader" for t in threading.enumerate()), "witness": owner.witness is not None}
    os.write(write_fd, json.dumps(result).encode())
    os.close(write_fd)
    os._exit(0)
os.close(write_fd)
_, status = os.waitpid(pid, 0)
result = os.read(read_fd, 4096).decode()
os.close(read_fd)
owner.shutdown_sync()
assert os.waitstatus_to_exitcode(status) == 0
print(result)
"""
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=5, check=True)
    assert json.loads(completed.stdout) == {"refused": True, "fork_invalid": True, "new_reader_threads": 0, "witness": False}


def test_actual_sdk_constructor_receives_explicit_false_and_no_exit_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    original_init = sdk_metrics.MeterProvider.__init__
    received: list[object] = []
    registrations: list[object] = []

    def capture(self, *args, **kwargs) -> None:
        assert "shutdown_on_exit" in kwargs
        received.append(kwargs["shutdown_on_exit"])
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(sdk_metrics.MeterProvider, "__init__", capture)
    monkeypatch.setattr(sdk_metrics, "register", registrations.append)
    custody = owner()
    custody.acquire_prometheus(custody.installation.registry)
    provider = custody.acquire_provider(OwnedProviderFactory(), resource=Resource({}), views=())
    assert received == [False]
    assert registrations == []
    assert provider._atexit_handler is None
    assert custody.shutdown_sync() is custody.witness


def test_factory_bypassing_false_receipt_cannot_publish_completion() -> None:
    class BypassingFactory(OwnedProviderFactory):
        def construct_into(self, provider, *, resource, views, shutdown_on_exit) -> None:
            assert shutdown_on_exit is False
            sdk_metrics.MeterProvider.__init__(
                provider, metric_readers=provider.owned_readers, resource=resource, views=views, shutdown_on_exit=False
            )
            provider.sdk_initialized = True

    custody = owner()
    reader = custody.acquire_prometheus(custody.installation.registry)
    with pytest.raises(TelemetryCustodyUnresolved):
        custody.acquire_provider(BypassingFactory(), resource=Resource({}), views=())
    with pytest.raises(TelemetryCustodyUnresolved):
        custody.shutdown_sync()
    assert custody.witness is None
    assert not reader.registered()


@pytest.mark.parametrize("package", ["opentelemetry-sdk", "opentelemetry-exporter-otlp-proto-grpc", "opentelemetry-exporter-prometheus"])
def test_unsupported_sdk_version_refuses_before_reader_allocation(package: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web import operator_telemetry
    from elspeth.web.config import WebSettings
    from elspeth.web.operator_telemetry_custody import importlib

    actual_version = importlib.metadata.version
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "unsupported" if name == package else actual_version(name))
    custody = owner()
    with pytest.raises(TelemetryCustodyUnresolved):
        operator_telemetry.bootstrap_operator_telemetry(
            WebSettings(
                composer_max_composition_turns=15,
                composer_max_discovery_turns=10,
                composer_timeout_seconds=85,
                composer_rate_limit_per_minute=100,
                shareable_link_signing_key=b"\x00" * 32,
            ),
            cleanup_owner=custody,
        )
    assert custody.readers == [] and custody.provider is None
    custody.shutdown_sync()


@pytest.mark.parametrize("field", ["_all_metric_readers", "_all_metric_readers_lock", "shutdown"])
def test_unsupported_sdk_shape_refuses_before_reader_allocation(field: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web import operator_telemetry
    from elspeth.web.config import WebSettings

    monkeypatch.setattr(sdk_metrics.MeterProvider, field, object())
    custody = owner()
    with pytest.raises(TelemetryCustodyUnresolved):
        operator_telemetry.bootstrap_operator_telemetry(
            WebSettings(
                composer_max_composition_turns=15,
                composer_max_discovery_turns=10,
                composer_timeout_seconds=85,
                composer_rate_limit_per_minute=100,
                shareable_link_signing_key=b"\x00" * 32,
            ),
            cleanup_owner=custody,
        )
    assert custody.readers == [] and custody.provider is None
    custody.shutdown_sync()


def test_prometheus_registration_insert_then_raise_retains_original_and_exact_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    custody = owner()
    registry = custody.installation.registry
    register = registry.register
    original = KeyboardInterrupt()

    def insert_then_raise(collector) -> None:
        register(collector)
        raise original

    monkeypatch.setattr(registry, "register", insert_then_raise)
    with pytest.raises(KeyboardInterrupt) as failure:
        custody.acquire_prometheus(registry)
    assert failure.value is original
    [reader] = custody.readers
    assert reader.registration_inserted is True and reader.registered()
    witness = custody.shutdown_sync()
    assert witness is custody.witness
    assert not reader.registered()
