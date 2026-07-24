"""Focused contracts for the operator-telemetry acceptance owner."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from elspeth.web import aws_ecs_acceptance as acceptance
from elspeth.web._aws_ecs_acceptance import operator_telemetry
from tests.unit.web.aws_ecs_acceptance.test_manifest_schema_inventory import _init_control_manifest

PUBLIC_OPERATOR_TELEMETRY_EXPORTS = {
    "AWSOperatorMetricEmitter",
    "AWSOperatorTelemetryQueries",
    "AcceptancePolicy",
    "AuditSentinel",
    "ExistingLandscapeLifecycleAudit",
    "OperatorTelemetryEvidence",
    "OperatorTelemetryOutageEvidence",
    "PublicApiLifecycleAudit",
    "TelemetryQueries",
    "TelemetrySentinelEmitter",
    "operator_metric_dimensions",
    "verify_connection_budget_live",
    "verify_operator_telemetry",
    "verify_operator_telemetry_live",
    "verify_operator_telemetry_outage",
    "xray_trace_id",
}


def test_positive_operator_receipt_creates_and_binds_exact_retained_checkpoint(tmp_path: Path) -> None:
    run_id = "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48"
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_retained=False)
    sentinel = "checkpoint-positive-sentinel"
    sentinel_value = int(hashlib.sha256(sentinel.encode()).hexdigest()[:12], 16)
    trace_id = acceptance.xray_trace_id("landscape-run-internal", started_at=_TELEMETRY_STARTED_AT)

    class CloudWatch:
        def get_metric_data(self, **_kwargs: object) -> object:
            return {
                "MetricDataResults": [
                    {
                        "Id": "acceptance",
                        "StatusCode": "Complete",
                        "Timestamps": [datetime(2026, 7, 14, 1, 4, tzinfo=UTC)],
                        "Values": [float(sentinel_value)],
                    }
                ]
            }

        def close(self) -> None:
            pass

    class XRay:
        def batch_get_traces(self, **_kwargs: object) -> object:
            return {
                "Traces": [
                    {
                        "Id": trace_id,
                        "Segments": [
                            {"Document": json.dumps({"name": "RunStarted", "annotations": {"run_id": "landscape-run-internal"}})},
                            {
                                "Document": json.dumps(
                                    {
                                        "name": "RunFinished",
                                        "annotations": {"run_id": "landscape-run-internal", "status": "completed"},
                                    }
                                )
                            },
                        ],
                    }
                ],
                "UnprocessedTraceIds": [],
            }

        def close(self) -> None:
            pass

    settings = SimpleNamespace(
        deployment_target="aws-ecs",
        operator_telemetry="aws-otlp",
        operator_pipeline_telemetry_granularity="lifecycle",
        operator_telemetry_service_name="elspeth-web",
        operator_telemetry_environment="acceptance",
        operator_telemetry_release="0.7.1",
        operator_telemetry_ecs_cluster="cluster-a",
        operator_telemetry_ecs_service="service-a",
        operator_telemetry_task_definition_family="elspeth-web",
        operator_telemetry_task_definition_revision="17",
    )
    details = acceptance.verify_operator_telemetry_live(
        {
            "AWS_REGION": "ap-southeast-2",
            "ELSPETH_ACCEPTANCE_RUN_ID": run_id,
            "ELSPETH_ACCEPTANCE_SCENARIO_ID": "A",
        },
        phase="positive",
        settings_loader=lambda: settings,
        audit_factory=lambda _settings, _env: _TelemetryAudit([]),
        emitter_factory=lambda _settings: _TelemetryEmitter([]),
        aws_client_factory=lambda service, _region: CloudWatch() if service == "cloudwatch" else XRay(),
        policy=acceptance.AcceptancePolicy(attempts=1, interval_seconds=0),
        sentinel_factory=lambda: sentinel,
        now_datetime=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
        now_epoch=lambda: 1234.5,
    )
    exec_receipt = {
        "version": 1,
        "check": "verify-operator-telemetry",
        "ok": True,
        "candidate_sha": "c" * 40,
        "task_arn_sha256": "d" * 64,
        "scenario_id": "A",
        "details": details,
    }
    exec_path = tmp_path / "operator-exec.json"
    exec_path.write_text(json.dumps(exec_receipt))
    os.chmod(exec_path, 0o600)
    checkpoint_path = tmp_path / "retained-from-positive.json"

    bound = acceptance.control_manifest_checkpoint_operator_evidence(
        manifest_path,
        exec_receipt_path=str(exec_path),
        checkpoint_path=str(checkpoint_path),
        now=lambda: datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
    )

    checkpoint = json.loads(checkpoint_path.read_text())
    assert checkpoint["acceptance_run_id"] == run_id
    assert checkpoint["scenarios"]["A"] == {
        "cloudwatch_retained_metrics": [details["retained_metric_query"]],
        "xray_retained_trace_ids": [details["retained_trace_id"]],
        "expected_retained_metric_series": 1,
        "expected_retained_trace_ids": 1,
    }
    assert checkpoint["scenarios"]["B"] == {
        "cloudwatch_retained_metrics": [],
        "xray_retained_trace_ids": [],
        "expected_retained_metric_series": 0,
        "expected_retained_trace_ids": 0,
    }
    assert bound["evidence"]["retained_evidence_path"] == str(checkpoint_path)  # type: ignore[index]
    assert (
        acceptance.control_manifest_checkpoint_operator_evidence(
            manifest_path,
            exec_receipt_path=str(exec_path),
            checkpoint_path=str(checkpoint_path),
            now=lambda: datetime(2026, 7, 14, 1, 6, tzinfo=UTC),
        )["evidence"]["retained_evidence_path"]  # type: ignore[index]
        == str(checkpoint_path)
    )


def test_operator_telemetry_symbols_are_owned_by_private_module_and_reexported_by_identity() -> None:
    for name in PUBLIC_OPERATOR_TELEMETRY_EXPORTS:
        owned = getattr(operator_telemetry, name)
        assert getattr(acceptance, name) is owned
        assert owned.__module__ == operator_telemetry.__name__


_TELEMETRY_STARTED_AT = datetime(2026, 7, 14, 1, 1, tzinfo=UTC)


class _TelemetryAudit:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def execute_lifecycle_run(self) -> str:
        self.events.append("audit.execute")
        return "landscape-run-internal"

    def verify_run(self, run_id: str) -> bool:
        assert run_id == "landscape-run-internal"
        self.events.append("audit.verify")
        return True

    def terminal_status(self, run_id: str) -> str:
        assert run_id == "landscape-run-internal"
        self.events.append("audit.status")
        return "completed"

    def started_at(self, run_id: str) -> datetime:
        assert run_id == "landscape-run-internal"
        return _TELEMETRY_STARTED_AT


class _TelemetryEmitter:
    def __init__(self, events: list[str], *, delivery: bool = True) -> None:
        self.events = events
        self.delivery = delivery

    def emit_web_metric(self, sentinel_value: int, *, acceptance_namespace: str) -> bool:
        assert sentinel_value >= 0
        assert acceptance_namespace.endswith("-a")
        self.events.append("metric.emit")
        return self.delivery

    def health_degraded(self) -> bool:
        self.events.append("health.degraded")
        return not self.delivery


class _TelemetryQueries:
    def __init__(self, *, available_on: int) -> None:
        self.available_on = available_on
        self.metric_calls = 0
        self.trace_calls = {"RunStarted": 0, "RunFinished": 0}

    def metric_observed(self, *, metric_name: str, sentinel_value: int, acceptance_namespace: str) -> bool:
        assert metric_name == "operator.acceptance.sentinel"
        assert sentinel_value >= 0
        assert acceptance_namespace.endswith("-a")
        self.metric_calls += 1
        return self.metric_calls >= self.available_on

    def trace_observed(self, *, trace_name: str, run_id: str, started_at: datetime) -> bool:
        assert trace_name in self.trace_calls
        assert run_id == "landscape-run-internal"
        assert started_at == _TELEMETRY_STARTED_AT
        self.trace_calls[trace_name] += 1
        return self.trace_calls[trace_name] >= self.available_on

    def trace_terminal_status(self, *, run_id: str) -> str | None:
        assert run_id == "landscape-run-internal"
        return "completed"


def test_operator_telemetry_positive_lane_is_audit_first_bounded_status_correlated_and_sanitized() -> None:
    events: list[str] = []
    queries = _TelemetryQueries(available_on=3)
    sleeps: list[float] = []
    evidence = operator_telemetry.verify_operator_telemetry(
        audit=_TelemetryAudit(events),
        emitter=_TelemetryEmitter(events),
        queries=queries,
        resource=operator_telemetry.SanitizedResourceIdentity(
            service_name="elspeth-web",
            service_version="0.7.1",
            deployment_environment="acceptance",
            cloud_provider="aws",
        ),
        policy=operator_telemetry.AcceptancePolicy(attempts=3, interval_seconds=0.25),
        sleep=sleeps.append,
        sentinel_factory=lambda: "non-content-sentinel",
        acceptance_namespace="acceptance-run-a",
        metric_dimensions=(("service.name", "elspeth-web"),),
        now=lambda: 1234.5,
    )

    assert events[:4] == ["audit.execute", "audit.verify", "audit.status", "metric.emit"]
    assert queries.metric_calls == 3
    assert queries.trace_calls == {"RunStarted": 3, "RunFinished": 3}
    assert sleeps == [0.25, 0.25]
    assert {field.name for field in fields(evidence)} == {
        "metric_name",
        "trace_names",
        "observed_at",
        "resource",
        "sentinel_sha256",
        "landscape_status_agrees",
        "retained_metric_query",
        "retained_trace_id",
    }
    assert evidence.trace_names == ("RunStarted", "RunFinished")
    assert evidence.landscape_status_agrees is True
    rendered = repr(evidence)
    assert "non-content-sentinel" not in rendered
    assert "landscape-run-internal" not in rendered


def test_operator_telemetry_positive_lane_rejects_landscape_trace_terminal_mismatch() -> None:
    queries = _TelemetryQueries(available_on=1)
    queries.trace_terminal_status = lambda **_kwargs: "failed"  # type: ignore[method-assign]
    with pytest.raises(operator_telemetry.OperatorTelemetryAcceptanceError, match="terminal status"):
        operator_telemetry.verify_operator_telemetry(
            audit=_TelemetryAudit([]),
            emitter=_TelemetryEmitter([]),
            queries=queries,
            resource=operator_telemetry.SanitizedResourceIdentity("elspeth-web", "0.7.1", "acceptance", "aws"),
            policy=operator_telemetry.AcceptancePolicy(attempts=1, interval_seconds=0),
            acceptance_namespace="acceptance-run-a",
            metric_dimensions=(("service.name", "elspeth-web"),),
        )


def test_operator_telemetry_outage_lane_assumes_external_stop_and_keeps_audit_without_false_receipt() -> None:
    events: list[str] = []
    queries = _TelemetryQueries(available_on=100)
    evidence = operator_telemetry.verify_operator_telemetry_outage(
        audit=_TelemetryAudit(events),
        emitter=_TelemetryEmitter(events, delivery=False),
        queries=queries,
        policy=operator_telemetry.AcceptancePolicy(attempts=2, interval_seconds=0),
        sentinel_factory=lambda: "negative-sentinel",
        acceptance_namespace="acceptance-run-a",
        now=lambda: 1235.5,
    )

    assert events == [
        "audit.execute",
        "metric.emit",
        "audit.verify",
        "health.degraded",
    ]
    assert evidence.landscape_correct is True
    assert evidence.telemetry_degraded is True
    assert evidence.cloud_receipt is False
    assert "negative-sentinel" not in repr(evidence)


def test_aws_operator_telemetry_queries_use_exact_metric_dimensions_and_trace_correlation() -> None:
    metric_calls: list[dict[str, object]] = []
    trace_calls: list[dict[str, object]] = []
    sentinel_value = 123456789
    run_id = "landscape-run-internal"
    trace_id = operator_telemetry.xray_trace_id(run_id, started_at=_TELEMETRY_STARTED_AT)
    assert trace_id.split("-")[1] == f"{int(_TELEMETRY_STARTED_AT.timestamp()):08x}"

    class CloudWatch:
        def get_metric_data(self, **kwargs: object) -> object:
            metric_calls.append(kwargs)
            return {
                "MetricDataResults": [
                    {
                        "Id": "acceptance",
                        "StatusCode": "Complete",
                        "Timestamps": [datetime(2026, 7, 14, 1, 2, 3, tzinfo=UTC)],
                        "Values": [float(sentinel_value)],
                    }
                ]
            }

    class XRay:
        def batch_get_traces(self, **kwargs: object) -> object:
            trace_calls.append(kwargs)
            return {
                "Traces": [
                    {
                        "Id": trace_id,
                        "Segments": [
                            {"Document": json.dumps({"name": "RunStarted", "annotations": {"run_id": run_id}})},
                            {"Document": json.dumps({"name": "RunFinished", "annotations": {"run_id": run_id, "status": "completed"}})},
                        ],
                    }
                ],
                "UnprocessedTraceIds": [],
            }

    dimensions = operator_telemetry.operator_metric_dimensions(
        SimpleNamespace(
            operator_telemetry_service_name="elspeth-web",
            operator_telemetry_environment="acceptance",
            operator_telemetry_release="0.7.1",
            operator_telemetry_ecs_cluster="cluster-a",
            operator_telemetry_ecs_service="service-a",
            operator_telemetry_task_definition_family="elspeth-web",
            operator_telemetry_task_definition_revision="17",
        )
    )
    queries = operator_telemetry.AWSOperatorTelemetryQueries(
        cloudwatch=CloudWatch(),
        xray=XRay(),
        dimensions=dimensions,
        start_time=datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
    )

    assert (
        queries.metric_observed(
            metric_name="operator.acceptance.sentinel",
            sentinel_value=sentinel_value,
            acceptance_namespace="acceptance-run-a",
        )
        is True
    )
    assert queries.trace_observed(trace_name="RunStarted", run_id=run_id, started_at=_TELEMETRY_STARTED_AT) is True
    assert queries.trace_observed(trace_name="RunFinished", run_id=run_id, started_at=_TELEMETRY_STARTED_AT) is True
    assert queries.trace_terminal_status(run_id=run_id) == "completed"
    metric = metric_calls[0]["MetricDataQueries"][0]["MetricStat"]["Metric"]  # type: ignore[index]
    assert metric == {
        "Namespace": "ELSPETH/Operator",
        "MetricName": "operator.acceptance.sentinel",
        "Dimensions": [
            *[{"Name": name, "Value": value} for name, value in dimensions],
            {"Name": "elspeth.acceptance.namespace", "Value": "acceptance-run-a"},
            {"Name": "elspeth.acceptance.sentinel", "Value": str(sentinel_value)},
        ],
    }
    assert metric_calls[0]["MaxDatapoints"] == 100
    assert trace_calls == [{"TraceIds": [trace_id]}, {"TraceIds": [trace_id]}]


def test_aws_operator_telemetry_queries_accept_matching_point_among_repeated_window_and_reject_signal_content() -> None:
    now = datetime(2026, 7, 14, 1, 2, tzinfo=UTC)

    class CloudWatch:
        def get_metric_data(self, **_kwargs: object) -> object:
            return {
                "MetricDataResults": [
                    {
                        "Id": "acceptance",
                        "StatusCode": "Complete",
                        "Timestamps": [now, now + timedelta(minutes=1)],
                        "Values": [7.0, 11.0],
                    }
                ]
            }

    class XRay:
        def batch_get_traces(self, **kwargs: object) -> object:
            trace_id = kwargs["TraceIds"][0]  # type: ignore[index]
            return {
                "Traces": [
                    {
                        "Id": trace_id,
                        "Segments": [
                            {"Document": json.dumps({"name": "RunStarted", "annotations": {"run_id": "run-a", "prompt": "raw-secret"}})}
                        ],
                    }
                ],
                "UnprocessedTraceIds": [],
            }

    queries = operator_telemetry.AWSOperatorTelemetryQueries(
        cloudwatch=CloudWatch(),
        xray=XRay(),
        dimensions=(("service.name", "elspeth-web"),),
        start_time=now - timedelta(minutes=1),
        end_time=now + timedelta(minutes=3),
        forbidden_values=("raw-secret",),
    )
    assert (
        queries.metric_observed(
            metric_name="operator.acceptance.sentinel",
            sentinel_value=11,
            acceptance_namespace="acceptance-run-a",
        )
        is True
    )
    with pytest.raises(operator_telemetry.OperatorTelemetryAcceptanceError, match="forbidden content"):
        queries.trace_observed(trace_name="RunStarted", run_id="run-a", started_at=_TELEMETRY_STARTED_AT)


def test_aws_operator_telemetry_queries_treat_absence_as_retryable_and_malformed_or_provider_failures_as_static() -> None:
    class EmptyCloudWatch:
        def get_metric_data(self, **_kwargs: object) -> object:
            return {"MetricDataResults": []}

    class EmptyXRay:
        def batch_get_traces(self, **_kwargs: object) -> object:
            return {"Traces": [], "UnprocessedTraceIds": []}

    queries = operator_telemetry.AWSOperatorTelemetryQueries(
        cloudwatch=EmptyCloudWatch(),
        xray=EmptyXRay(),
        dimensions=(("service.name", "elspeth-web"),),
        start_time=datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
    )
    assert (
        queries.metric_observed(
            metric_name="operator.acceptance.sentinel",
            sentinel_value=1,
            acceptance_namespace="acceptance-run-a",
        )
        is False
    )
    assert queries.trace_observed(trace_name="RunStarted", run_id="run-a", started_at=_TELEMETRY_STARTED_AT) is False

    class MalformedCloudWatch:
        def get_metric_data(self, **_kwargs: object) -> object:
            return {"MetricDataResults": [{"Id": "acceptance", "StatusCode": "PartialData", "Values": [1]}]}

    queries = operator_telemetry.AWSOperatorTelemetryQueries(
        cloudwatch=MalformedCloudWatch(),
        xray=EmptyXRay(),
        dimensions=(("service.name", "elspeth-web"),),
        start_time=datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
    )
    with pytest.raises(operator_telemetry.OperatorTelemetryAcceptanceError, match="CloudWatch projection"):
        queries.metric_observed(
            metric_name="operator.acceptance.sentinel",
            sentinel_value=1,
            acceptance_namespace="acceptance-run-a",
        )

    class FailedXRay:
        def batch_get_traces(self, **_kwargs: object) -> object:
            raise RuntimeError("raw trace document credential URL request-id sentinel")

    queries = operator_telemetry.AWSOperatorTelemetryQueries(
        cloudwatch=EmptyCloudWatch(),
        xray=FailedXRay(),
        dimensions=(("service.name", "elspeth-web"),),
        start_time=datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
    )
    with pytest.raises(operator_telemetry.OperatorTelemetryAcceptanceError, match="X-Ray query") as raised:
        queries.trace_observed(trace_name="RunStarted", run_id="run-a", started_at=_TELEMETRY_STARTED_AT)
    assert "raw trace" not in str(raised.value)


def test_verify_operator_telemetry_live_positive_uses_default_chain_clients_and_closed_receipt() -> None:
    sentinel = "fixed-non-content-sentinel"
    sentinel_value = int(hashlib.sha256(sentinel.encode()).hexdigest()[:12], 16)
    trace_id = operator_telemetry.xray_trace_id("landscape-run-internal", started_at=_TELEMETRY_STARTED_AT)
    client_calls: list[tuple[str, str]] = []

    class CloudWatch:
        def get_metric_data(self, **_kwargs: object) -> object:
            return {
                "MetricDataResults": [
                    {
                        "Id": "acceptance",
                        "StatusCode": "Complete",
                        "Timestamps": [datetime(2026, 7, 14, 1, 2, tzinfo=UTC)],
                        "Values": [float(sentinel_value)],
                    }
                ]
            }

        def close(self) -> None:
            client_calls.append(("close", "cloudwatch"))

    class XRay:
        def batch_get_traces(self, **_kwargs: object) -> object:
            return {
                "Traces": [
                    {
                        "Id": trace_id,
                        "Segments": [
                            {"Document": json.dumps({"name": "RunStarted", "annotations": {"run_id": "landscape-run-internal"}})},
                            {
                                "Document": json.dumps(
                                    {
                                        "name": "RunFinished",
                                        "annotations": {"run_id": "landscape-run-internal", "status": "completed"},
                                    }
                                )
                            },
                        ],
                    }
                ],
                "UnprocessedTraceIds": [],
            }

        def close(self) -> None:
            client_calls.append(("close", "xray"))

    def client_factory(service: str, region: str) -> object:
        client_calls.append((service, region))
        return CloudWatch() if service == "cloudwatch" else XRay()

    settings = SimpleNamespace(
        deployment_target="aws-ecs",
        operator_telemetry="aws-otlp",
        operator_pipeline_telemetry_granularity="lifecycle",
        operator_telemetry_service_name="elspeth-web",
        operator_telemetry_environment="acceptance",
        operator_telemetry_release="0.7.1",
        operator_telemetry_ecs_cluster="cluster-a",
        operator_telemetry_ecs_service="service-a",
        operator_telemetry_task_definition_family="elspeth-web",
        operator_telemetry_task_definition_revision="17",
    )
    existing_run_ids: list[str] = []

    def existing_audit_factory(_settings: object, run_id: str) -> _TelemetryAudit:
        existing_run_ids.append(run_id)
        return _TelemetryAudit([])

    result = operator_telemetry.verify_operator_telemetry_live(
        {
            "AWS_REGION": "ap-southeast-2",
            "ELSPETH_ACCEPTANCE_PASSWORD": "must-not-escape",
            "ELSPETH_ACCEPTANCE_RUN_ID": "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            "ELSPETH_ACCEPTANCE_SCENARIO_ID": "A",
        },
        phase="positive",
        landscape_run_id="landscape-run-internal",
        settings_loader=lambda: settings,
        audit_factory=lambda _settings, _env: pytest.fail("new API capture must not run for an existing Landscape ID"),
        existing_audit_factory=existing_audit_factory,
        emitter_factory=lambda _settings: _TelemetryEmitter([]),
        aws_client_factory=client_factory,
        policy=operator_telemetry.AcceptancePolicy(attempts=1, interval_seconds=0),
        sentinel_factory=lambda: sentinel,
        now_datetime=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
        now_epoch=lambda: 1234.5,
    )

    assert result == {
        "phase": "positive",
        "metric_name": "operator.acceptance.sentinel",
        "trace_names": ["RunStarted", "RunFinished"],
        "observed_at": 1234.5,
        "resource": {
            "service_name": "elspeth-web",
            "service_version": "0.7.1",
            "deployment_environment": "acceptance",
            "cloud_provider": "aws",
        },
        "sentinel_sha256": hashlib.sha256(sentinel.encode()).hexdigest(),
        "landscape_terminal": True,
        "trace_terminal_agrees": True,
        "collector_degraded": False,
        "cloud_receipt": True,
        "retained_metric_query": {
            "namespace": "ELSPETH/Operator",
            "metric_name": "operator.acceptance.sentinel",
            "dimensions": [
                *[{"name": name, "value": value} for name, value in operator_telemetry.operator_metric_dimensions(settings)],
                {
                    "name": "elspeth.acceptance.namespace",
                    "value": "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-a",
                },
                {"name": "elspeth.acceptance.sentinel", "value": str(sentinel_value)},
            ],
        },
        "retained_trace_id": trace_id,
        "forbidden_content_absent": True,
    }
    assert client_calls == [
        ("cloudwatch", "ap-southeast-2"),
        ("xray", "ap-southeast-2"),
        ("close", "xray"),
        ("close", "cloudwatch"),
    ]
    assert existing_run_ids == ["landscape-run-internal"]
    assert "must-not-escape" not in json.dumps(result)


def test_verify_connection_budget_live_queries_cluster_metric_and_database_limit() -> None:
    calls: list[dict[str, object]] = []

    class CloudWatch:
        def get_metric_data(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return {
                "MetricDataResults": [
                    {
                        "Id": "connections",
                        "StatusCode": "Complete",
                        "Timestamps": [datetime(2026, 7, 14, 1, minute, tzinfo=UTC) for minute in range(10)],
                        "Values": [7.0, 8.0, 7.0, 6.0, 5.0, 5.0, 4.0, 4.0, 3.0, 3.0],
                    }
                ]
            }

        def close(self) -> None:
            calls.append({"closed": True})

    receipt = operator_telemetry.verify_connection_budget_live(
        {"AWS_REGION": "ap-southeast-2"},
        cluster_id="a-0123456789abcdef0123-db",
        start_time="2026-07-14T01:00:00Z",
        approved_budget=20,
        safety_margin=10,
        settings_loader=lambda: object(),
        max_connections_reader=lambda _settings: 100,
        aws_client_factory=lambda service, region: (
            CloudWatch() if (service, region) == ("cloudwatch", "ap-southeast-2") else pytest.fail("unexpected client")
        ),
        now=lambda: datetime(2026, 7, 14, 1, 11, tzinfo=UTC),
        attempts=1,
    )

    assert receipt == {
        "schema": "elspeth.rds-connection-budget.v2",
        "cluster_id_sha256": hashlib.sha256(b"a-0123456789abcdef0123-db").hexdigest(),
        "window_start": "2026-07-14T01:00:00Z",
        "window_end": "2026-07-14T01:10:00Z",
        "period_seconds": 60,
        "expected_points": 10,
        "points": [
            {"timestamp": f"2026-07-14T01:{minute:02d}:00Z", "count": count}
            for minute, count in enumerate([7.0, 8.0, 7.0, 6.0, 5.0, 5.0, 4.0, 4.0, 3.0, 3.0])
        ],
        "high_water": 8.0,
        "max_connections": 100,
        "approved_budget": 20,
        "safety_margin": 10,
        "ok": True,
    }
    query = calls[0]["MetricDataQueries"]
    assert query[0]["MetricStat"]["Metric"]["Dimensions"] == [  # type: ignore[index]
        {"Name": "DBClusterIdentifier", "Value": "a-0123456789abcdef0123-db"}
    ]
    assert calls[0]["StartTime"] == datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
    assert calls[0]["EndTime"] == datetime(2026, 7, 14, 1, 10, tzinfo=UTC)
    assert calls[-1] == {"closed": True}


def test_verify_connection_budget_live_rejects_non_minute_aligned_start() -> None:
    with pytest.raises(operator_telemetry.AcceptanceCheckError, match="connection_budget_input"):
        operator_telemetry.verify_connection_budget_live(
            {"AWS_REGION": "ap-southeast-2"},
            cluster_id="a-0123456789abcdef0123-db",
            start_time="2026-07-14T01:00:59Z",
            approved_budget=20,
            safety_margin=10,
            now=lambda: datetime(2026, 7, 14, 1, 11, tzinfo=UTC),
        )


def test_verify_connection_budget_live_retries_partial_data_even_when_it_has_points() -> None:
    responses = [
        {
            "MetricDataResults": [
                {
                    "Id": "connections",
                    "StatusCode": "PartialData",
                    "Timestamps": [datetime(2026, 7, 14, 1, 1, tzinfo=UTC)],
                    "Values": [2.0],
                }
            ]
        },
        {
            "MetricDataResults": [
                {
                    "Id": "connections",
                    "StatusCode": "Complete",
                    "Timestamps": [datetime(2026, 7, 14, 1, minute, tzinfo=UTC) for minute in range(10)],
                    "Values": [8.0] * 10,
                }
            ]
        },
    ]
    sleeps: list[float] = []

    class CloudWatch:
        def get_metric_data(self, **_kwargs: object) -> object:
            return responses.pop(0)

        def close(self) -> None:
            pass

    receipt = operator_telemetry.verify_connection_budget_live(
        {"AWS_REGION": "ap-southeast-2"},
        cluster_id="a-0123456789abcdef0123-db",
        start_time="2026-07-14T01:00:00Z",
        approved_budget=20,
        safety_margin=10,
        settings_loader=lambda: object(),
        max_connections_reader=lambda _settings: 100,
        aws_client_factory=lambda _service, _region: CloudWatch(),
        now=lambda: datetime(2026, 7, 14, 1, 11, tzinfo=UTC),
        sleep=sleeps.append,
        attempts=2,
    )

    assert receipt["high_water"] == 8.0
    assert sleeps == [30.0]


def test_verify_connection_budget_live_retries_complete_but_sparse_grid() -> None:
    full_timestamps = [datetime(2026, 7, 14, 1, minute, tzinfo=UTC) for minute in range(10)]
    responses = [
        {"MetricDataResults": [{"Id": "connections", "StatusCode": "Complete", "Timestamps": full_timestamps[:-1], "Values": [2.0] * 9}]},
        {"MetricDataResults": [{"Id": "connections", "StatusCode": "Complete", "Timestamps": full_timestamps, "Values": [3.0] * 10}]},
    ]
    sleeps: list[float] = []

    class CloudWatch:
        def get_metric_data(self, **_kwargs: object) -> object:
            return responses.pop(0)

        def close(self) -> None:
            pass

    receipt = operator_telemetry.verify_connection_budget_live(
        {"AWS_REGION": "ap-southeast-2"},
        cluster_id="a-0123456789abcdef0123-db",
        start_time="2026-07-14T01:00:00Z",
        approved_budget=20,
        safety_margin=10,
        settings_loader=lambda: object(),
        max_connections_reader=lambda _settings: 100,
        aws_client_factory=lambda _service, _region: CloudWatch(),
        now=lambda: datetime(2026, 7, 14, 1, 11, tzinfo=UTC),
        sleep=sleeps.append,
        attempts=2,
    )

    assert receipt["expected_points"] == 10
    assert sleeps == [30.0]


def test_verify_operator_telemetry_live_outage_requires_external_stop_effects_and_rejects_aws_overrides() -> None:
    settings = SimpleNamespace(
        deployment_target="aws-ecs",
        operator_telemetry="aws-otlp",
        operator_pipeline_telemetry_granularity="lifecycle",
        operator_telemetry_service_name="elspeth-web",
        operator_telemetry_environment="acceptance",
        operator_telemetry_release="0.7.1",
        operator_telemetry_ecs_cluster="cluster-a",
        operator_telemetry_ecs_service="service-a",
        operator_telemetry_task_definition_family="elspeth-web",
        operator_telemetry_task_definition_revision="17",
    )

    class EmptyClient:
        def get_metric_data(self, **_kwargs: object) -> object:
            return {"MetricDataResults": []}

        def batch_get_traces(self, **_kwargs: object) -> object:
            return {"Traces": [], "UnprocessedTraceIds": []}

        def close(self) -> None:
            pass

    result = operator_telemetry.verify_operator_telemetry_live(
        {
            "AWS_REGION": "ap-southeast-2",
            "ELSPETH_ACCEPTANCE_RUN_ID": "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            "ELSPETH_ACCEPTANCE_SCENARIO_ID": "A",
        },
        phase="outage",
        settings_loader=lambda: settings,
        audit_factory=lambda _settings, _env: _TelemetryAudit([]),
        emitter_factory=lambda _settings: _TelemetryEmitter([], delivery=False),
        aws_client_factory=lambda _service, _region: EmptyClient(),
        policy=operator_telemetry.AcceptancePolicy(attempts=2, interval_seconds=0),
        sentinel_factory=lambda: "outage-sentinel",
        now_datetime=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
        now_epoch=lambda: 1235.5,
    )
    assert result["phase"] == "outage"
    assert result["landscape_terminal"] is True
    assert result["trace_terminal_agrees"] is None
    assert result["collector_degraded"] is True
    assert result["cloud_receipt"] is False

    with pytest.raises(operator_telemetry.OperatorTelemetryAcceptanceError, match="AWS override"):
        operator_telemetry.verify_operator_telemetry_live(
            {"AWS_REGION": "ap-southeast-2", "AWS_ENDPOINT_URL_XRAY": "https://raw-provider.invalid"},
            phase="positive",
            settings_loader=lambda: settings,
        )


def test_xray_trace_identity_is_stable_across_retry_wall_clock_changes() -> None:
    run_id = "landscape-run-internal"
    persisted_start = datetime(2026, 7, 14, 1, 1, tzinfo=UTC)

    first_retry_wall_clock = datetime(2026, 7, 14, 1, 2, tzinfo=UTC)
    second_retry_wall_clock = datetime(2026, 7, 14, 1, 9, tzinfo=UTC)

    def trace_id_at_retry(_wall_clock: datetime) -> str:
        return operator_telemetry.xray_trace_id(run_id, started_at=persisted_start)

    assert trace_id_at_retry(first_retry_wall_clock) == trace_id_at_retry(second_retry_wall_clock)


def test_public_lifecycle_adapter_loads_and_caches_persisted_start() -> None:
    run_id = "landscape-run-internal"
    starts: list[str] = []

    def started_at_reader(_settings: object, observed_run_id: str) -> datetime:
        starts.append(observed_run_id)
        return _TELEMETRY_STARTED_AT

    audit = operator_telemetry.PublicApiLifecycleAudit(
        object(),
        {},
        capture_runner=lambda _env, **_kwargs: SimpleNamespace(landscape_run_id=run_id),
        status_reader=lambda _settings, _run_id: "completed",
        started_at_reader=started_at_reader,
    )

    assert audit.execute_lifecycle_run() == run_id
    assert audit.verify_run(run_id) is True
    assert audit.started_at(run_id) == _TELEMETRY_STARTED_AT
    assert audit.started_at(run_id) == _TELEMETRY_STARTED_AT
    assert starts == [run_id]


def test_existing_run_lifecycle_adapter_loads_and_caches_persisted_start() -> None:
    run_id = "landscape-run-internal"
    starts: list[str] = []

    def started_at_reader(_settings: object, observed_run_id: str) -> datetime:
        starts.append(observed_run_id)
        return _TELEMETRY_STARTED_AT

    audit = operator_telemetry.ExistingLandscapeLifecycleAudit(
        object(),
        run_id,
        status_reader=lambda _settings, _run_id: "completed",
        started_at_reader=started_at_reader,
    )

    assert audit.execute_lifecycle_run() == run_id
    assert audit.verify_run(run_id) is True
    assert audit.started_at(run_id) == _TELEMETRY_STARTED_AT
    assert audit.started_at(run_id) == _TELEMETRY_STARTED_AT
    assert starts == [run_id]


@pytest.mark.parametrize(
    "invalid_start",
    [None, "2026-07-14T01:01:00Z", datetime(2026, 7, 14, 1, 1, tzinfo=UTC).replace(tzinfo=None)],
)
@pytest.mark.parametrize("adapter", ["public", "existing"])
def test_lifecycle_adapters_fail_closed_when_persisted_start_is_missing_or_invalid(
    invalid_start: object,
    adapter: str,
) -> None:
    run_id = "landscape-run-internal"

    def started_at_reader(_settings: object, _run_id: str) -> object:
        return invalid_start

    audit = (
        operator_telemetry.PublicApiLifecycleAudit(
            object(),
            {},
            capture_runner=lambda _env, **_kwargs: SimpleNamespace(landscape_run_id=run_id),
            started_at_reader=started_at_reader,  # type: ignore[arg-type]
        )
        if adapter == "public"
        else operator_telemetry.ExistingLandscapeLifecycleAudit(
            object(),
            run_id,
            started_at_reader=started_at_reader,  # type: ignore[arg-type]
        )
    )
    audit.execute_lifecycle_run()

    with pytest.raises(operator_telemetry.OperatorTelemetryAcceptanceError, match="run start is unavailable"):
        audit.started_at(run_id)


def test_cloudwatch_and_xray_queries_reject_paginated_responses() -> None:
    now = datetime(2026, 7, 14, 1, 2, tzinfo=UTC)

    class PaginatedCloudWatch:
        def get_metric_data(self, **_kwargs: object) -> object:
            return {"MetricDataResults": [], "NextToken": "more"}

    class PaginatedXRay:
        def batch_get_traces(self, **_kwargs: object) -> object:
            return {"Traces": [], "UnprocessedTraceIds": [], "NextToken": "more"}

    queries = operator_telemetry.AWSOperatorTelemetryQueries(
        cloudwatch=PaginatedCloudWatch(),
        xray=PaginatedXRay(),
        dimensions=(("service.name", "elspeth-web"),),
        start_time=now - timedelta(minutes=1),
        end_time=now + timedelta(minutes=1),
    )

    with pytest.raises(operator_telemetry.OperatorTelemetryAcceptanceError, match="CloudWatch projection"):
        queries.metric_observed(
            metric_name="operator.acceptance.sentinel",
            sentinel_value=1,
            acceptance_namespace="acceptance-run-a",
        )
    with pytest.raises(operator_telemetry.OperatorTelemetryAcceptanceError, match="X-Ray projection"):
        queries.trace_observed(trace_name="RunStarted", run_id="run-a", started_at=_TELEMETRY_STARTED_AT)


@pytest.mark.parametrize(
    ("high_water", "approved_budget", "safety_margin", "expected_error"),
    [(21.0, 20, 10, "connection_budget_exceeded"), (8.0, 100, 10, "connection_budget_limit")],
)
def test_connection_budget_rejects_high_water_or_an_approved_budget_without_safety_margin(
    high_water: float,
    approved_budget: int,
    safety_margin: int,
    expected_error: str,
) -> None:
    class CloudWatch:
        def get_metric_data(self, **_kwargs: object) -> object:
            return {
                "MetricDataResults": [
                    {
                        "Id": "connections",
                        "StatusCode": "Complete",
                        "Timestamps": [datetime(2026, 7, 14, 1, minute, tzinfo=UTC) for minute in range(10)],
                        "Values": [high_water] * 10,
                    }
                ]
            }

        def close(self) -> None:
            pass

    with pytest.raises(operator_telemetry.AcceptanceCheckError, match=expected_error):
        operator_telemetry.verify_connection_budget_live(
            {"AWS_REGION": "ap-southeast-2"},
            cluster_id="a-0123456789abcdef0123-db",
            start_time="2026-07-14T01:00:00Z",
            approved_budget=approved_budget,
            safety_margin=safety_margin,
            settings_loader=lambda: object(),
            max_connections_reader=lambda _settings: 100,
            aws_client_factory=lambda _service, _region: CloudWatch(),
            now=lambda: datetime(2026, 7, 14, 1, 11, tzinfo=UTC),
            attempts=1,
        )
