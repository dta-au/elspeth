"""Focused contracts for AWS operator metrics projected from audited events."""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    InMemoryMetricReader,
    MetricExporter,
    MetricExportResult,
    MetricsData,
)

from elspeth.contracts.enums import CallStatus, CallType, RunStatus, TelemetryGranularity
from elspeth.contracts.events import ExternalCallCompleted, RunFinished
from elspeth.contracts.token_usage import TokenUsage
from elspeth.telemetry.manager import TelemetryManager
from elspeth.web import operator_telemetry
from elspeth.web.config import WebSettings
from elspeth.web.operator_telemetry import OperatorTelemetryFactories
from tests.fixtures.telemetry import MockTelemetryConfig, TelemetryTestExporter

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _reset_operator_runtime() -> Iterator[None]:
    """Keep the process-global runtime isolated from other tests on this worker."""
    operator_telemetry.reset_operator_telemetry_for_tests()
    yield
    operator_telemetry.reset_operator_telemetry_for_tests()


class _NoopMetricExporter(MetricExporter):
    def export(
        self,
        _metrics_data: MetricsData,
        timeout_millis: float = 10_000,
        **_kwargs: object,
    ) -> MetricExportResult:
        del timeout_millis
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        del timeout_millis
        return True

    def shutdown(self, timeout_millis: float = 30_000, **_kwargs: object) -> None:
        del timeout_millis


def _aws_settings() -> WebSettings:
    return WebSettings(
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        deployment_target="aws-ecs",
        operator_telemetry="aws-otlp",
        operator_telemetry_environment="production",
        operator_telemetry_release="git-deadbeef",
        operator_telemetry_ecs_cluster="elspeth-production",
        operator_telemetry_ecs_service="elspeth-web",
        operator_telemetry_task_definition_family="elspeth-web-task",
        operator_telemetry_task_definition_revision="42",
    )


def _metric_factories(reader: InMemoryMetricReader) -> OperatorTelemetryFactories:
    secondary_reader = InMemoryMetricReader()

    def provider(
        readers: Sequence[object],
        *,
        resource: object,
        views: tuple[object, ...],
    ) -> MeterProvider:
        del views
        return MeterProvider(
            metric_readers=readers,  # type: ignore[arg-type]
            resource=resource,  # type: ignore[arg-type]
            shutdown_on_exit=False,
        )

    return OperatorTelemetryFactories(
        prometheus_reader=lambda: reader,
        otlp_exporter=lambda **_kwargs: _NoopMetricExporter(),
        periodic_reader=lambda _exporter, **_kwargs: secondary_reader,
        meter_provider=provider,
        set_meter_provider=lambda _provider: None,
    )


def _metrics_by_name(reader: InMemoryMetricReader) -> dict[str, Any]:
    data = reader.get_metrics_data()
    return {
        metric.name: metric
        for resource_metric in data.resource_metrics
        for scope_metric in resource_metric.scope_metrics
        for metric in scope_metric.metrics
    }


def _single_point_value(metric: Any) -> int | float:
    [point] = metric.data.data_points
    value = getattr(point, "value", None)
    return point.sum if value is None else value


def _run_finished() -> RunFinished:
    return RunFinished(
        timestamp=datetime(2026, 7, 30, tzinfo=UTC),
        run_id="sensitive-run-id",
        status=RunStatus.FAILED,
        row_count=3,
        duration_ms=301_000,
    )


def _external_call(
    *,
    call_type: CallType,
    status: CallStatus,
    latency_ms: float | None,
    token_usage: TokenUsage | None = None,
) -> ExternalCallCompleted:
    return ExternalCallCompleted(
        timestamp=datetime(2026, 7, 30, tzinfo=UTC),
        run_id="sensitive-run-id",
        state_id="sensitive-state-id",
        operation_id=None,
        token_id="sensitive-token-id",
        call_type=call_type,
        provider="high-cardinality-provider",
        status=status,
        latency_ms=latency_ms,
        token_usage=token_usage,
    )


def test_aws_operator_projects_audited_run_call_and_token_metrics_without_dimensions() -> None:
    record_event = getattr(operator_telemetry, "record_operator_pipeline_event", None)
    metric_names = getattr(operator_telemetry, "AWS_OPERATOR_PIPELINE_METRIC_NAMES", frozenset())
    assert callable(record_event), "AWS operator pipeline event projection is not implemented"
    assert metric_names == {
        "run.failure",
        "run.duration",
        "external_call.failure",
        "external_call.latency",
        "llm.prompt_tokens",
        "llm.completion_tokens",
    }

    reader = InMemoryMetricReader()
    runtime = operator_telemetry.bootstrap_operator_telemetry(
        _aws_settings(),
        factories=_metric_factories(reader),
    )
    try:
        record_event(_run_finished())
        record_event(
            _external_call(
                call_type=CallType.HTTP,
                status=CallStatus.ERROR,
                latency_ms=31_000,
            )
        )
        record_event(
            _external_call(
                call_type=CallType.LLM,
                status=CallStatus.SUCCESS,
                latency_ms=None,
                token_usage=TokenUsage(prompt_tokens=11, completion_tokens=7),
            )
        )

        collected = _metrics_by_name(reader)

        assert _single_point_value(collected["run.failure"]) == 1
        assert _single_point_value(collected["run.duration"]) == 301
        assert _single_point_value(collected["external_call.failure"]) == 1
        assert _single_point_value(collected["external_call.latency"]) == 31
        assert _single_point_value(collected["llm.prompt_tokens"]) == 11
        assert _single_point_value(collected["llm.completion_tokens"]) == 7
        assert "llm.cost" not in collected
        for name in metric_names:
            for point in collected[name].data.data_points:
                assert point.attributes == {}
    finally:
        runtime.provider.shutdown()
        operator_telemetry.reset_operator_telemetry_for_tests()


def test_pipeline_metric_observer_runs_before_lifecycle_granularity_filter() -> None:
    assert "event_observers" in inspect.signature(TelemetryManager).parameters
    observed: list[object] = []
    exporter = TelemetryTestExporter()
    manager = TelemetryManager(
        MockTelemetryConfig(granularity=TelemetryGranularity.LIFECYCLE),
        exporters=[exporter],
        event_observers=[observed.append],
    )
    event = _external_call(
        call_type=CallType.HTTP,
        status=CallStatus.ERROR,
        latency_ms=31_000,
    )
    try:
        manager.handle_event(event)
        manager.flush()

        assert observed == [event]
        assert exporter.events == []
    finally:
        manager.close()


def test_llm_token_projection_retains_partial_provider_usage_without_fabrication() -> None:
    record_event = getattr(operator_telemetry, "record_operator_pipeline_event", None)
    assert callable(record_event), "AWS operator pipeline event projection is not implemented"

    reader = InMemoryMetricReader()
    runtime = operator_telemetry.bootstrap_operator_telemetry(
        _aws_settings(),
        factories=_metric_factories(reader),
    )
    try:
        record_event(
            _external_call(
                call_type=CallType.LLM,
                status=CallStatus.SUCCESS,
                latency_ms=None,
                token_usage=TokenUsage(prompt_tokens=None, completion_tokens=3),
            )
        )

        collected = _metrics_by_name(reader)

        assert "llm.prompt_tokens" not in collected
        assert _single_point_value(collected["llm.completion_tokens"]) == 3
    finally:
        runtime.provider.shutdown()
        operator_telemetry.reset_operator_telemetry_for_tests()


def test_aws_web_execution_wires_the_post_audit_metric_observer() -> None:
    execution_service = (REPO_ROOT / "src/elspeth/web/execution/service.py").read_text(encoding="utf-8")

    assert "record_operator_pipeline_event" in execution_service
    assert re.search(
        r"create_telemetry_manager\(\s*telemetry_config,\s*"
        r"event_observers=\(record_operator_pipeline_event,\),\s*\)",
        execution_service,
    )


def test_operator_metric_projection_failure_cannot_replace_the_audited_outcome() -> None:
    record_event = getattr(operator_telemetry, "record_operator_pipeline_event", None)
    assert callable(record_event), "AWS operator pipeline event projection is not implemented"

    class _FailingRecorder:
        def record(self, _event: object) -> None:
            raise RuntimeError("metric recorder unavailable")

    reader = InMemoryMetricReader()
    runtime = operator_telemetry.bootstrap_operator_telemetry(
        _aws_settings(),
        factories=_metric_factories(reader),
    )
    try:
        runtime.pipeline_metrics = _FailingRecorder()  # type: ignore[assignment]

        assert record_event(_run_finished()) is None
    finally:
        runtime.provider.shutdown()
        operator_telemetry.reset_operator_telemetry_for_tests()
