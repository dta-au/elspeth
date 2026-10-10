"""AWS ECS operator telemetry policy and process bootstrap tests."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, MetricsData, PeriodicExportingMetricReader

from elspeth.contracts.events import RunStarted
from elspeth.core.config import TelemetrySettings
from elspeth.telemetry.manager import TelemetryManager
from elspeth.web import operator_telemetry
from elspeth.web.config import WebSettings
from elspeth.web.operator_telemetry import (
    AWS_OTLP_ENDPOINT,
    SAFE_CLOUDWATCH_METRIC_ATTRIBUTES,
    OwnedTestOperatorTelemetryFactories,
    apply_operator_pipeline_telemetry,
    bootstrap_operator_telemetry,
    build_aws_operator_pipeline_telemetry,
    record_operator_pipeline_queue_drops,
    reset_operator_telemetry_for_tests,
)
from elspeth.web.operator_telemetry_custody import (
    OperatorTelemetryCleanupOwner,
    OwnedMeterProvider,
    OwnedPeriodicExportingMetricReader,
    OwnedPrometheusMetricReader,
    OwnedProviderFactory,
    RawMetricExporterCustodian,
    TelemetryCustodyUnresolved,
)
from elspeth.web.operator_telemetry_installation import OwnedTestTelemetryInstallation
from tests.fixtures.telemetry import FailingExporter, MockTelemetryConfig


def _web_settings(**overrides: object) -> WebSettings:
    values: dict[str, object] = {
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
        "shareable_link_signing_key": b"\x00" * 32,
    }
    values.update(overrides)
    return WebSettings(**values)  # type: ignore[arg-type]


def _pipeline_settings(telemetry: TelemetrySettings) -> Any:
    @dataclass(frozen=True)
    class _Settings:
        telemetry: TelemetrySettings

        def model_copy(self, *, update: dict[str, object]) -> _Settings:
            return _Settings(telemetry=update["telemetry"])  # type: ignore[arg-type]

    return _Settings(telemetry=telemetry)


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    reset_operator_telemetry_for_tests()
    yield
    reset_operator_telemetry_for_tests()


def test_local_pipeline_telemetry_is_unchanged() -> None:
    authored = TelemetrySettings(
        enabled=True,
        granularity="full",
        fail_on_total_exporter_failure=True,
        exporters=[{"name": "datadog", "options": {"api_key": "authored-secret"}}],
    )
    pipeline = _pipeline_settings(authored)

    effective = apply_operator_pipeline_telemetry(pipeline, _web_settings())

    assert effective is pipeline
    assert effective.telemetry is authored


def test_cloudwatch_metric_dimensions_exclude_unbounded_identity_and_content() -> None:
    forbidden = {
        "account_id",
        "aws_account_id",
        "content",
        "exception_message",
        "prompt",
        "request_id",
        "row_id",
        "run_id",
        "session_id",
        "task_arn",
        "token_id",
        "url",
        "user_id",
    }

    assert SAFE_CLOUDWATCH_METRIC_ATTRIBUTES.isdisjoint(forbidden)
    assert {"reason", "operation", "status", "surface"} <= SAFE_CLOUDWATCH_METRIC_ATTRIBUTES
    # Preflight verdict split (elspeth-ca0bd5d4ef): the three bounded
    # attributes must survive the CloudWatch strip so the verdict series is
    # readable on the AWS OTLP lane, not only via Prometheus.
    assert {"verdict", "budget_exhausted", "repair_turns_used"} <= SAFE_CLOUDWATCH_METRIC_ATTRIBUTES


@pytest.mark.parametrize(
    "authored",
    [
        TelemetrySettings(enabled=False),
        TelemetrySettings(enabled=True, granularity="full", exporters=[{"name": "console"}]),
        TelemetrySettings(enabled=True, exporters=[{"name": "azure_monitor", "options": {"connection_string": "secret"}}]),
        TelemetrySettings(enabled=True, exporters=[{"name": "datadog", "options": {"api_key": "secret"}}]),
        TelemetrySettings(
            enabled=True,
            fail_on_total_exporter_failure=True,
            exporters=[{"name": "otlp", "options": {"endpoint": "https://remote.invalid:4317", "headers": {"authorization": "secret"}}}],
        ),
    ],
)
def test_aws_pipeline_telemetry_is_replaced_by_operator_policy(authored: TelemetrySettings) -> None:
    web = _web_settings(
        deployment_target="aws-ecs",
        operator_telemetry="aws-otlp",
        operator_telemetry_service_name="elspeth-web-prod",
        operator_telemetry_environment="production",
        operator_telemetry_release="git-deadbeef",
        operator_telemetry_ecs_cluster="elspeth-production",
        operator_telemetry_ecs_service="elspeth-web",
        operator_telemetry_task_definition_family="elspeth-web-task",
        operator_telemetry_task_definition_revision="42",
        operator_pipeline_telemetry_granularity="rows",
    )

    effective = apply_operator_pipeline_telemetry(_pipeline_settings(authored), web)

    assert effective.telemetry.enabled is True
    assert effective.telemetry.granularity == "rows"
    assert effective.telemetry.fail_on_total_exporter_failure is False
    assert len(effective.telemetry.exporters) == 1
    exporter = effective.telemetry.exporters[0]
    assert exporter.name == "otlp"
    assert exporter.options == {
        "endpoint": AWS_OTLP_ENDPOINT,
        "headers": {},
        "service_name": "elspeth-web-prod",
        "service_version": "git-deadbeef",
        "deployment_environment": "production",
        "cloud_provider": "aws",
        "aws_ecs_cluster_name": "elspeth-production",
        "aws_ecs_service_name": "elspeth-web",
        "aws_ecs_task_family": "elspeth-web-task",
        "aws_ecs_task_revision": "42",
        "batch_size": 100,
    }
    rendered = repr(effective.telemetry.model_dump())
    assert "remote.invalid" not in rendered
    assert "authorization" not in rendered
    assert "secret" not in rendered


def test_aws_pipeline_telemetry_pure_builder_matches_applied_policy() -> None:
    web = _web_settings(
        deployment_target="aws-ecs",
        operator_telemetry="aws-otlp",
        operator_telemetry_service_name="elspeth-web-prod",
        operator_telemetry_environment="production",
        operator_telemetry_release="git-deadbeef",
        operator_telemetry_ecs_cluster="elspeth-production",
        operator_telemetry_ecs_service="elspeth-web",
        operator_telemetry_task_definition_family="elspeth-web-task",
        operator_telemetry_task_definition_revision="42",
        operator_pipeline_telemetry_granularity="rows",
    )

    built = build_aws_operator_pipeline_telemetry(web)
    applied = apply_operator_pipeline_telemetry(_pipeline_settings(TelemetrySettings(enabled=False)), web)

    assert built == applied.telemetry


@dataclass
class _FakeReader:
    kind: str
    exporter: object | None = None
    interval_ms: int | None = None


class _FakeExporter(MetricExporter):
    def __init__(self, *, endpoint: str, insecure: bool, headers: dict[str, str], timeout: float) -> None:
        super().__init__()
        self.endpoint = endpoint
        self.insecure = insecure
        self.headers = headers
        self.timeout = timeout
        self.results: list[MetricExportResult] = []
        self.exports: list[MetricsData] = []

    def export(self, _data: object, timeout_millis: float = 10_000, **_kwargs: object) -> MetricExportResult:
        del timeout_millis
        self.exports.append(_data)
        return self.results.pop(0) if self.results else MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        del timeout_millis
        return True

    def shutdown(self, timeout_millis: float = 30_000, **_kwargs: object) -> None:
        del timeout_millis


class _CapturingMetricExporter(MetricExporter):
    def __init__(self) -> None:
        super().__init__()
        self.exports: list[MetricsData] = []

    def export(self, metrics_data: MetricsData, timeout_millis: float = 10_000, **_kwargs: object) -> MetricExportResult:
        del timeout_millis
        self.exports.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        del timeout_millis
        return True

    def shutdown(self, timeout_millis: float = 30_000, **_kwargs: object) -> None:
        del timeout_millis


def test_aws_metric_export_preserves_only_bounded_acceptance_correlation() -> None:
    inner = _CapturingMetricExporter()
    exporter = operator_telemetry._HealthTrackingMetricExporter(RawMetricExporterCustodian(inner), operator_telemetry._ExportHealth())
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=60_000)
    provider = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    try:
        counter = provider.get_meter("acceptance-contract").create_counter("operator.acceptance.sentinel")
        counter.add(
            17,
            attributes={
                "elspeth.acceptance.namespace": "acceptance-run-a",
                "elspeth.acceptance.sentinel": "17",
                "surface": "freeform",
                "run_id": "must-not-become-a-metric-dimension",
            },
        )

        assert provider.force_flush(timeout_millis=5_000) is True
        assert len(inner.exports) == 1
        point = inner.exports[0].resource_metrics[0].scope_metrics[0].metrics[0].data.data_points[0]  # type: ignore[union-attr]
        assert point.attributes == {
            "elspeth.acceptance.namespace": "acceptance-run-a",
            "elspeth.acceptance.sentinel": "17",
            "surface": "freeform",
        }
    finally:
        provider.shutdown()


def test_periodic_reader_shutdown_performs_final_collection() -> None:
    inner = _CapturingMetricExporter()
    reader = PeriodicExportingMetricReader(inner, export_interval_millis=60_000)
    provider = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    counter = provider.get_meter("shutdown-contract").create_counter("shutdown.final_collection")
    counter.add(1)

    provider.shutdown(timeout_millis=5_000)

    metric_names = {
        metric.name
        for export in inner.exports
        for resource_metric in export.resource_metrics
        for scope_metric in resource_metric.scope_metrics
        for metric in scope_metric.metrics
    }
    assert "shutdown.final_collection" in metric_names


@dataclass
class _FactoryRecord:
    providers: list[operator_telemetry._Provider] = field(default_factory=list)
    exporters: list[_FakeExporter] = field(default_factory=list)
    installed: list[object] = field(default_factory=list)


class _RecordingFactories(OwnedTestOperatorTelemetryFactories):
    def __init__(self, record: _FactoryRecord) -> None:
        class Installation(OwnedTestTelemetryInstallation):
            __slots__ = ()

            def set_provider(self, provider: OwnedMeterProvider) -> None:
                super().set_provider(provider)
                record.installed.append(provider)

        class ProviderFactory(OwnedProviderFactory):
            def construct_into(self, provider: OwnedMeterProvider, *, resource, views, shutdown_on_exit) -> None:
                assert shutdown_on_exit is False
                super().construct_into(provider, resource=resource, views=views, shutdown_on_exit=shutdown_on_exit)
                record.providers.append(provider)

        def exporter_factory(**kwargs) -> _FakeExporter:
            exporter = _FakeExporter(**kwargs)
            record.exporters.append(exporter)
            return exporter

        super().__init__(exporter_factory=exporter_factory, provider_factory=ProviderFactory())
        self.owner = OperatorTelemetryCleanupOwner(installation=Installation())


def _factories(record: _FactoryRecord) -> _RecordingFactories:
    return _RecordingFactories(record)


def _bootstrap(settings: WebSettings, *, factories: _RecordingFactories):
    try:
        return bootstrap_operator_telemetry(settings, cleanup_owner=factories.owner, factories=factories)
    except BaseException as original:
        try:
            factories.owner.shutdown_sync()
        except BaseException as cleanup:
            raise BaseExceptionGroup("Test bootstrap and actual cleanup failed", [original, cleanup]) from None
        raise


def _aws_settings() -> WebSettings:
    return _web_settings(
        deployment_target="aws-ecs",
        operator_telemetry="aws-otlp",
        operator_telemetry_environment="production",
        operator_telemetry_release="git-deadbeef",
        operator_telemetry_ecs_cluster="elspeth-production",
        operator_telemetry_ecs_service="elspeth-web",
        operator_telemetry_task_definition_family="elspeth-web-task",
        operator_telemetry_task_definition_revision="42",
    )


def _gauge_value(runtime, exporter: _FakeExporter, name: str) -> int | float:
    assert runtime.provider.force_flush(timeout_millis=5_000)
    return next(
        point.value
        for resource in exporter.exports[-1].resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    )


def test_process_bootstrap_local_is_idempotent_prometheus_only() -> None:
    record = _FactoryRecord()
    factories = _factories(record)
    first = _bootstrap(_web_settings(), factories=factories)
    second = _bootstrap(_web_settings(), factories=factories)
    assert first is second
    assert len(record.providers) == len(record.installed) == 1
    assert len(first.readers) == 1
    assert isinstance(first.readers[0], OwnedPrometheusMetricReader)


def test_process_bootstrap_aws_adds_one_fixed_otlp_reader_and_safe_resource() -> None:
    record = _FactoryRecord()
    runtime = _bootstrap(
        _aws_settings().model_copy(update={"operator_telemetry_export_interval_seconds": 17}), factories=_factories(record)
    )
    assert len(runtime.readers) == 2
    assert isinstance(runtime.readers[0], OwnedPrometheusMetricReader)
    assert isinstance(runtime.readers[1], OwnedPeriodicExportingMetricReader)
    exporter = record.exporters[0]
    assert exporter.endpoint == AWS_OTLP_ENDPOINT
    assert exporter.insecure is True
    assert exporter.headers == {}
    assert exporter.timeout == 5.0
    assert runtime.readers[1]._export_interval_millis == 17_000
    assert runtime.resource.attributes == {
        "service.name": "elspeth-web",
        "service.version": "git-deadbeef",
        "deployment.environment": "production",
        "cloud.provider": "aws",
        "aws.ecs.cluster.name": "elspeth-production",
        "aws.ecs.service.name": "elspeth-web",
        "aws.ecs.task.family": "elspeth-web-task",
        "aws.ecs.task.revision": "42",
    }


def test_aws_bootstrap_rejects_provider_without_meter_contract() -> None:
    factories = _factories(_FactoryRecord())
    factories.provider_factory = object()
    with pytest.raises(TypeError, match="nominal"):
        _bootstrap(_aws_settings(), factories=factories)
    assert factories.owner.provider is None
    assert factories.owner.witness is not None


def test_aws_bootstrap_rejects_exporter_outside_factory_contract() -> None:
    factories = _factories(_FactoryRecord())
    factories.exporter_factory = lambda **kwargs: object()
    with pytest.raises(BaseExceptionGroup) as failure:
        _bootstrap(_aws_settings(), factories=factories)
    assert factories.owner.provider is None

    assert isinstance(failure.value.exceptions[0], TypeError)
    assert isinstance(failure.value.exceptions[1], TelemetryCustodyUnresolved)
    assert factories.owner.witness is None


@pytest.mark.parametrize(
    ("field", "raw_value"),
    [
        ("operator_telemetry_service_name", "arn:aws:ecs:ap-southeast-2:123456789012:service/elspeth-web"),
        ("operator_telemetry_service_name", "123456789012"),
        ("operator_telemetry_service_name", "elspeth-123456789012-web"),
        ("operator_telemetry_environment", "arn:aws:ecs:ap-southeast-2:123456789012:cluster/production"),
        ("operator_telemetry_environment", "123456789012"),
        ("operator_telemetry_environment", "prod-123456789012-blue"),
    ],
)
def test_aws_bootstrap_defensively_rejects_unvalidated_resource_labels(field: str, raw_value: str) -> None:
    with pytest.raises(ValueError, match=field) as caught:
        _bootstrap(_aws_settings().model_copy(update={field: raw_value}), factories=_factories(_FactoryRecord()))
    assert raw_value not in str(caught.value)


def test_pipeline_exporter_failures_are_excluded_from_operator_queue_drop_gauge() -> None:
    record = _FactoryRecord()
    runtime = _bootstrap(_aws_settings(), factories=_factories(record))
    exporter = record.exporters[0]
    assert _gauge_value(runtime, exporter, "operator.telemetry.queue_drops") == 0
    manager = TelemetryManager(MockTelemetryConfig(), exporters=[FailingExporter()])
    try:
        manager.handle_event(
            RunStarted(timestamp=datetime(2026, 7, 14, tzinfo=UTC), run_id="run-dropped", config_hash="config-hash", source_plugin="text")
        )
        manager.flush()
        assert manager.health_metrics["events_dropped"] == 1
        assert manager.health_metrics["queue_drops"] == 0
        record_operator_pipeline_queue_drops(manager.health_metrics["queue_drops"])
        assert _gauge_value(runtime, exporter, "operator.telemetry.queue_drops") == 0
    finally:
        manager.close()


def test_pipeline_queue_drop_fact_is_observed_by_operator_queue_drop_gauge() -> None:
    record = _FactoryRecord()
    runtime = _bootstrap(_aws_settings(), factories=_factories(record))
    record_operator_pipeline_queue_drops(1)
    assert _gauge_value(runtime, record.exporters[0], "operator.telemetry.queue_drops") == 1


@pytest.mark.asyncio
async def test_shutdown_is_bounded_and_once_only(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _FactoryRecord()
    runtime = _bootstrap(_aws_settings(), factories=_factories(record))
    provider = runtime.provider
    calls: list[float] = []
    shutdown = provider.shutdown

    def observed(timeout_millis: float = 30_000) -> None:
        calls.append(timeout_millis)
        shutdown(timeout_millis=timeout_millis)

    monkeypatch.setattr(provider, "shutdown", observed)
    await runtime.shutdown()
    await runtime.shutdown()
    assert calls == [5_000]
    assert runtime.cleanup_owner.witness is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown_error", [TimeoutError(), ConnectionError("collector unavailable")])
async def test_shutdown_failure_is_handled_once(shutdown_error: BaseException, monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep projection-pointer restoration scoped before publishing this
    # deliberately failed isolated domain; its authority record is retained.
    monkeypatch.setattr(operator_telemetry, "_runtime", None)
    record = _FactoryRecord()
    runtime = _bootstrap(_aws_settings(), factories=_factories(record))
    calls: list[float] = []

    def fail(timeout_millis: float = 30_000) -> None:
        calls.append(timeout_millis)
        raise shutdown_error

    monkeypatch.setattr(runtime.provider, "shutdown", fail)
    # This negative owns an isolated retained failed domain, never a reset.
    monkeypatch.setattr(operator_telemetry, "_runtime", None)
    for _ in range(2):
        with pytest.raises(type(shutdown_error)) as caught:
            await runtime.shutdown()
        assert caught.value is shutdown_error
    assert calls == [5_000]
    assert runtime.cleanup_owner.witness is None
    assert all(reader.cleanup.finished for reader in runtime.cleanup_owner.readers)
    assert all(
        not reader._daemon_thread.is_alive()
        for reader in runtime.cleanup_owner.readers
        if isinstance(reader, OwnedPeriodicExportingMetricReader)
    )


@pytest.mark.asyncio
async def test_shutdown_finishes_on_calling_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _bootstrap(_aws_settings(), factories=_factories(_FactoryRecord()))
    threads: list[int] = []
    shutdown = runtime.provider.shutdown

    def observed(timeout_millis: float = 30_000) -> None:
        threads.append(threading.get_ident())
        shutdown(timeout_millis=timeout_millis)

    monkeypatch.setattr(runtime.provider, "shutdown", observed)
    calling_thread = threading.get_ident()
    await runtime.shutdown()
    assert threads == [calling_thread]
    assert runtime._shutdown_complete is True


def test_reset_shuts_down_provider_before_forgetting_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _bootstrap(_web_settings(), factories=_factories(_FactoryRecord()))
    observed: list[object] = []
    shutdown = runtime.provider.shutdown

    def check(timeout_millis: float = 30_000) -> None:
        observed.append(operator_telemetry._runtime)
        shutdown(timeout_millis=timeout_millis)

    monkeypatch.setattr(runtime.provider, "shutdown", check)
    reset_operator_telemetry_for_tests()
    assert observed == [runtime]
    assert runtime.cleanup_owner.witness is not None
    replacement = _bootstrap(_web_settings(), factories=_factories(_FactoryRecord()))
    assert replacement is not runtime


def test_reset_does_not_shutdown_process_global_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _bootstrap(_web_settings(), factories=_factories(_FactoryRecord()))
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: runtime.provider)
    with pytest.raises(TelemetryCustodyUnresolved, match="reset"):
        reset_operator_telemetry_for_tests()
    # No domain is forgotten, even after physical cleanup was completed.
    assert operator_telemetry._runtime is runtime
    assert runtime.cleanup_owner.installation.record is not None
    monkeypatch.undo()
