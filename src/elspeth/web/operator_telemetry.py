"""Process-wide web telemetry and AWS ECS pipeline telemetry policy.

Landscape remains the authoritative run record.  This module owns only the
best-effort operational signal path: Prometheus for every web process and a
fixed task-local OTLP metric reader plus pipeline overlay in AWS ECS mode.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import structlog
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, MetricsData
from opentelemetry.sdk.resources import Resource
from opentelemetry.util.types import Attributes

from elspeth import __version__
from elspeth.contracts.enums import CallStatus, CallType, RunStatus
from elspeth.contracts.events import ExternalCallCompleted, RunFinished, TelemetryEvent
from elspeth.core.config import ElspethSettings, ExporterSettings, TelemetrySettings
from elspeth.telemetry.errors import TELEMETRY_TRANSPORT_ERRORS
from elspeth.telemetry.resource_identity import is_aws_resource_label
from elspeth.web.config import WebSettings
from elspeth.web.operator_telemetry_custody import (
    OperatorTelemetryCleanupOwner,
    OwnedMetricExporter,
    OwnedProviderFactory,
    RawMetricExporterCustodian,
    TelemetryCompletionWitness,
    TelemetryCustodyUnresolved,
    verify_telemetry_sdk,
)
from elspeth.web.operator_telemetry_dispatch import validate_telemetry_reservation_owner
from elspeth.web.operator_telemetry_installation import OwnedTestTelemetryInstallation

AWS_OTLP_ENDPOINT = "http://127.0.0.1:4317"
_EXPORT_TIMEOUT_MILLIS = 5_000

AWS_OPERATOR_PIPELINE_METRIC_NAMES: frozenset[str] = frozenset(
    {
        "run.failure",
        "run.duration",
        "external_call.failure",
        "external_call.latency",
        "llm.prompt_tokens",
        "llm.completion_tokens",
    }
)

# The task-local exporter removes every other point attribute before OTLP.
# Resource identity is configured separately and is similarly bounded by
# WebSettings.  The Prometheus reader receives the existing attribute sets so
# its authenticated exposition remains backwards-compatible.
SAFE_CLOUDWATCH_METRIC_ATTRIBUTES: frozenset[str] = frozenset(
    {
        # Preflight verdict split (elspeth-ca0bd5d4ef): closed vocab
        # (valid|invalid|pending_review), bool, and an int the repair loop
        # caps at _MAX_REPAIR_TURNS — all bounded, so the CloudWatch lane
        # may carry them alongside the unconditional Prometheus reader.
        "budget_exhausted",
        "cap_type",
        "completion_path",
        "completion_verb",
        "component_type",
        "elspeth.acceptance.namespace",
        "elspeth.acceptance.sentinel",
        "failure_class",
        "from_mode",
        "kind",
        "operation",
        "outcome",
        "probe_status",
        "reason",
        "repair_turns_used",
        "result",
        "source",
        "status",
        "surface",
        "to_mode",
        "verdict",
    }
)

_log = structlog.get_logger(__name__)


class _Provider(Protocol):
    def get_meter(self, name: str, version: str) -> Any: ...

    def force_flush(self, timeout_millis: float = 10_000) -> bool: ...

    def shutdown(self, timeout_millis: float = 30_000) -> None: ...


@dataclass(frozen=True, slots=True)
class _OperatorPipelineMetrics:
    run_failure: Any
    run_duration: Any
    external_call_failure: Any
    external_call_latency: Any
    llm_prompt_tokens: Any
    llm_completion_tokens: Any

    def record(self, event: TelemetryEvent) -> None:
        """Project only bounded aggregate facts from an already-audited event."""

        if isinstance(event, RunFinished):
            self.run_duration.record(event.duration_ms / 1_000)
            if event.status in {
                RunStatus.COMPLETED_WITH_FAILURES,
                RunStatus.FAILED,
                RunStatus.INTERRUPTED,
            }:
                self.run_failure.add(1)
            return

        if not isinstance(event, ExternalCallCompleted):
            return
        if event.latency_ms is not None:
            self.external_call_latency.record(event.latency_ms / 1_000)
        if event.status is CallStatus.ERROR:
            self.external_call_failure.add(1)
        if event.call_type is not CallType.LLM or event.token_usage is None:
            return
        if event.token_usage.prompt_tokens is not None:
            self.llm_prompt_tokens.add(event.token_usage.prompt_tokens)
        if event.token_usage.completion_tokens is not None:
            self.llm_completion_tokens.add(event.token_usage.completion_tokens)


class OperatorTelemetryFactories:
    """Nominal production construction; providers have explicit no-atexit ABI."""

    def __init__(self, *, provider_factory: OwnedProviderFactory | None = None) -> None:
        selected = OwnedProviderFactory() if provider_factory is None else provider_factory
        if not isinstance(selected, OwnedProviderFactory):
            raise TypeError("Telemetry provider factory must be nominal")
        self.provider_factory = selected

    def acquire_exporter(self, *, endpoint: str, insecure: bool, headers: dict[str, str], timeout: float) -> MetricExporter:
        return OTLPMetricExporter(endpoint=endpoint, insecure=insecure, headers=headers, timeout=timeout)


class OwnedTestOperatorTelemetryFactories(OperatorTelemetryFactories):
    """Explicit no-network exporter seam for isolated nominal test domains."""

    def __init__(self, *, exporter_factory: Callable[..., MetricExporter], provider_factory: OwnedProviderFactory | None = None) -> None:
        super().__init__(provider_factory=provider_factory)
        self.exporter_factory = exporter_factory

    def acquire_exporter(self, *, endpoint: str, insecure: bool, headers: dict[str, str], timeout: float) -> MetricExporter:
        return self.exporter_factory(endpoint=endpoint, insecure=insecure, headers=headers, timeout=timeout)


def _production_factories() -> OperatorTelemetryFactories:
    return OperatorTelemetryFactories()


@dataclass(slots=True)
class _ExportHealth:
    attempted: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_success_monotonic: float | None = None
    _queue_drops: int = 0
    _queue_drops_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def success(self) -> None:
        self.attempted += 1
        self.consecutive_failures = 0
        self.last_success_monotonic = time.monotonic()

    def failure(self) -> None:
        self.attempted += 1
        self.failures += 1
        self.consecutive_failures += 1

    @property
    def queue_drops(self) -> int:
        with self._queue_drops_lock:
            return self._queue_drops

    def record_queue_drops(self, count: int) -> None:
        with self._queue_drops_lock:
            self._queue_drops += count


def _safe_point_attributes(attributes: Attributes) -> Attributes:
    if attributes is None:
        return None
    return {key: value for key, value in attributes.items() if key in SAFE_CLOUDWATCH_METRIC_ATTRIBUTES}


def _sanitize_metric_data(metrics_data: MetricsData) -> MetricsData:
    """Copy a collection with CloudWatch-safe point attributes and no exemplars."""

    resource_metrics = []
    for resource_metric in metrics_data.resource_metrics:
        scope_metrics = []
        for scope_metric in resource_metric.scope_metrics:
            sanitized_metrics = []
            for metric in scope_metric.metrics:
                data = metric.data
                sanitized_points = []
                for point in data.data_points:
                    sanitized_points.append(
                        dataclasses.replace(
                            point,
                            attributes=_safe_point_attributes(point.attributes),
                            exemplars=(),
                        )
                    )
                sanitized_data = dataclasses.replace(
                    cast(Any, data),
                    data_points=tuple(sanitized_points),
                )
                sanitized_metrics.append(dataclasses.replace(metric, data=sanitized_data))
            scope_metrics.append(dataclasses.replace(scope_metric, metrics=tuple(sanitized_metrics)))
        resource_metrics.append(dataclasses.replace(resource_metric, scope_metrics=tuple(scope_metrics)))
    return MetricsData(resource_metrics=tuple(resource_metrics))


class _HealthTrackingMetricExporter(OwnedMetricExporter):
    """Sanitize AWS-bound dimensions and retain aggregate exporter health."""

    def __init__(self, custody: RawMetricExporterCustodian, health: _ExportHealth) -> None:
        super().__init__(custody)
        self._health = health

    def _record_transport_failure(self) -> None:
        self._health.failure()
        count = self._health.consecutive_failures
        # Aggregate logs at powers of two.  The metric callback exposes the
        # exact count without recursively recording from inside export().
        if count & (count - 1) == 0:
            _log.warning(
                "operator_otlp_export_unavailable",
                consecutive_failures=count,
                destination="task-local",
            )

    def export(self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: object) -> MetricExportResult:
        try:
            result = self._inner.export(_sanitize_metric_data(metrics_data), timeout_millis=timeout_millis, **kwargs)
        except TELEMETRY_TRANSPORT_ERRORS:
            self._record_transport_failure()
            return MetricExportResult.FAILURE
        if result is MetricExportResult.SUCCESS:
            self._health.success()
        else:
            self._record_transport_failure()
        return result

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        try:
            return self._inner.force_flush(timeout_millis=timeout_millis)
        except TELEMETRY_TRANSPORT_ERRORS:
            self._record_transport_failure()
            return False

    def shutdown(self, timeout_millis: float = 30_000, *, timeout: float | None = None, **kwargs: object) -> None:
        try:
            super().shutdown(timeout_millis=timeout_millis, timeout=timeout, **kwargs)
        except TELEMETRY_TRANSPORT_ERRORS as original:
            try:
                self._record_transport_failure()
            except BaseException as logging_error:
                raise BaseExceptionGroup("Exporter shutdown and health recording failed", [original, logging_error]) from None
            raise


@dataclass(slots=True)
class OperatorTelemetryRuntime:
    """Retained process provider plus bounded, once-only shutdown."""

    mode: str
    provider: _Provider
    readers: tuple[Any, ...]
    resource: Resource
    health: _ExportHealth
    cleanup_owner: OperatorTelemetryCleanupOwner
    pipeline_metrics: _OperatorPipelineMetrics | None = None
    _shutdown_state_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _shutdown_started: bool = False
    _shutdown_complete: bool = False

    def _begin_shutdown(self) -> bool:
        with self._shutdown_state_lock:
            if self._shutdown_started or self._shutdown_complete:
                return False
            self._shutdown_started = True
            return True

    def _finish_shutdown(self) -> None:
        with self._shutdown_state_lock:
            self._shutdown_complete = True

    def shutdown_sync(self) -> TelemetryCompletionWitness:
        self.cleanup_owner.assert_process()
        self._begin_shutdown()
        witness = self.cleanup_owner.shutdown_sync()
        self._finish_shutdown()
        return witness

    async def shutdown(self) -> None:
        # The independently armed process watchdog bounds physical joins.
        # The async ABI deliberately owns synchronous SDK completion here.
        self.shutdown_sync()


_runtime: OperatorTelemetryRuntime | None = None
_runtime_lock = threading.Lock()


def _required_aws_resource_identity(settings: WebSettings) -> dict[str, str]:
    for field_name, value in (
        ("operator_telemetry_service_name", settings.operator_telemetry_service_name),
        ("operator_telemetry_environment", settings.operator_telemetry_environment),
    ):
        if value is not None and not is_aws_resource_label(value):
            raise ValueError(f"{field_name} must be a bounded AWS-safe resource label without ARN or account identity")
    configured = {
        "service.version": settings.operator_telemetry_release,
        "aws.ecs.cluster.name": settings.operator_telemetry_ecs_cluster,
        "aws.ecs.service.name": settings.operator_telemetry_ecs_service,
        "aws.ecs.task.family": settings.operator_telemetry_task_definition_family,
        "aws.ecs.task.revision": settings.operator_telemetry_task_definition_revision,
    }
    if any(value is None for value in configured.values()):
        raise ValueError("validated AWS ECS operator telemetry identity is incomplete")
    return {key: cast(str, value) for key, value in configured.items()}


def _wire_health_instruments(provider: _Provider, health: _ExportHealth) -> None:
    meter = provider.get_meter("elspeth.web.operator_telemetry", __version__)
    meter.create_observable_gauge(
        "operator.telemetry.last_success_age_seconds",
        callbacks=[
            lambda _options: [
                Observation(-1.0 if health.last_success_monotonic is None else max(0.0, time.monotonic() - health.last_success_monotonic))
            ]
        ],
        description="Age of the last successful task-local OTLP metric export; -1 means never exported.",
        unit="s",
    )
    meter.create_observable_gauge(
        "operator.telemetry.export_failures",
        callbacks=[lambda _options: [Observation(health.failures)]],
        description="Cumulative task-local OTLP metric export failures.",
    )
    meter.create_observable_gauge(
        "operator.telemetry.queue_drops",
        callbacks=[lambda _options: [Observation(health.queue_drops)]],
        description="Cumulative pipeline telemetry enqueue and backpressure losses.",
    )
    meter.create_observable_gauge(
        "operator.telemetry.collector_unavailable",
        callbacks=[lambda _options: [Observation(1 if health.consecutive_failures else 0)]],
        description="One while the task-local OTLP collector has consecutive failures.",
    )


def _wire_pipeline_instruments(provider: _Provider) -> _OperatorPipelineMetrics:
    meter = provider.get_meter("elspeth.web.operator_pipeline", __version__)
    return _OperatorPipelineMetrics(
        run_failure=meter.create_counter(
            "run.failure",
            description="Audited pipeline runs with a non-clean terminal status.",
        ),
        run_duration=meter.create_histogram(
            "run.duration",
            description="Audited pipeline run duration.",
            unit="s",
        ),
        external_call_failure=meter.create_counter(
            "external_call.failure",
            description="Audited external calls that completed with an error.",
        ),
        external_call_latency=meter.create_histogram(
            "external_call.latency",
            description="Audited external-call latency.",
            unit="s",
        ),
        llm_prompt_tokens=meter.create_counter(
            "llm.prompt_tokens",
            description="Provider-reported audited LLM prompt tokens.",
            unit="{token}",
        ),
        llm_completion_tokens=meter.create_counter(
            "llm.completion_tokens",
            description="Provider-reported audited LLM completion tokens.",
            unit="{token}",
        ),
    )


def record_operator_pipeline_queue_drops(count: int) -> None:
    """Add one completed pipeline manager's queue-only losses to operator health."""

    if type(count) is not int or count < 0:
        raise ValueError("operator pipeline queue drop count must be a non-negative integer")
    with _runtime_lock:
        runtime = _runtime
    if runtime is None or runtime.mode != "aws-otlp" or count == 0:
        return
    runtime.health.record_queue_drops(count)


def bootstrap_operator_telemetry(
    settings: WebSettings,
    *,
    cleanup_owner: OperatorTelemetryCleanupOwner,
    factories: OperatorTelemetryFactories | None = None,
) -> OperatorTelemetryRuntime:
    """Install only the exact lexical application owner's retained provider."""

    global _runtime
    installation = validate_telemetry_reservation_owner(cleanup_owner)
    cleanup_owner.assert_process()
    existing = installation.reserve(cleanup_owner)
    if existing is not None:
        return existing
    selected = _production_factories() if factories is None else factories
    if not isinstance(selected, OperatorTelemetryFactories):
        raise TypeError("Telemetry construction factories must be nominal")
    if isinstance(selected, OwnedTestOperatorTelemetryFactories) and not isinstance(installation, OwnedTestTelemetryInstallation):
        raise TypeError("Test telemetry factories require an isolated test installation")
    verify_telemetry_sdk()
    resource_attributes: dict[str, str] = {
        "service.name": settings.operator_telemetry_service_name,
        "service.version": __version__,
    }
    if settings.operator_telemetry_environment is not None:
        resource_attributes["deployment.environment"] = settings.operator_telemetry_environment
    if settings.operator_telemetry == "aws-otlp":
        resource_attributes["cloud.provider"] = "aws"
        resource_attributes.update(_required_aws_resource_identity(settings))
    resource = Resource(resource_attributes)
    cleanup_owner.acquire_prometheus(installation.registry)
    health = _ExportHealth()
    if settings.operator_telemetry == "aws-otlp":
        try:
            raw_exporter = selected.acquire_exporter(
                endpoint=AWS_OTLP_ENDPOINT,
                insecure=True,
                headers={},
                timeout=_EXPORT_TIMEOUT_MILLIS / 1_000,
            )
            custody = cleanup_owner.retain_exporter(raw_exporter)
        except BaseException as original:
            cleanup_owner.exporter_acquisition_unknown(original)
            raise
        exporter = _HealthTrackingMetricExporter(custody, health)
        cleanup_owner.acquire_periodic(
            exporter,
            interval_millis=settings.operator_telemetry_export_interval_seconds * 1_000,
            timeout_millis=_EXPORT_TIMEOUT_MILLIS,
        )
    provider = cleanup_owner.acquire_provider(selected.provider_factory, resource=resource, views=())
    pipeline_metrics = None
    if settings.operator_telemetry == "aws-otlp":
        _wire_health_instruments(provider, health)
        pipeline_metrics = _wire_pipeline_instruments(provider)
    installation.install(cleanup_owner, provider)
    runtime = OperatorTelemetryRuntime(
        mode=settings.operator_telemetry,
        provider=provider,
        readers=tuple(cleanup_owner.readers),
        resource=resource,
        health=health,
        cleanup_owner=cleanup_owner,
        pipeline_metrics=pipeline_metrics,
    )
    installation.publish(cleanup_owner, runtime)
    with _runtime_lock:
        _runtime = runtime
    return runtime


def record_operator_pipeline_event(event: TelemetryEvent) -> None:
    """Best-effort projection of an already-audited event into AWS metrics."""

    with _runtime_lock:
        runtime = _runtime
    if runtime is None or runtime.mode != "aws-otlp" or runtime.pipeline_metrics is None:
        return
    try:
        runtime.pipeline_metrics.record(event)
    except Exception as exc:
        # Operational metrics are subordinate to Landscape. Metric SDK or
        # recorder failure must not replace the already-audited run/call
        # outcome; retain only bounded class/type facts in the fallback log.
        _log.error(
            "operator_pipeline_metric_projection_failed",
            event_type=type(event).__name__,
            error_type=type(exc).__name__,
        )


def build_aws_operator_pipeline_telemetry(web_settings: WebSettings) -> TelemetrySettings:
    """Build the pure, fixed AWS ECS pipeline telemetry policy."""

    if web_settings.deployment_target != "aws-ecs":
        raise ValueError("AWS operator pipeline telemetry requires AWS ECS settings")
    identity = _required_aws_resource_identity(web_settings)
    return TelemetrySettings(
        enabled=True,
        granularity=web_settings.operator_pipeline_telemetry_granularity,
        backpressure_mode="drop",
        fail_on_total_exporter_failure=False,
        exporters=[
            ExporterSettings(
                name="otlp",
                options={
                    "endpoint": AWS_OTLP_ENDPOINT,
                    "headers": {},
                    "service_name": web_settings.operator_telemetry_service_name,
                    "service_version": identity["service.version"],
                    "deployment_environment": web_settings.operator_telemetry_environment,
                    "cloud_provider": "aws",
                    "aws_ecs_cluster_name": identity["aws.ecs.cluster.name"],
                    "aws_ecs_service_name": identity["aws.ecs.service.name"],
                    "aws_ecs_task_family": identity["aws.ecs.task.family"],
                    "aws_ecs_task_revision": identity["aws.ecs.task.revision"],
                    "batch_size": 100,
                },
            )
        ],
    )


def apply_operator_pipeline_telemetry(settings: ElspethSettings, web_settings: WebSettings) -> ElspethSettings:
    """Replace web-authored routing with the fixed AWS operator policy."""

    if web_settings.deployment_target != "aws-ecs":
        return settings
    effective = build_aws_operator_pipeline_telemetry(web_settings)
    return settings.model_copy(update={"telemetry": effective})


def reset_operator_telemetry_for_tests() -> None:
    """Reset only a nominal isolated domain after actual successful cleanup."""
    global _runtime
    with _runtime_lock:
        runtime = _runtime
    if runtime is None:
        return
    owner = runtime.cleanup_owner
    owner.assert_process()
    installation = owner.installation
    if not isinstance(installation, OwnedTestTelemetryInstallation):
        raise TelemetryCustodyUnresolved("Production telemetry installation cannot be reset")
    witness = runtime.shutdown_sync()
    installation.reset(owner, witness, installation.replaceability_token)
    with _runtime_lock:
        if _runtime is not runtime:
            raise TelemetryCustodyUnresolved("Telemetry reset lost exact runtime identity")
        _runtime = None


__all__ = [
    "AWS_OPERATOR_PIPELINE_METRIC_NAMES",
    "AWS_OTLP_ENDPOINT",
    "SAFE_CLOUDWATCH_METRIC_ATTRIBUTES",
    "OperatorTelemetryFactories",
    "OperatorTelemetryRuntime",
    "OwnedTestOperatorTelemetryFactories",
    "apply_operator_pipeline_telemetry",
    "bootstrap_operator_telemetry",
    "build_aws_operator_pipeline_telemetry",
    "record_operator_pipeline_event",
    "record_operator_pipeline_queue_drops",
    "reset_operator_telemetry_for_tests",
]
