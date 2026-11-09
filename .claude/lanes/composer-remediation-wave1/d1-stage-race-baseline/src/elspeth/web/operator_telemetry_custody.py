"""Exact application ownership of the reviewed OpenTelemetry SDK resources."""

from __future__ import annotations

import importlib.metadata
import inspect
import math
import os
import threading
import weakref
from _thread import LockType
from dataclasses import dataclass
from types import MethodType
from typing import TYPE_CHECKING, Literal

from opentelemetry.exporter.prometheus import PrometheusMetricReader, _CustomCollector
from opentelemetry.sdk.metrics import (
    Counter,
    Histogram,
    MeterProvider,
    ObservableCounter,
    ObservableGauge,
    ObservableUpDownCounter,
    UpDownCounter,
)
from opentelemetry.sdk.metrics._internal.measurement_consumer import SynchronousMeasurementConsumer
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    MetricExporter,
    MetricExportResult,
    MetricReader,
    MetricsData,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.metrics.view import View
from opentelemetry.sdk.resources import Resource
from opentelemetry.util._once import Once
from prometheus_client import REGISTRY, CollectorRegistry

from elspeth.contracts.errors import AuditIntegrityError

if TYPE_CHECKING:
    from elspeth.web.operator_telemetry_installation import TelemetryInstallation

_LOCK_TYPE = type(threading.Lock())
_SDK_PROVIDER_SHUTDOWN = MeterProvider.shutdown
_SDK_PERIODIC_SHUTDOWN = PeriodicExportingMetricReader.shutdown


class TelemetryCustodyUnresolved(AuditIntegrityError):
    """Actual SDK ownership could not be discharged with a physical witness."""


def _sdk_field(instance: object, name: str) -> object:
    # This boundary parses retained SDK objects, including failed constructors.
    try:
        return object.__getattribute__(instance, name)
    except AttributeError as error:
        raise TelemetryCustodyUnresolved("Required telemetry SDK field absent") from error


def _identity_present(values: object, target: object) -> bool:
    if type(values) is not weakref.WeakSet:
        raise TelemetryCustodyUnresolved("Telemetry SDK registration set malformed")
    return any(value is target for value in values)


def verify_telemetry_sdk() -> None:
    for package, expected in (
        ("opentelemetry-sdk", "1.40.0"),
        ("opentelemetry-exporter-otlp-proto-grpc", "1.40.0"),
        ("opentelemetry-exporter-prometheus", "0.61b0"),
    ):
        if importlib.metadata.version(package) != expected:
            raise TelemetryCustodyUnresolved("Telemetry SDK version unsupported")
    if not isinstance(MeterProvider._all_metric_readers, weakref.WeakSet):
        raise TelemetryCustodyUnresolved("Telemetry SDK registration set malformed")
    if MeterProvider.shutdown is not _SDK_PROVIDER_SHUTDOWN or PeriodicExportingMetricReader.shutdown is not _SDK_PERIODIC_SHUTDOWN:
        raise TelemetryCustodyUnresolved("Telemetry SDK cleanup implementation changed")
    if "shutdown_on_exit" not in inspect.signature(MeterProvider.__init__).parameters:
        raise TelemetryCustodyUnresolved("Telemetry SDK explicit shutdown ABI absent")
    if type(MeterProvider._all_metric_readers_lock) is not _LOCK_TYPE:
        raise TelemetryCustodyUnresolved("Telemetry SDK registration lock malformed")


@dataclass(frozen=True, slots=True)
class TelemetryCompletionWitness:
    owner: OperatorTelemetryCleanupOwner
    creator_pid: int


class _CleanupInvocation:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started = False
        self.finished = False
        self.errors: list[BaseException] = []

    def begin(self) -> bool:
        with self.lock:
            if self.started:
                return False
            self.started = True
            return True

    def require_completed(self) -> None:
        with self.lock:
            if not self.finished:
                raise TelemetryCustodyUnresolved("Telemetry cleanup invocation is unresolved")
            errors = list(self.errors)
        _raise_originals(errors)

    def finish(self, error: BaseException | None) -> None:
        with self.lock:
            if error is not None:
                self.errors.append(error)
            self.finished = True


class RawMetricExporterCustodian:
    """One actual returned exporter, reachable before wrapper construction."""

    def __init__(self, exporter: MetricExporter) -> None:
        if not isinstance(exporter, MetricExporter):
            raise TypeError("Telemetry exporter must satisfy its nominal SDK contract")
        self.exporter = exporter
        self.cleanup = _CleanupInvocation()

    def shutdown(self, timeout_millis: float) -> None:
        if not self.cleanup.begin():
            if not self.cleanup.finished:
                raise TelemetryCustodyUnresolved("Exporter cleanup is already running")
            _raise_originals(self.cleanup.errors)
            return
        error: BaseException | None = None
        try:
            self.exporter.shutdown(timeout_millis=timeout_millis)
        except BaseException as failure:
            error = failure
            raise
        finally:
            self.cleanup.finish(error)


class OwnedMetricExporter(MetricExporter):
    """Exact millisecond adapter for the periodic SDK shutdown keyword."""

    def __init__(self, custody: RawMetricExporterCustodian) -> None:
        if not isinstance(custody, RawMetricExporterCustodian):
            raise TypeError("Exporter adapter requires actual owned custody")
        self.custody = custody
        self._inner = custody.exporter
        super().__init__(
            preferred_temporality=self._inner._preferred_temporality,
            preferred_aggregation=self._inner._preferred_aggregation,
        )

    def export(self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: object) -> MetricExportResult:
        return self._inner.export(metrics_data, timeout_millis=timeout_millis, **kwargs)

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return self._inner.force_flush(timeout_millis=timeout_millis)

    def shutdown(self, timeout_millis: float = 30_000, *, timeout: float | None = None, **kwargs: object) -> None:
        if kwargs:
            raise TelemetryCustodyUnresolved("Exporter shutdown keyword arguments unsupported")
        if timeout is not None and timeout_millis != 30_000:
            raise TelemetryCustodyUnresolved("Exporter shutdown timeout arguments conflict")
        selected = timeout_millis if timeout is None else timeout
        if isinstance(selected, bool) or not isinstance(selected, (int, float)) or not math.isfinite(selected) or selected < 0:
            raise TelemetryCustodyUnresolved("Exporter shutdown timeout malformed")
        self.custody.shutdown(selected)


class OwnedPrometheusMetricReader(PrometheusMetricReader):
    """Bind registration and removal to one exact retained collector registry."""

    def prepare(self, registry: CollectorRegistry) -> None:
        self.cleanup = _CleanupInvocation()
        self.retained_registry = registry
        self.retained_collector: _CustomCollector | None = None
        self.registration_inserted: bool | None = None

    def initialize_sdk(self) -> None:
        MetricReader.__init__(
            self,
            preferred_temporality={
                Counter: AggregationTemporality.CUMULATIVE,
                UpDownCounter: AggregationTemporality.CUMULATIVE,
                Histogram: AggregationTemporality.CUMULATIVE,
                ObservableCounter: AggregationTemporality.CUMULATIVE,
                ObservableUpDownCounter: AggregationTemporality.CUMULATIVE,
                ObservableGauge: AggregationTemporality.CUMULATIVE,
            },
        )
        self._collector = _CustomCollector(False)
        self.retained_collector = self._collector
        failures: list[BaseException] = []
        try:
            self.retained_registry.register(self._collector)
        except BaseException as error:
            failures.append(error)
        try:
            self.registration_inserted = self.registered()
        except BaseException as error:
            failures.append(error)
        _raise_originals(failures)
        if self.registration_inserted is not True:
            raise TelemetryCustodyUnresolved("Prometheus registration did not acquire membership")
        object.__setattr__(self._collector, "_callback", self.collect)

    def registered(self) -> bool:
        collector = self.retained_collector
        if collector is None:
            raise TelemetryCustodyUnresolved("Prometheus collector acquisition incomplete")
        lock = _sdk_field(self.retained_registry, "_lock")
        members = _sdk_field(self.retained_registry, "_collector_to_names")
        if not isinstance(lock, LockType) or not isinstance(members, dict):
            raise TelemetryCustodyUnresolved("Prometheus registry shape malformed")
        with lock:
            return any(member is collector for member in members)

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        if not self.cleanup.begin():
            self.cleanup.require_completed()
            return
        error: BaseException | None = None
        try:
            if self.registration_inserted is None or self.retained_collector is None:
                raise TelemetryCustodyUnresolved("Prometheus collector registration unresolved")
            current = self.registered()
            if current is not self.registration_inserted:
                raise TelemetryCustodyUnresolved("Prometheus collector membership changed")
            if current:
                self.retained_registry.unregister(self.retained_collector)
            if self.registered():
                raise TelemetryCustodyUnresolved("Prometheus collector remained registered")
        except BaseException as failure:
            error = failure
            raise
        finally:
            self.cleanup.finish(error)


class OwnedPeriodicExportingMetricReader(PeriodicExportingMetricReader):
    """Retain an actual SDK thread through constructor and shutdown failures."""

    def prepare(self, exporter: MetricExporter) -> None:
        self.retained_exporter = exporter
        self.cleanup = _CleanupInvocation()
        self.creator_pid = os.getpid()
        self.fork_invalid = False

    def _at_fork_reinit(self) -> None:
        # No inherited locks, parent events, cleanup or new worker in the child.
        self.fork_invalid = True

    def physical_fields(self) -> tuple[threading.Event, threading.Thread | None]:
        if self.creator_pid != os.getpid() or self.fork_invalid:
            raise TelemetryCustodyUnresolved("Inherited telemetry reader has foreign process ownership")
        event = _sdk_field(self, "_shutdown_event")
        once = _sdk_field(self, "_shutdown_once")
        thread = _sdk_field(self, "_daemon_thread")
        exporter = _sdk_field(self, "_exporter")
        interval = _sdk_field(self, "_export_interval_millis")
        export_lock = _sdk_field(self, "_export_lock")
        timeout = _sdk_field(self, "_export_timeout_millis")
        shutdown = _sdk_field(self, "_shutdown")
        if not isinstance(export_lock, LockType) or type(shutdown) is not bool:
            raise TelemetryCustodyUnresolved("Telemetry reader execution fields malformed")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise TelemetryCustodyUnresolved("Telemetry reader export timeout malformed")
        if not isinstance(event, threading.Event) or not isinstance(once, Once):
            raise TelemetryCustodyUnresolved("Telemetry reader shutdown fields malformed")
        if exporter is not self.retained_exporter or isinstance(interval, bool) or not isinstance(interval, (int, float)):
            raise TelemetryCustodyUnresolved("Telemetry reader exporter/interval malformed")
        if thread is not None and not isinstance(thread, threading.Thread):
            raise TelemetryCustodyUnresolved("Telemetry reader thread malformed")
        if thread is None and interval != math.inf:
            raise TelemetryCustodyUnresolved("Telemetry reader thread acquisition unresolved")
        return event, thread

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        if self.creator_pid != os.getpid() or self.fork_invalid:
            raise TelemetryCustodyUnresolved("Inherited telemetry reader cannot shut down parent ownership")
        if not self.cleanup.begin():
            self.cleanup.require_completed()
            return
        failures: list[BaseException] = []
        try:
            try:
                super().shutdown(timeout_millis=timeout_millis, **kwargs)
            except BaseException as error:
                failures.append(error)
            try:
                event, thread = self.physical_fields()
                event.set()
                if thread is not None:
                    if thread.ident is None:
                        raise TelemetryCustodyUnresolved("Telemetry reader thread start unresolved")
                    thread.join()
                    if thread.is_alive():
                        raise TelemetryCustodyUnresolved("Telemetry reader thread remained alive")
            except BaseException as error:
                failures.append(error)
        finally:
            for retained_error in failures:
                self.cleanup.errors.append(retained_error)
            self.cleanup.finish(None)
        _raise_originals(failures)


class OwnedMeterProvider(MeterProvider):
    def prepare(self, readers: tuple[OwnedPrometheusMetricReader | OwnedPeriodicExportingMetricReader, ...]) -> None:
        self.cleanup = _CleanupInvocation()
        self.owned_readers = readers
        self.false_forwarded = False
        self.sdk_initialized = False
        self.registration_set = MeterProvider._all_metric_readers
        self.registration_lock = MeterProvider._all_metric_readers_lock
        self.registered_readers: list[MetricReader] = []
        self.associated_aggregate: BaseException | None = None
        if self._all_metric_readers is not self.registration_set or self._all_metric_readers_lock is not self.registration_lock:
            raise TelemetryCustodyUnresolved("Owned provider shadows SDK registration authority")
        with self.registration_lock:
            for reader in readers:
                self.verify_reader_identity(reader)
                if _identity_present(self.registration_set, reader) or _sdk_field(reader, "_collect") is not None:
                    raise TelemetryCustodyUnresolved("Telemetry reader already bound before provider acquisition")

    def initialize_sdk(self, *, resource: Resource, views: tuple[View, ...], shutdown_on_exit: Literal[False]) -> None:
        if shutdown_on_exit is not False:
            raise TelemetryCustodyUnresolved("SDK exit authority is forbidden")
        self.false_forwarded = True
        failures: list[BaseException] = []
        try:
            super().__init__(metric_readers=self.owned_readers, resource=resource, shutdown_on_exit=False, views=views)
            self.sdk_initialized = True
        except BaseException as error:
            failures.append(error)
        for observation in (self.capture_registration, self.verify_no_exit_authority):
            try:
                observation()
            except BaseException as error:
                failures.append(error)
        _raise_originals(failures)

    def verify_no_exit_authority(self) -> None:
        if not self.false_forwarded or _sdk_field(self, "_atexit_handler") is not None:
            raise TelemetryCustodyUnresolved("Telemetry SDK exit authority unresolved")

    def verify_reader_identity(self, reader: MetricReader) -> None:
        reader_type = type(reader)
        if reader_type.__eq__ is not object.__eq__ or reader_type.__hash__ is not object.__hash__:
            raise TelemetryCustodyUnresolved("Telemetry reader identity semantics changed")

    def verify_registration_authority(self) -> None:
        if (
            MeterProvider._all_metric_readers is not self.registration_set
            or MeterProvider._all_metric_readers_lock is not self.registration_lock
            or self._all_metric_readers is not self.registration_set
            or self._all_metric_readers_lock is not self.registration_lock
        ):
            raise TelemetryCustodyUnresolved("Telemetry SDK registration authority changed")

    def capture_registration(self) -> None:
        self.verify_registration_authority()
        consumer = _sdk_field(self, "_measurement_consumer")
        config = _sdk_field(self, "_sdk_config")
        if not isinstance(consumer, SynchronousMeasurementConsumer) or _sdk_field(config, "metric_readers") is not self.owned_readers:
            raise TelemetryCustodyUnresolved("Telemetry provider reader/consumer binding malformed")
        with self.registration_lock:
            for reader in self.owned_readers:
                self.verify_reader_identity(reader)
                callback = _sdk_field(reader, "_collect")
                if callback is not None and (
                    not isinstance(callback, MethodType)
                    or callback.__self__ is not consumer
                    or callback.__func__ is not SynchronousMeasurementConsumer.collect
                ):
                    raise TelemetryCustodyUnresolved("Telemetry reader has foreign callback")
                if _identity_present(self.registration_set, reader) and all(reader is not prior for prior in self.registered_readers):
                    self.registered_readers.append(reader)

    def reconcile_registration(self) -> None:
        self.capture_registration()
        with self.registration_lock:
            for reader in self.registered_readers:
                if not _identity_present(self.registration_set, reader):
                    raise TelemetryCustodyUnresolved("Telemetry reader registration changed unexpectedly")
                self.registration_set.remove(reader)
                if _identity_present(self.registration_set, reader):
                    raise TelemetryCustodyUnresolved("Telemetry reader registration retained")
                object.__setattr__(reader, "_collect", None)

    def shutdown(self, timeout_millis: float = 30_000) -> None:
        if not self.cleanup.begin():
            self.cleanup.require_completed()
            return
        error: BaseException | None = None
        try:
            self.verify_no_exit_authority()
            self.capture_registration()
            if not self.sdk_initialized or not isinstance(_sdk_field(self, "_shutdown_once"), Once):
                raise TelemetryCustodyUnresolved("Partial telemetry provider shutdown guard unresolved")
            super().shutdown(timeout_millis=timeout_millis)
            if _sdk_field(self, "_shutdown") is not True:
                raise TelemetryCustodyUnresolved("Telemetry SDK provider did not shut down")
        except BaseException as failure:
            error = failure
            # The pinned SDK raises its aggregate only after visiting every
            # reader. Preserve it as a diagnostic associated with the exact
            # ordinary reader originals; independent provider failures remain
            # semantic roots. No exception text is inspected.
            if (
                type(failure) is Exception
                and all(reader.cleanup.finished for reader in self.owned_readers)
                and any(isinstance(item, Exception) for reader in self.owned_readers for item in reader.cleanup.errors)
            ):
                self.associated_aggregate = failure
            raise
        finally:
            self.cleanup.finish(error)


class OwnedProviderFactory:
    def construct_into(
        self, provider: OwnedMeterProvider, *, resource: Resource, views: tuple[View, ...], shutdown_on_exit: Literal[False]
    ) -> None:
        provider.initialize_sdk(resource=resource, views=views, shutdown_on_exit=shutdown_on_exit)


def _append_identity(errors: list[BaseException], error: BaseException) -> None:
    if all(error is not prior for prior in errors):
        errors.append(error)


def _raise_originals(errors: list[BaseException]) -> None:
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Owned telemetry cleanup failed", errors)


class OperatorTelemetryCleanupOwner:
    __slots__ = (
        "__weakref__",
        "acquisition_defects",
        "creator_pid",
        "diagnostics",
        "errors",
        "exporters",
        "installation",
        "lock",
        "provider",
        "readers",
        "state",
        "witness",
    )

    def __init__(self, *, installation: TelemetryInstallation | None = None) -> None:
        from elspeth.web.operator_telemetry_installation import PRODUCTION_TELEMETRY_INSTALLATION, TelemetryInstallation

        selected = PRODUCTION_TELEMETRY_INSTALLATION if installation is None else installation
        if not isinstance(selected, TelemetryInstallation):
            raise TypeError("Telemetry owner requires a nominal installation")
        self.installation = selected
        self.creator_pid = os.getpid()
        self.lock = threading.Lock()
        self.state = "new"
        self.readers: list[OwnedPrometheusMetricReader | OwnedPeriodicExportingMetricReader] = []
        self.provider: OwnedMeterProvider | None = None
        self.errors: list[BaseException] = []
        self.diagnostics: list[BaseException] = []
        self.acquisition_defects: list[BaseException] = []
        self.exporters: list[RawMetricExporterCustodian] = []
        self.witness: TelemetryCompletionWitness | None = None

    @classmethod
    def create_for_application(cls) -> OperatorTelemetryCleanupOwner:
        return cls()

    def assert_process(self) -> None:
        if self.creator_pid != os.getpid():
            raise TelemetryCustodyUnresolved("Inherited telemetry owner has foreign process ownership")
        self.installation.assert_process()

    def assert_acquiring(self) -> None:
        self.assert_process()
        with self.lock:
            if self.state != "new":
                raise TelemetryCustodyUnresolved("Telemetry resource acquisition is closed")

    def retain_exporter(self, exporter: MetricExporter) -> RawMetricExporterCustodian:
        self.assert_acquiring()
        custody = RawMetricExporterCustodian(exporter)
        self.exporters.append(custody)
        return custody

    def exporter_acquisition_unknown(self, original: BaseException) -> None:
        self.assert_acquiring()
        defect = TelemetryCustodyUnresolved("Exporter constructor ownership unresolved")
        defect.__cause__ = original
        self.acquisition_defects.append(defect)

    def acquire_prometheus(self, registry: CollectorRegistry = REGISTRY) -> OwnedPrometheusMetricReader:
        self.assert_acquiring()
        reader = OwnedPrometheusMetricReader.__new__(OwnedPrometheusMetricReader)
        reader.prepare(registry)
        self.readers.append(reader)
        reader.initialize_sdk()
        return reader

    def acquire_periodic(
        self, exporter: MetricExporter, *, interval_millis: float, timeout_millis: float
    ) -> OwnedPeriodicExportingMetricReader:
        self.assert_acquiring()
        if not isinstance(exporter, OwnedMetricExporter):
            raise TypeError("Periodic reader requires the owned timeout adapter")
        reader = OwnedPeriodicExportingMetricReader.__new__(OwnedPeriodicExportingMetricReader)
        reader.prepare(exporter)
        self.readers.append(reader)
        failures: list[BaseException] = []
        try:
            PeriodicExportingMetricReader.__init__(
                reader, exporter, export_interval_millis=interval_millis, export_timeout_millis=timeout_millis
            )
        except BaseException as error:
            failures.append(error)
        try:
            reader.physical_fields()
        except BaseException as error:
            self.acquisition_defects.append(error)
            failures.append(error)
        _raise_originals(failures)
        return reader

    def acquire_provider(self, factory: OwnedProviderFactory, *, resource: Resource, views: tuple[View, ...]) -> OwnedMeterProvider:
        self.assert_acquiring()
        if self.provider is not None:
            raise TelemetryCustodyUnresolved("Telemetry provider acquisition was reused")
        if not isinstance(factory, OwnedProviderFactory):
            raise TypeError("Telemetry provider factory must be nominal")
        provider = OwnedMeterProvider.__new__(OwnedMeterProvider)
        self.provider = provider
        provider.prepare(tuple(self.readers))
        factory.construct_into(provider, resource=resource, views=views, shutdown_on_exit=False)
        provider.verify_no_exit_authority()
        if not provider.sdk_initialized:
            raise TelemetryCustodyUnresolved("Telemetry provider factory ignored SDK construction")
        return provider

    def shutdown_sync(self) -> TelemetryCompletionWitness:
        self.assert_process()
        with self.lock:
            if self.state == "running":
                raise TelemetryCustodyUnresolved("Telemetry cleanup is already running")
            if self.witness is not None:
                return self.witness
            if self.state == "failed":
                _raise_originals(self.errors)
                raise TelemetryCustodyUnresolved("Telemetry cleanup retained unknown custody")
            self.state = "running"
        provider_error: BaseException | None = None
        if self.provider is not None:
            try:
                self.provider.shutdown(timeout_millis=5_000)
            except BaseException as error:
                provider_error = error
        for reader in self.readers:
            invocation_failed = False
            try:
                reader.shutdown(timeout_millis=5_000)
            except BaseException as error:
                invocation_failed = True
                if not reader.cleanup.finished or not reader.cleanup.errors:
                    _append_identity(self.errors, error)
            if not reader.cleanup.finished and not invocation_failed:
                _append_identity(self.errors, TelemetryCustodyUnresolved("Telemetry reader cleanup has no completion receipt"))
            for retained_error in reader.cleanup.errors:
                _append_identity(self.errors, retained_error)
        if self.provider is not None and not self.provider.cleanup.finished and provider_error is None:
            _append_identity(self.errors, TelemetryCustodyUnresolved("Telemetry provider cleanup has no completion receipt"))
        if provider_error is not None:
            if self.provider is not None and provider_error is self.provider.associated_aggregate:
                self.diagnostics.append(provider_error)
            else:
                _append_identity(self.errors, provider_error)
        for retained_error in self.acquisition_defects:
            _append_identity(self.errors, retained_error)
        for exporter in self.exporters:
            try:
                exporter.shutdown(5_000)
            except BaseException as error:
                _append_identity(self.errors, error)
        if self.provider is not None:
            try:
                self.provider.reconcile_registration()
            except BaseException as error:
                self.errors.append(error)
        try:
            self.installation.finish_cleanup(self, successful=not self.errors)
        except BaseException as installation_error:
            _append_identity(self.errors, installation_error)
        with self.lock:
            if self.errors:
                self.state = "failed"
            else:
                self.state = "complete"
                self.witness = TelemetryCompletionWitness(self, self.creator_pid)
        _raise_originals(self.errors)
        if self.witness is None:
            raise TelemetryCustodyUnresolved("Telemetry completion witness missing")
        return self.witness
