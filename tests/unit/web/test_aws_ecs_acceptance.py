"""Contract tests for the AWS ECS acceptance controller."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import multiprocessing
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Empty
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web import aws_ecs_acceptance as acceptance
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH
from tests.unit.web.aws_ecs_acceptance.test_bedrock_guardrails import _guardrail_env

EXPECTED_COMMANDS = {
    "capture",
    "provision-storage",
    "scenario-namespace",
    "verify-api",
    "verify-payloads",
    "verify-local-auth",
    "verify-s3",
    "verify-bedrock",
    "verify-bedrock-guardrails",
    "verify-connection-budget",
    "verify-operator-telemetry",
    "extract-exec-receipt",
    "sanitize-evidence",
    "control-manifest",
    "gate-ledger",
    "receipt-store",
    "approval-verify",
    "approval-require-current",
    "scenario-load",
    "validate-task-definition-policy",
    "compatibility-record-validate",
    "orphan-sweep",
    "cleanup-evidence-finalize",
    "evidence-export-receipt",
}


def _all_parsers(parser: argparse.ArgumentParser) -> list[argparse.ArgumentParser]:
    parsers = [parser]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                parsers.extend(_all_parsers(child))
    return parsers


def test_cli_exposes_the_exact_reviewed_command_surface() -> None:
    parser = acceptance.build_parser()
    command_actions = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]

    assert len(command_actions) == 1
    assert set(command_actions[0].choices) == EXPECTED_COMMANDS


def test_cli_never_accepts_credentials_or_tokens_as_arguments() -> None:
    parser = acceptance.build_parser()
    option_strings = {option for candidate in _all_parsers(parser) for action in candidate._actions for option in action.option_strings}

    assert not option_strings & {
        "--username",
        "--password",
        "--token",
        "--bearer-token",
        "--access-token",
        "--aws-access-key-id",
        "--aws-secret-access-key",
        "--aws-session-token",
    }


def test_capture_and_verify_api_require_state_file_arguments() -> None:
    parser = acceptance.build_parser()

    capture = parser.parse_args(["capture", "--state-file", "state.json"])
    verify = parser.parse_args(["verify-api", "--state-file", "state.json"])

    assert capture.command == "capture"
    assert capture.state_file == "state.json"
    assert verify.command == "verify-api"
    assert verify.state_file == "state.json"


def test_verify_payloads_requires_landscape_run_id_argument() -> None:
    parser = acceptance.build_parser()

    parsed = parser.parse_args(["verify-payloads", "--landscape-run-id", "6ad6bff9-5e84-48ea-8588-f49cfb93cc62"])

    assert parsed.command == "verify-payloads"
    assert parsed.landscape_run_id == "6ad6bff9-5e84-48ea-8588-f49cfb93cc62"


def test_sanitize_evidence_kinds_are_closed() -> None:
    parser = acceptance.build_parser()
    command_action = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    sanitizer = command_action.choices["sanitize-evidence"]
    kind_action = next(action for action in sanitizer._actions if action.dest == "kind")

    assert set(kind_action.choices or ()) == {
        "web-log",
        "doctor-log",
        "deployment-event",
        "task-definition",
        "terraform-plan",
        "terraform-destroy-plan",
    }


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
    evidence = acceptance.verify_operator_telemetry(
        audit=_TelemetryAudit(events),
        emitter=_TelemetryEmitter(events),
        queries=queries,
        resource=acceptance.SanitizedResourceIdentity(
            service_name="elspeth-web",
            service_version="0.7.1",
            deployment_environment="acceptance",
            cloud_provider="aws",
        ),
        policy=acceptance.AcceptancePolicy(attempts=3, interval_seconds=0.25),
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
    with pytest.raises(acceptance.OperatorTelemetryAcceptanceError, match="terminal status"):
        acceptance.verify_operator_telemetry(
            audit=_TelemetryAudit([]),
            emitter=_TelemetryEmitter([]),
            queries=queries,
            resource=acceptance.SanitizedResourceIdentity("elspeth-web", "0.7.1", "acceptance", "aws"),
            policy=acceptance.AcceptancePolicy(attempts=1, interval_seconds=0),
            acceptance_namespace="acceptance-run-a",
            metric_dimensions=(("service.name", "elspeth-web"),),
        )


def test_operator_telemetry_outage_lane_assumes_external_stop_and_keeps_audit_without_false_receipt() -> None:
    events: list[str] = []
    queries = _TelemetryQueries(available_on=100)
    evidence = acceptance.verify_operator_telemetry_outage(
        audit=_TelemetryAudit(events),
        emitter=_TelemetryEmitter(events, delivery=False),
        queries=queries,
        policy=acceptance.AcceptancePolicy(attempts=2, interval_seconds=0),
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
    trace_id = acceptance.xray_trace_id(run_id, started_at=_TELEMETRY_STARTED_AT)
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

    dimensions = acceptance.operator_metric_dimensions(
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
    queries = acceptance.AWSOperatorTelemetryQueries(
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

    queries = acceptance.AWSOperatorTelemetryQueries(
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
    with pytest.raises(acceptance.OperatorTelemetryAcceptanceError, match="forbidden content"):
        queries.trace_observed(trace_name="RunStarted", run_id="run-a", started_at=_TELEMETRY_STARTED_AT)


def test_aws_operator_telemetry_queries_treat_absence_as_retryable_and_malformed_or_provider_failures_as_static() -> None:
    class EmptyCloudWatch:
        def get_metric_data(self, **_kwargs: object) -> object:
            return {"MetricDataResults": []}

    class EmptyXRay:
        def batch_get_traces(self, **_kwargs: object) -> object:
            return {"Traces": [], "UnprocessedTraceIds": []}

    queries = acceptance.AWSOperatorTelemetryQueries(
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

    queries = acceptance.AWSOperatorTelemetryQueries(
        cloudwatch=MalformedCloudWatch(),
        xray=EmptyXRay(),
        dimensions=(("service.name", "elspeth-web"),),
        start_time=datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
    )
    with pytest.raises(acceptance.OperatorTelemetryAcceptanceError, match="CloudWatch projection"):
        queries.metric_observed(
            metric_name="operator.acceptance.sentinel",
            sentinel_value=1,
            acceptance_namespace="acceptance-run-a",
        )

    class FailedXRay:
        def batch_get_traces(self, **_kwargs: object) -> object:
            raise RuntimeError("raw trace document credential URL request-id sentinel")

    queries = acceptance.AWSOperatorTelemetryQueries(
        cloudwatch=EmptyCloudWatch(),
        xray=FailedXRay(),
        dimensions=(("service.name", "elspeth-web"),),
        start_time=datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
    )
    with pytest.raises(acceptance.OperatorTelemetryAcceptanceError, match="X-Ray query") as raised:
        queries.trace_observed(trace_name="RunStarted", run_id="run-a", started_at=_TELEMETRY_STARTED_AT)
    assert "raw trace" not in str(raised.value)


def test_verify_operator_telemetry_live_positive_uses_default_chain_clients_and_closed_receipt() -> None:
    sentinel = "fixed-non-content-sentinel"
    sentinel_value = int(hashlib.sha256(sentinel.encode()).hexdigest()[:12], 16)
    trace_id = acceptance.xray_trace_id("landscape-run-internal", started_at=_TELEMETRY_STARTED_AT)
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

    result = acceptance.verify_operator_telemetry_live(
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
        policy=acceptance.AcceptancePolicy(attempts=1, interval_seconds=0),
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
                *[{"name": name, "value": value} for name, value in acceptance.operator_metric_dimensions(settings)],
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

    receipt = acceptance.verify_connection_budget_live(
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
    with pytest.raises(acceptance.AcceptanceCheckError, match="connection_budget_input"):
        acceptance.verify_connection_budget_live(
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

    receipt = acceptance.verify_connection_budget_live(
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

    receipt = acceptance.verify_connection_budget_live(
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

    result = acceptance.verify_operator_telemetry_live(
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
        policy=acceptance.AcceptancePolicy(attempts=2, interval_seconds=0),
        sentinel_factory=lambda: "outage-sentinel",
        now_datetime=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
        now_epoch=lambda: 1235.5,
    )
    assert result["phase"] == "outage"
    assert result["landscape_terminal"] is True
    assert result["trace_terminal_agrees"] is None
    assert result["collector_degraded"] is True
    assert result["cloud_receipt"] is False

    with pytest.raises(acceptance.OperatorTelemetryAcceptanceError, match="AWS override"):
        acceptance.verify_operator_telemetry_live(
            {"AWS_REGION": "ap-southeast-2", "AWS_ENDPOINT_URL_XRAY": "https://raw-provider.invalid"},
            phase="positive",
            settings_loader=lambda: settings,
        )


def _s3_receipt_details() -> dict[str, object]:
    return {
        "object_count": 1,
        "source_sha256": "a" * 64,
        "sink_sha256": "a" * 64,
        "collision_rejected": True,
        "cleanup_succeeded": True,
    }


def _guardrail_receipt_details() -> dict[str, object]:
    return {
        "controls": [
            {
                "plugin_id": "aws_bedrock_prompt_shield",
                "profile_alias": "prompt-approved",
                "guardrail_version": "7",
                "safe_case_passed": True,
                "attack_case_blocked": True,
                "request_ids_present": True,
                "safe_text_sha256": "a" * 64,
                "blocked_text_sha256": "b" * 64,
                "checked_at": "2026-07-14T01:02:03Z",
            },
            {
                "plugin_id": "aws_bedrock_content_safety",
                "profile_alias": "content-approved",
                "guardrail_version": "11",
                "safe_case_passed": True,
                "attack_case_blocked": True,
                "request_ids_present": True,
                "safe_text_sha256": "c" * 64,
                "blocked_text_sha256": "d" * 64,
                "checked_at": "2026-07-14T01:02:03Z",
            },
        ],
        "plugin_policy": _plugin_policy_receipt(),
    }


def _plugin_policy_receipt(*, include_landscape: bool = True) -> dict[str, object]:
    receipt: dict[str, object] = {
        "policy_hash": "1" * 64,
        "snapshot_hash": "2" * 64,
        "binding_sha256": "3" * 64,
        "tutorial_profile_ready": True,
        "tutorial_ready": False,
        "tutorial_blocker": "tutorial_required_control_coverage",
        "tutorial_profile_alias": "tutorial",
        "target_llm": "transform:llm",
        "selected_controls": [
            {
                "capability": "prompt_shield",
                "plugin_id": "transform:aws_bedrock_prompt_shield",
                "profile_alias": "prompt-approved",
                "mode": "required",
            },
            {
                "capability": "content_safety",
                "plugin_id": "transform:aws_bedrock_content_safety",
                "profile_alias": "content-approved",
                "mode": "required",
            },
        ],
    }
    if include_landscape:
        receipt["landscape_evidence"] = True
    return receipt


def _scenario_inventory(
    run_id: str,
    scenario_id: str,
    binding: str,
    binding_file: str,
    *,
    phase: str = "resolved",
) -> dict[str, object]:
    values = {name: "" for name in acceptance.SCENARIO_ASSIGNMENT_NAMES if name not in {"ACTIVE_SCENARIO_ID", "ACCEPTANCE_RUN_ID"}}
    namespace = acceptance.scenario_resource_namespace(run_id, scenario_id)
    account = "123456789012"
    region = "ap-southeast-2"
    task_families = [f"acceptance-{namespace}"]
    task_arns = [f"arn:aws:ecs:{region}:{account}:task-definition/{task_families[0]}:{revision}" for revision in range(1, 7)]
    load_balancer_suffix = f"app/{namespace}-alb/0123456789abcdef"
    listener_arn = f"arn:aws:elasticloadbalancing:{region}:{account}:listener/{load_balancer_suffix}/0123456789abcdef"
    listener_rule_arn = (
        f"arn:aws:elasticloadbalancing:{region}:{account}:listener-rule/{load_balancer_suffix}/0123456789abcdef/0123456789abcdef"
    )
    log_groups = [
        f"/aws/ecs/{namespace}-web",
        f"/aws/ecs/{namespace}-doctor",
        f"/aws/events/{namespace}-deployments",
        f"/aws/ecs/{namespace}-operator-metrics",
    ]
    values.update(
        {
            "DEPLOYMENT_MODE": "first" if scenario_id == "A" else "upgrade",
            "TARGET_PLATFORM": "linux/amd64",
            "AWS_REGION": region,
            "ECS_CLUSTER": f"acceptance-{namespace}-cluster",
            "ECS_SERVICE": f"acceptance-{namespace}-service",
            "WEB_CONTAINER_NAME": "elspeth-web",
            "ELSPETH_WEB__DATA_DIR": "/var/lib/elspeth",
            "ELSPETH_WEB__PAYLOAD_STORE_PATH": "/var/lib/elspeth/payloads",
            "ELSPETH_WEB__COMPOSER_MODEL": "openrouter/openai/gpt-5.4",
            "ELSPETH_WEB__COMPOSER_ADVISOR_MODEL": "openrouter/anthropic/claude-opus-4.6",
            "ELSPETH_BEDROCK_LIVE_TEST_MODEL": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
            "TARGET_GROUP_ARN": f"arn:aws:elasticloadbalancing:{region}:{account}:targetgroup/{namespace}-target/0123456789abcdef",
            "ALB_BASE_URL": f"https://{namespace}.example.invalid",
            "ALB_ARN": f"arn:aws:elasticloadbalancing:{region}:{account}:loadbalancer/app/{namespace}-alb/0123456789abcdef",
            "CANDIDATE_TASK_DEFINITION": task_arns[0],
            "DOCTOR_TASK_DEFINITION": task_arns[1],
            "DOCTOR_CONTAINER_NAME": "doctor",
            "DOCTOR_NETWORK_CONFIGURATION": json.dumps(
                {
                    "awsvpcConfiguration": {
                        "subnets": [f"subnet-0123456789abcde{scenario_id.lower()}"],
                        "securityGroups": [f"sg-0123456789abcde{scenario_id.lower()}"],
                        "assignPublicIp": "DISABLED",
                    }
                },
                separators=(",", ":"),
            ),
            "PAYLOAD_VERIFIER_TASK_DEFINITION": task_arns[2],
            "LOCAL_AUTH_VERIFIER_TASK_DEFINITION": task_arns[3],
            "WEB_LOG_GROUP": log_groups[0],
            "WEB_LOG_STREAM_PREFIX": "web",
            "DOCTOR_LOG_GROUP": log_groups[1],
            "DOCTOR_LOG_STREAM_PREFIX": "doctor",
            "OPERATOR_METRICS_LOG_GROUP": log_groups[3],
            "ECS_DEPLOYMENT_EVENT_RULE": f"{namespace}-deployments",
            "ECS_DEPLOYMENT_EVENT_TARGET_ID": f"{namespace}-deployment-log",
            "ECS_DEPLOYMENT_EVENT_LOG_GROUP": log_groups[2],
            "DB_CLUSTER_IDENTIFIER": f"{namespace}-aurora",
            "ELSPETH_TEST_S3_BUCKET": f"elspeth-{namespace}",
            "SCENARIO_TF_DIR": f"/iac/scenario-{scenario_id.lower()}",
            "SCENARIO_TF_VARS": f"/iac/scenario-{scenario_id.lower()}.tfvars",
            "SCENARIO_TF_BINDING_SHA": binding,
            "SCENARIO_TF_BINDING_FILE": binding_file,
            "OIDC_EXPECTED_AUDIENCE_CLAIM": "client_id",
        }
    )
    policy_env = _guardrail_env()
    scenario_profiles = json.loads(policy_env["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"])
    compact_namespace = namespace.replace("-", "")
    for profile in scenario_profiles:
        profile["guardrail_identifier"] = (
            f"{compact_namespace}{'prompt' if profile['plugin'] == 'aws_bedrock_prompt_shield' else 'content'}"
        )
    policy_env["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"] = json.dumps(scenario_profiles, separators=(",", ":"))
    values.update({name: policy_env[name] for name in acceptance.PLUGIN_POLICY_ASSIGNMENT_NAMES})
    values["ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256"] = acceptance.plugin_policy_binding_sha256(values)
    values.update(
        {
            "FIRST_DEPLOY_LISTENER_RULE_ARN": listener_rule_arn,
            "FIRST_DEPLOY_FORWARD_ACTIONS": json.dumps(
                [{"Type": "forward", "TargetGroupArn": values["TARGET_GROUP_ARN"]}], separators=(",", ":")
            ),
            "FIRST_DEPLOY_DISABLED_ACTIONS": '[{"Type":"fixed-response","FixedResponseConfig":{"StatusCode":"503"}}]',
        }
    )
    if scenario_id == "B":
        pool_id = f"{region}_AbCd1234"
        values.update(
            {
                "PREVIOUS_TASK_DEFINITION": task_arns[5],
                "ROLLBACK_DOCTOR_TASK_DEFINITION": task_arns[4],
                "COGNITO_USER_POOL_ID": pool_id,
                "OIDC_EXPECTED_ISSUER": f"https://cognito-idp.{region}.amazonaws.com/{pool_id}",
                "OIDC_EXPECTED_AUDIENCE": "1234567890abcdefghijklmnop",
                "OIDC_EXPECTED_AUTHORIZATION_ORIGIN": f"https://{namespace}.auth.{region}.amazoncognito.com",
            }
        )
    if phase == "preapply":
        for field in (
            "TARGET_GROUP_ARN",
            "ALB_BASE_URL",
            "ALB_ARN",
            "CANDIDATE_TASK_DEFINITION",
            "DOCTOR_TASK_DEFINITION",
            "DOCTOR_NETWORK_CONFIGURATION",
            "PAYLOAD_VERIFIER_TASK_DEFINITION",
            "LOCAL_AUTH_VERIFIER_TASK_DEFINITION",
            "ROLLBACK_DOCTOR_TASK_DEFINITION",
            "PREVIOUS_TASK_DEFINITION",
            "FIRST_DEPLOY_LISTENER_RULE_ARN",
            "FIRST_DEPLOY_FORWARD_ACTIONS",
            "FIRST_DEPLOY_DISABLED_ACTIONS",
            "COGNITO_USER_POOL_ID",
            "OIDC_EXPECTED_ISSUER",
            "OIDC_EXPECTED_AUDIENCE",
            "OIDC_EXPECTED_AUTHORIZATION_ORIGIN",
        ):
            values[field] = ""
        values["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"] = "[]"
        values["ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256"] = acceptance.plugin_policy_binding_sha256(values)
    guardrail_profiles = json.loads(values["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"])
    bedrock_guardrails = [
        {
            "identifier": profile["guardrail_identifier"],
            "versions": [profile["guardrail_version"]],
        }
        for profile in guardrail_profiles
    ]
    return {
        "schema": "elspeth.aws-ecs-scenario-inventory.v6",
        "acceptance_run_id": run_id,
        "candidate_sha": "c" * 40,
        "aws_account_id": account,
        "aws_region": region,
        "scenario_id": scenario_id,
        "phase": phase,
        "values": values,
        "orphan_sweep": {
            "tag_key": "ACCEPTANCE_RUN_ID",
            "cleanup_owner": "aws-acceptance-owner",
            "ecs_task_definition_families": task_families,
            "elbv2_listener_arns": [listener_arn] if phase == "resolved" else [],
            "rds_db_instance_identifiers": [f"{namespace}-aurora-1"],
            "efs_creation_tokens": [f"{namespace}-efs"],
            "efs_file_system_ids": [f"fs-0123456789abcde{scenario_id.lower()}"] if phase == "resolved" else [],
            "efs_access_point_ids": [f"fsap-0123456789abcde{scenario_id.lower()}"] if phase == "resolved" else [],
            "secret_ids": [
                f"{namespace}-database-runtime",
                f"{namespace}-database-schema",
                f"{namespace}-database-bootstrap",
                f"{namespace}-openrouter-composer",
            ],
            "iam_role_names": [f"{namespace}-task-role", f"{namespace}-execution-role"],
            "log_group_names": log_groups,
            "log_resource_policy_names": [f"{namespace}-delivery-policy"],
            "cloudwatch_dashboard_names": [f"{namespace}-dashboard"],
            "cloudwatch_alarm_names": [f"{namespace}-alarm"],
            "cloudwatch_retained_metrics": [],
            "xray_group_names": [f"{namespace}-xray"],
            "xray_sampling_rule_names": [f"{namespace}-sampling"],
            "xray_retained_trace_ids": [],
            "transaction_search_baseline_sha256": hashlib.sha256(
                json.dumps(
                    {
                        "destination": None,
                        "indexing_rules": [],
                        "spans_log_group_present": False,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            "event_rules": [
                {
                    "event_bus_name": "default",
                    "rule_name": f"{namespace}-deployments",
                    "target_ids": [f"{namespace}-deployment-log"],
                }
            ],
            "bedrock_guardrails": bedrock_guardrails,
            "cognito_subject_sub": "subject-1234" if scenario_id == "B" and phase == "resolved" else "",
            "cognito_pool_owned": scenario_id == "B" and phase == "resolved",
            "expected_retained_metric_series": 0,
            "expected_retained_trace_ids": 0,
        },
    }


def _init_control_manifest(
    path: Path,
    *,
    run_id: str = "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
    deadline: str = "2026-07-14T05:00:00Z",
    inventory_mutator: Callable[[dict[str, object], str], None] | None = None,
    preapply_inventory_mutator: Callable[[dict[str, object], str], None] | None = None,
    retained_mutator: Callable[[dict[str, object]], None] | None = None,
    binding_mutator: Callable[[dict[str, object], str], None] | None = None,
    bind_resolved: bool = True,
    bind_retained: bool = True,
    prepare_apply_evidence: bool = True,
) -> dict[str, object]:
    scenario_a = path.parent / "scenario-a.json"
    scenario_b = path.parent / "scenario-b.json"
    scenario_a_preapply = path.parent / "scenario-a-preapply.json"
    scenario_b_preapply = path.parent / "scenario-b-preapply.json"
    bindings: dict[str, str] = {}
    for inventory_path, preapply_path, scenario in (
        (scenario_a, scenario_a_preapply, "A"),
        (scenario_b, scenario_b_preapply, "B"),
    ):
        binding_path = path.parent / f"tf-binding-{scenario.lower()}.json"
        binding_receipt = {
            "schema": "elspeth.aws-ecs-tf-binding.v1",
            "acceptance_run_id": run_id,
            "scenario_id": scenario,
            "repository_commit": ("a" if scenario == "A" else "b") * 40,
            "terraform_lock_sha256": ("c" if scenario == "A" else "d") * 64,
            "terraform_version": "1.9.0",
            "backend_type": "s3",
            "backend_encrypted": True,
            "backend_locked": True,
            "backend_state_key_sha256": hashlib.sha256(f"state-{scenario}".encode()).hexdigest(),
            "workspace": f"acceptance-{scenario.lower()}",
            "aws_account_id": "123456789012",
            "aws_region": "ap-southeast-2",
            "vars_sha256": ("e" if scenario == "A" else "f") * 64,
        }
        if binding_mutator is not None:
            binding_mutator(binding_receipt, scenario)
        binding_path.write_text(json.dumps(binding_receipt))
        os.chmod(binding_path, 0o600)
        binding = hashlib.sha256(json.dumps(binding_receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        bindings[scenario] = binding
        preapply_inventory = _scenario_inventory(run_id, scenario, binding, str(binding_path), phase="preapply")
        if preapply_inventory_mutator is not None:
            preapply_inventory_mutator(preapply_inventory, scenario)
        preapply_path.write_text(json.dumps(preapply_inventory))
        os.chmod(preapply_path, 0o600)
        inventory = _scenario_inventory(run_id, scenario, binding, str(binding_path), phase="resolved")
        if inventory_mutator is not None:
            inventory_mutator(inventory, scenario)
        inventory_path.write_text(json.dumps(inventory))
        os.chmod(inventory_path, 0o600)
    acceptance.control_manifest_init(
        path,
        acceptance_run_id=run_id,
        candidate_sha="c" * 40,
        aws_account_id="123456789012",
        aws_region="ap-southeast-2",
        scenario_a_inventory=str(scenario_a_preapply),
        scenario_b_inventory=str(scenario_b_preapply),
        scenario_a_tf_binding=bindings["A"],
        scenario_b_tf_binding=bindings["B"],
        evidence_destination_sha256="9" * 64,
        gate_ledger=str(path.parent / "gate-ledger.json"),
        teardown_deadline_utc=deadline,
        now=lambda: datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
    )
    if prepare_apply_evidence:
        for scenario, plan_character, noop_character in (("A", "1", "3"), ("B", "2", "4")):
            plan_sha = plan_character * 64
            plan_path = path.parent / f"{scenario.lower()}-plan-receipt.json"
            plan_path.write_text(json.dumps(_terraform_receipt()))
            os.chmod(plan_path, 0o600)
            plan_receipt_hash = acceptance.receipt_store(
                path,
                scenario_id=scenario,
                kind="terraform-plan",
                subject_id=plan_sha,
                receipt_file=plan_path,
                now=lambda: datetime(2026, 7, 14, 1, 0, 10, tzinfo=UTC),
            )
            approval_path = path.parent / f"{scenario.lower()}-plan-approval.json"
            approval_path.write_text(
                json.dumps(
                    {
                        "schema": "elspeth.aws-ecs-approval.v1",
                        "acceptance_run_id": run_id,
                        "scenario_id": scenario,
                        "kind": "terraform-plan",
                        "plan_receipt_hash": plan_receipt_hash,
                        "approver_identity": "infrastructure-owner",
                        "authority": "terraform-apply",
                        "decision": "approved",
                        "approved_at": "2026-07-14T01:00:00Z",
                        "expires_at": "2026-07-14T04:00:00Z",
                        "key_id": "owner-key-1",
                        "signature": "opaque-signature",
                    }
                )
            )
            os.chmod(approval_path, 0o600)
            approval_hash = acceptance.approval_verify(
                path,
                scenario_id=scenario,
                kind="terraform-plan",
                plan_receipt_hash=plan_receipt_hash,
                approval_file=approval_path,
                signature_verifier=lambda _payload, _signature, _key_id: True,
                now=lambda: datetime(2026, 7, 14, 1, 0, 20, tzinfo=UTC),
            )
            plan_binding = f"{scenario}:{plan_sha}:{plan_receipt_hash}:{approval_hash}"
            acceptance.control_manifest_update(
                path,
                terraform_plan_receipt=plan_binding,
                now=lambda: datetime(2026, 7, 14, 1, 0, 30, tzinfo=UTC),
            )
            acceptance.control_manifest_update(
                path,
                terraform_applied=plan_binding,
                now=lambda: datetime(2026, 7, 14, 1, 0, 40, tzinfo=UTC),
            )
            noop_sha = noop_character * 64
            noop_path = path.parent / f"{scenario.lower()}-noop-receipt.json"
            noop_path.write_text(json.dumps(_terraform_receipt()))
            os.chmod(noop_path, 0o600)
            noop_receipt_hash = acceptance.receipt_store(
                path,
                scenario_id=scenario,
                kind="terraform-noop",
                subject_id=noop_sha,
                receipt_file=noop_path,
                now=lambda: datetime(2026, 7, 14, 1, 0, 50, tzinfo=UTC),
            )
            acceptance.control_manifest_update(
                path,
                terraform_noop_receipt=f"{scenario}:{noop_sha}:{noop_receipt_hash}",
                now=lambda: datetime(2026, 7, 14, 1, 0, 55, tzinfo=UTC),
            )
    if bind_resolved:
        acceptance.control_manifest_bind_scenario(
            path,
            scenario_id="A",
            inventory_path=str(scenario_a),
            now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
        )
        acceptance.control_manifest_bind_scenario(
            path,
            scenario_id="B",
            inventory_path=str(scenario_b),
            now=lambda: datetime(2026, 7, 14, 1, 1, 10, tzinfo=UTC),
        )
    if bind_resolved and bind_retained:
        retained_evidence: dict[str, object] = {
            "schema": "elspeth.aws-ecs-retained-evidence.v1",
            "acceptance_run_id": run_id,
            "candidate_sha": "c" * 40,
            "scenarios": {
                scenario: {
                    "cloudwatch_retained_metrics": [
                        {
                            "namespace": "ELSPETH/Acceptance",
                            "metric_name": "CompletedRuns",
                            "dimensions": [
                                {"name": "elspeth.acceptance.namespace", "value": f"{run_id}-{scenario.lower()}"},
                            ],
                        }
                    ],
                    "xray_retained_trace_ids": [f"1-1234567{0 if scenario == 'A' else 1}-{'a' if scenario == 'A' else 'b'}" + "0" * 23],
                    "expected_retained_metric_series": 1,
                    "expected_retained_trace_ids": 1,
                }
                for scenario in ("A", "B")
            },
            "captured_at": "2026-07-14T01:01:20Z",
        }
        if retained_mutator is not None:
            retained_mutator(retained_evidence)
        retained_path = path.parent / "retained-evidence.json"
        retained_path.write_text(json.dumps(retained_evidence))
        os.chmod(retained_path, 0o600)
        acceptance.control_manifest_bind_retained_evidence(
            path,
            receipt_path=str(retained_path),
            now=lambda: datetime(2026, 7, 14, 1, 1, 30, tzinfo=UTC),
        )
    return json.loads(path.read_text())


def test_control_manifest_init_update_validate_get_and_cleanup_assignments_are_closed_and_atomic(tmp_path: Path) -> None:
    path = tmp_path / "control.json"
    manifest = _init_control_manifest(path)

    assert path.stat().st_mode & 0o777 == 0o600
    assert manifest["schema"] == "elspeth.aws-ecs-control-manifest.v5"
    acceptance.control_manifest_validate(
        path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        candidate_sha="c" * 40,
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    updated = acceptance.control_manifest_update(
        path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )
    assert updated["cleanup_required"] is True
    assert acceptance.control_manifest_get(path, "cleanup_states.orphan_sweep") == "pending"
    assignments = acceptance.control_manifest_load_cleanup(
        path,
        now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
    )
    assert assignments.splitlines() == [
        "ACCEPTANCE_REENTRY_FORBIDDEN=0",
        "ACCEPTANCE_RUN_ID=4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        "ACCEPTANCE_TEARDOWN_DEADLINE_UTC=2026-07-14T05:00:00Z",
        "AWS_ACCOUNT_ID=123456789012",
        "AWS_REGION=ap-southeast-2",
        "CANDIDATE_SHA=cccccccccccccccccccccccccccccccccccccccc",
        "CANDIDATE_TAG=acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        "CLEANUP_REQUIRED=1",
        "DEADLINE_EXPIRED=0",
        "ELSPETH_CLEANUP_MODE=1",
        "ECR_REGISTRY=123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        "ECR_REPOSITORY=elspeth-acceptance",
        "EMERGENCY_CLEANUP_DEADLINE_UTC=''",
        f"GATE_LEDGER={tmp_path}/gate-ledger.json",
        "ROLLBACK_BASELINE_TAG=acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        "ROLLBACK_BASELINE_DIGEST=''",
        "IMAGE_DIGEST=''",
        "ROLLBACK_BASELINE_IMAGE=''",
        "CANDIDATE_IMAGE=''",
        "ACCEPTANCE_STATE=''",
        "OIDC_EVIDENCE_DIR=''",
        f"EVIDENCE_DESTINATION_SHA256={'9' * 64}",
        "EVIDENCE_EXPORT_RECEIPT=''",
        "FINAL_EVIDENCE_EXPORT_RECEIPT=''",
        f"SCENARIO_A_INVENTORY={tmp_path}/scenario-a.json",
        "SCENARIO_A_TF_DIR=/iac/scenario-a",
        "SCENARIO_A_TF_VARS=/iac/scenario-a.tfvars",
        f"SCENARIO_A_TF_BINDING_SHA={manifest['scenarios']['A']['tf_binding_sha256']}",  # type: ignore[index]
        f"SCENARIO_A_TF_BINDING_FILE={tmp_path}/tf-binding-a.json",
        f"SCENARIO_B_INVENTORY={tmp_path}/scenario-b.json",
        "SCENARIO_B_TF_DIR=/iac/scenario-b",
        "SCENARIO_B_TF_VARS=/iac/scenario-b.tfvars",
        f"SCENARIO_B_TF_BINDING_SHA={manifest['scenarios']['B']['tf_binding_sha256']}",  # type: ignore[index]
        f"SCENARIO_B_TF_BINDING_FILE={tmp_path}/tf-binding-b.json",
    ]


def test_control_manifest_deadline_blocks_acceptance_but_records_and_permits_cleanup_only_resume(tmp_path: Path) -> None:
    path = tmp_path / "control.json"
    _init_control_manifest(path, deadline="2026-07-14T02:00:00Z")
    acceptance.control_manifest_update(
        path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 30, tzinfo=UTC),
    )

    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_expired"):
        acceptance.control_manifest_validate(
            path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            candidate_sha="c" * 40,
            now=lambda: datetime(2026, 7, 14, 2, 1, tzinfo=UTC),
        )
    assignments = acceptance.control_manifest_load_cleanup(
        path,
        now=lambda: datetime(2026, 7, 14, 2, 1, tzinfo=UTC),
    )
    assert "DEADLINE_EXPIRED=1" in assignments
    assert "ELSPETH_CLEANUP_MODE=1" in assignments
    assert "EMERGENCY_CLEANUP_DEADLINE_UTC=2026-07-14T05:01:00Z" in assignments
    assert "ACCEPTANCE_REENTRY_FORBIDDEN=1" in assignments
    assert acceptance.control_manifest_get(path, "deadline_failure_recorded") == "true"
    assert acceptance.control_manifest_get(path, "verdict_failures") == '["teardown_deadline"]'
    assert acceptance.control_manifest_get(path, "cleanup_escalations") == '["teardown_deadline"]'

    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_conflict"):
        acceptance.control_manifest_update(
            path,
            verdict_failure="teardown_deadline",
            emergency_cleanup_deadline_utc="2026-07-14T03:32:00Z",
            cleanup_escalation="teardown_deadline",
            now=lambda: datetime(2026, 7, 14, 2, 1, 30, tzinfo=UTC),
        )

    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_expired"):
        acceptance.control_manifest_update(
            path,
            cleanup_required=True,
            ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
            ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-too-late",
            ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
            ecr_repository="elspeth-acceptance",
            now=lambda: datetime(2026, 7, 14, 2, 2, tzinfo=UTC),
        )
    acceptance.control_manifest_update(
        path,
        cleanup_checkpoint="orphan_sweep:confirmed",
        now=lambda: datetime(2026, 7, 14, 2, 2, tzinfo=UTC),
    )
    assert acceptance.control_manifest_get(path, "cleanup_states.orphan_sweep") == "confirmed"


def test_control_manifest_rejects_existing_init_symlink_permissive_and_wrong_binding(tmp_path: Path) -> None:
    path = tmp_path / "control.json"
    _init_control_manifest(path)
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_exists"):
        _init_control_manifest(path)
    os.chmod(path, 0o644)
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_file"):
        acceptance.control_manifest_get(path, "candidate_sha")

    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_file"):
        acceptance.control_manifest_get(link, "candidate_sha")

    os.chmod(path, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_binding"):
        acceptance.control_manifest_validate(
            path,
            acceptance_run_id="64b984d2-b617-42f7-ac4f-c0955ea9aadc",
            candidate_sha="c" * 40,
            now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
        )


def test_control_manifest_rejects_shared_terraform_state_and_foreign_scenario_resource(tmp_path: Path) -> None:
    def share_state(receipt: dict[str, object], scenario: str) -> None:
        if scenario == "B":
            receipt["backend_state_key_sha256"] = hashlib.sha256(b"state-A").hexdigest()
            receipt["workspace"] = "acceptance-a"

    with pytest.raises(acceptance.AcceptanceCheckError, match="tf_binding_binding"):
        _init_control_manifest(tmp_path / "shared-state.json", binding_mutator=share_state)

    def foreign_arn(inventory: dict[str, object], scenario: str) -> None:
        if scenario == "A":
            values = inventory["values"]
            assert isinstance(values, dict)
            values["TARGET_GROUP_ARN"] = "arn:aws:elasticloadbalancing:us-east-1:999999999999:targetgroup/foreign/0123456789abcdef"

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        _init_control_manifest(tmp_path / "foreign-resource.json", inventory_mutator=foreign_arn)


def test_tf_binding_rejects_non_ascii_terraform_version(tmp_path: Path) -> None:
    def non_ascii_version(receipt: dict[str, object], scenario: str) -> None:
        if scenario == "A":
            receipt["terraform_version"] = "1.9.0-ß"

    with pytest.raises(acceptance.AcceptanceCheckError, match="tf_binding_schema"):
        _init_control_manifest(tmp_path / "non-ascii-version.json", binding_mutator=non_ascii_version)

    def drift_policy_binding(inventory: dict[str, object], scenario: str) -> None:
        if scenario == "A":
            values = inventory["values"]
            assert isinstance(values, dict)
            values["ELSPETH_WEB__PLUGIN_ALLOWLIST"] = "[]"

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        _init_control_manifest(tmp_path / "policy-drift.json", inventory_mutator=drift_policy_binding)


def test_scenario_inventory_binds_listener_rule_to_its_parent_listener(tmp_path: Path) -> None:
    def replace_listener_with_rule(inventory: dict[str, object], scenario: str) -> None:
        if scenario != "A":
            return
        values = inventory["values"]
        orphan = inventory["orphan_sweep"]
        assert isinstance(values, dict)
        assert isinstance(orphan, dict)
        orphan["elbv2_listener_arns"] = [values["FIRST_DEPLOY_LISTENER_RULE_ARN"]]

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        _init_control_manifest(tmp_path / "rule-as-listener.json", inventory_mutator=replace_listener_with_rule)

    def omit_upgrade_listener(inventory: dict[str, object], scenario: str) -> None:
        if scenario == "B":
            orphan = inventory["orphan_sweep"]
            assert isinstance(orphan, dict)
            orphan["elbv2_listener_arns"] = []

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        _init_control_manifest(tmp_path / "missing-upgrade-listener.json", inventory_mutator=omit_upgrade_listener)


def test_scenario_load_is_exact_shell_round_trippable_and_rejects_inventory_drift(tmp_path: Path) -> None:
    path = tmp_path / "control.json"
    manifest = _init_control_manifest(path)
    assignments = acceptance.scenario_load(
        path,
        scenario_id="A",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    assert "OIDC_TEST_USERNAME" not in assignments
    assert "PASSWORD" not in assignments
    script = f"{assignments}\nprintf '%s\\n' \"$ACTIVE_SCENARIO_ID|$ECS_CLUSTER|$SCENARIO_TF_BINDING_SHA\""
    completed = subprocess.run(
        ["env", "-i", "bash", "--noprofile", "--norc", "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    namespace = acceptance.scenario_resource_namespace("4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48", "A")
    assert completed.stdout == f"A|acceptance-{namespace}-cluster|{manifest['scenarios']['A']['tf_binding_sha256']}\n"  # type: ignore[index]

    inventory = tmp_path / "scenario-a.json"
    drifted = json.loads(inventory.read_text())
    drifted["values"]["ECS_CLUSTER"] = "unbound-drift"
    inventory.write_text(json.dumps(drifted))
    os.chmod(inventory, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        acceptance.scenario_load(
            path,
            scenario_id="A",
            now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
        )


def _task_definition_policy_payload(
    tmp_path: Path,
    *,
    record_ecr: bool = True,
    composer_model: str = "openrouter/openai/gpt-5.4",
    composer_advisor_model: str = "openrouter/anthropic/claude-opus-4.6",
) -> tuple[Path, str, dict[str, Any], dict[str, Any]]:
    manifest_path = tmp_path / "control.json"

    def bind_composer_models(inventory: dict[str, object], _scenario_id: str) -> None:
        values = inventory["values"]
        assert isinstance(values, dict)
        values["ELSPETH_WEB__COMPOSER_MODEL"] = composer_model
        values["ELSPETH_WEB__COMPOSER_ADVISOR_MODEL"] = composer_advisor_model

    _init_control_manifest(
        manifest_path,
        inventory_mutator=bind_composer_models,
        preapply_inventory_mutator=bind_composer_models,
    )
    if record_ecr:
        acceptance.control_manifest_update(
            manifest_path,
            cleanup_required=True,
            ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
            ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
            ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
            ecr_repository="elspeth-acceptance",
            now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
        )
        acceptance.control_manifest_update(
            manifest_path,
            ecr_baseline_digest="sha256:" + "b" * 64,
            ecr_candidate_digest="sha256:" + "d" * 64,
            now=lambda: datetime(2026, 7, 14, 1, 2, 10, tzinfo=UTC),
        )
    inventory = json.loads((tmp_path / "scenario-a.json").read_text())
    values = inventory["values"]
    container_name = values["WEB_CONTAINER_NAME"]
    environment = [
        {"name": name, "value": values[name]}
        for name in (
            "ELSPETH_WEB__DATA_DIR",
            "ELSPETH_WEB__PAYLOAD_STORE_PATH",
            "ELSPETH_WEB__COMPOSER_MODEL",
            "ELSPETH_WEB__COMPOSER_ADVISOR_MODEL",
            *acceptance.PLUGIN_POLICY_ASSIGNMENT_NAMES,
            "ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256",
            "ELSPETH_BEDROCK_LIVE_TEST_MODEL",
            "AWS_REGION",
        )
    ]
    acceptance_run_id = inventory["acceptance_run_id"]
    environment.extend(
        [
            {"name": "ELSPETH_ACCEPTANCE_RUN_ID", "value": acceptance_run_id},
            {"name": "ELSPETH_ACCEPTANCE_CANDIDATE_SHA", "value": inventory["candidate_sha"]},
            {"name": "ELSPETH_ACCEPTANCE_SCENARIO_ID", "value": "A"},
            {"name": "ELSPETH_ACCEPTANCE_S3_BUCKET", "value": values["ELSPETH_TEST_S3_BUCKET"]},
            {
                "name": "ELSPETH_ACCEPTANCE_S3_PREFIX",
                "value": f"{acceptance.scenario_resource_namespace(acceptance_run_id, 'A')}/{acceptance_run_id}",
            },
        ]
    )
    namespace = acceptance.scenario_resource_namespace(inventory["acceptance_run_id"], "A")
    required_secret_bindings = (
        ("ELSPETH_WEB__SECRET_KEY", f"{namespace}-database-runtime", "secret_key"),
        ("ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY", f"{namespace}-database-runtime", "shareable_link_signing_key"),
        ("ELSPETH_WEB__SESSION_DB_URL", f"{namespace}-database-runtime", "session_url"),
        ("ELSPETH_WEB__LANDSCAPE_URL", f"{namespace}-database-runtime", "landscape_url"),
    )
    if composer_model.startswith("openrouter/") or composer_advisor_model.startswith("openrouter/"):
        required_secret_bindings += (("OPENROUTER_API_KEY", f"{namespace}-openrouter-composer", "openrouter_api_key"),)
    secrets = [
        {
            "name": name,
            "valueFrom": (f"arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:{secret_id}-AbCd12:{json_key}::"),
        }
        for name, secret_id, json_key in required_secret_bindings
    ]
    payload = {
        "taskDefinition": {
            "taskDefinitionArn": "arn:aws:ecs:ap-southeast-2:123456789012:task-definition/elspeth-web:17",
            "status": "ACTIVE",
            "taskRoleArn": f"arn:aws:iam::123456789012:role/{inventory['orphan_sweep']['iam_role_names'][0]}",
            "executionRoleArn": f"arn:aws:iam::123456789012:role/{inventory['orphan_sweep']['iam_role_names'][1]}",
            "containerDefinitions": [
                {
                    "name": container_name,
                    "essential": True,
                    "image": "123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/elspeth-acceptance@sha256:" + "d" * 64,
                    "environment": environment,
                    "secrets": secrets,
                    "mountPoints": [
                        {
                            "sourceVolume": "data",
                            "containerPath": values["ELSPETH_WEB__DATA_DIR"],
                            "readOnly": False,
                        }
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "data",
                    "efsVolumeConfiguration": {
                        "fileSystemId": inventory["orphan_sweep"]["efs_file_system_ids"][0],
                        "transitEncryption": "ENABLED",
                        "authorizationConfig": {
                            "accessPointId": inventory["orphan_sweep"]["efs_access_point_ids"][0],
                            "iam": "ENABLED",
                        },
                    },
                }
            ],
        }
    }
    return manifest_path, container_name, inventory, payload


def test_task_definition_policy_binding_allows_bedrock_models_without_openrouter_secret(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(
        tmp_path,
        composer_model="bedrock/global.anthropic.claude-sonnet-4-6",
        composer_advisor_model="bedrock/global.anthropic.claude-opus-4-6-v1",
    )
    container = payload["taskDefinition"]["containerDefinitions"][0]

    assert "OPENROUTER_API_KEY" not in {entry["name"] for entry in container["secrets"]}
    acceptance.validate_task_definition_policy_binding(
        payload,
        manifest_path=manifest_path,
        scenario_id="A",
        container_name=container_name,
    )


@pytest.mark.parametrize(
    ("composer_model", "composer_advisor_model"),
    [
        ("openrouter/openai/gpt-5.4", "bedrock/global.anthropic.claude-opus-4-6-v1"),
        ("bedrock/global.anthropic.claude-sonnet-4-6", "openrouter/anthropic/claude-opus-4.6"),
    ],
)
def test_task_definition_policy_binding_requires_openrouter_secret_for_mixed_models(
    tmp_path: Path,
    composer_model: str,
    composer_advisor_model: str,
) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(
        tmp_path,
        composer_model=composer_model,
        composer_advisor_model=composer_advisor_model,
    )
    container = payload["taskDefinition"]["containerDefinitions"][0]
    container["secrets"] = [entry for entry in container["secrets"] if entry["name"] != "OPENROUTER_API_KEY"]

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


@pytest.mark.parametrize("reference_kind", ["missing", "unapproved"])
def test_task_definition_policy_binding_rejects_missing_or_unapproved_secret_reference(
    tmp_path: Path,
    reference_kind: str,
) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    container = payload["taskDefinition"]["containerDefinitions"][0]
    entry = next(item for item in container["secrets"] if item["name"] == "ELSPETH_WEB__SESSION_DB_URL")

    acceptance.validate_task_definition_policy_binding(
        payload,
        manifest_path=manifest_path,
        scenario_id="A",
        container_name=container_name,
    )

    if reference_kind == "missing":
        del entry["valueFrom"]
    else:
        entry["valueFrom"] = "arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:unapproved-database-secret-AbCd12:SESSION_DB_URL::"
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


def test_task_definition_policy_binding_requires_complete_runtime_secret_set(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    container = payload["taskDefinition"]["containerDefinitions"][0]
    container["secrets"] = [entry for entry in container["secrets"] if entry["name"] != "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY"]

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


def test_task_definition_policy_binding_requires_exact_runtime_secret_selectors(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    container = payload["taskDefinition"]["containerDefinitions"][0]
    shared_reference = container["secrets"][0]["valueFrom"]
    for entry in container["secrets"]:
        entry["valueFrom"] = shared_reference

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


@pytest.mark.parametrize(
    ("location", "name"),
    [
        ("environment", "ELSPETH_WEB__SECRET_KEY"),
        ("environment", "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY"),
        ("environment", "ELSPETH_WEB__SESSION_DB_URL"),
        ("environment", "ELSPETH_WEB__LANDSCAPE_URL"),
        ("environment", "ELSPETH_FINGERPRINT_KEY"),
        ("environment", "ELSPETH_WEB__OIDC_CLIENT_SECRET"),
        ("environment", "DATABASE_URL"),
        ("environment", "OPENROUTER_API_KEY"),
        ("environment", "AWS_ACCESS_KEY_ID"),
        ("environment", "AWS_PROFILE"),
        ("environment", "AWS_ENDPOINT_URL"),
        ("environment", "AWS_ROLE_ARN"),
        ("secrets", "AWS_ACCESS_KEY_ID"),
    ],
)
def test_task_definition_policy_binding_rejects_plaintext_secrets_and_aws_overrides(
    tmp_path: Path,
    location: str,
    name: str,
) -> None:
    manifest_path, container_name, inventory, payload = _task_definition_policy_payload(tmp_path)
    container = payload["taskDefinition"]["containerDefinitions"][0]
    if location == "environment":
        container["environment"].append({"name": name, "value": "raw-override-sentinel"})
    else:
        secret_id = inventory["orphan_sweep"]["secret_ids"][0]
        container["secrets"].append(
            {
                "name": name,
                "valueFrom": (f"arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:{secret_id}-AbCd12:AWS_ACCESS_KEY_ID::"),
            }
        )

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


@pytest.mark.parametrize(
    "field",
    [
        "ELSPETH_WEB__DATA_DIR",
        "ELSPETH_WEB__PAYLOAD_STORE_PATH",
        *acceptance.PLUGIN_POLICY_ASSIGNMENT_NAMES,
        "ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256",
        "ELSPETH_BEDROCK_LIVE_TEST_MODEL",
        "AWS_REGION",
    ],
)
def test_task_definition_policy_binding_compares_returned_environment_to_protected_inventory(
    tmp_path: Path,
    field: str,
) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    task_definition_arn = payload["taskDefinition"]["taskDefinitionArn"]
    environment = payload["taskDefinition"]["containerDefinitions"][0]["environment"]

    assert (
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )
        == task_definition_arn
    )

    observed = {entry["name"]: entry["value"] for entry in environment}
    observed[field] = "us-east-1" if field == "AWS_REGION" else "substituted"
    if field in acceptance.PLUGIN_POLICY_ASSIGNMENT_NAMES:
        observed["ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256"] = acceptance.plugin_policy_binding_sha256(observed)
    for entry in environment:
        entry["value"] = observed[entry["name"]]

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


def test_task_definition_policy_binding_requires_explicit_nonroot_one_shot_entrypoint(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    container = payload["taskDefinition"]["containerDefinitions"][0]
    container["user"] = "1000:1000"
    container["entryPoint"] = ["python", "-m", "elspeth.web.aws_ecs_acceptance"]

    acceptance.validate_task_definition_policy_binding(
        payload,
        manifest_path=manifest_path,
        scenario_id="A",
        container_name=container_name,
        expected_user="1000:1000",
    )
    task_definition = payload["taskDefinition"]
    original_task_role = task_definition["taskRoleArn"]
    original_execution_role = task_definition["executionRoleArn"]
    task_definition["taskRoleArn"] = original_execution_role
    task_definition["executionRoleArn"] = original_task_role
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
            expected_user="1000:1000",
        )
    task_definition["taskRoleArn"] = original_task_role
    task_definition["executionRoleArn"] = original_execution_role
    task_definition["taskRoleArn"] = "arn:aws:iam::999999999999:role/foreign-task-role"
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
            expected_user="1000:1000",
        )
    task_definition["taskRoleArn"] = original_task_role
    task_definition["volumes"].append(
        {
            "name": "foreign-data",
            "efsVolumeConfiguration": {
                "fileSystemId": "fs-ffffffffffffffffa",
                "transitEncryption": "ENABLED",
                "authorizationConfig": {"accessPointId": "fsap-ffffffffffffffffa", "iam": "ENABLED"},
            },
        }
    )
    container["mountPoints"].append({"sourceVolume": "foreign-data", "containerPath": "/foreign", "readOnly": False})
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
            expected_user="1000:1000",
        )
    task_definition["volumes"].pop()
    container["mountPoints"].pop()
    task_definition["volumes"].append({"name": "data", "host": {}})
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
            expected_user="1000:1000",
        )
    task_definition["volumes"].pop()
    container["user"] = "0"
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
            expected_user="1000:1000",
        )
    container["user"] = "1000:1000"
    payload["taskDefinition"]["volumes"][0]["efsVolumeConfiguration"]["fileSystemId"] = "fs-ffffffffffffffffa"  # type: ignore[index]
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
            expected_user="1000:1000",
        )


def test_task_definition_policy_binding_requires_manifest_pinned_image(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    container = payload["taskDefinition"]["containerDefinitions"][0]

    acceptance.validate_task_definition_policy_binding(
        payload,
        manifest_path=manifest_path,
        scenario_id="A",
        container_name=container_name,
    )

    reference = "123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/elspeth-acceptance"
    for image in (
        f"{reference}:latest",
        f"{reference}@sha256:{'e' * 64}",
        f"{reference}@sha256:{'b' * 64}",
    ):
        container["image"] = image
        with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
            acceptance.validate_task_definition_policy_binding(
                payload,
                manifest_path=manifest_path,
                scenario_id="A",
                container_name=container_name,
            )

    del container["image"]
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


def test_task_definition_policy_binding_binds_rollback_image_to_baseline_digest(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path)
    container = payload["taskDefinition"]["containerDefinitions"][0]

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
            expected_image_role="rollback-baseline",
        )

    container["image"] = "123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/elspeth-acceptance@sha256:" + "b" * 64
    acceptance.validate_task_definition_policy_binding(
        payload,
        manifest_path=manifest_path,
        scenario_id="A",
        container_name=container_name,
        expected_image_role="rollback-baseline",
    )
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )
    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
            expected_image_role="latest",
        )


def test_task_definition_policy_binding_fails_closed_without_recorded_image_identity(tmp_path: Path) -> None:
    manifest_path, container_name, _inventory, payload = _task_definition_policy_payload(tmp_path, record_ecr=False)

    with pytest.raises(acceptance.AcceptanceCheckError, match="task_definition_policy_binding"):
        acceptance.validate_task_definition_policy_binding(
            payload,
            manifest_path=manifest_path,
            scenario_id="A",
            container_name=container_name,
        )


def test_scenario_inventory_requires_atomic_preapply_to_resolved_binding(tmp_path: Path) -> None:
    path = tmp_path / "control.json"
    manifest = _init_control_manifest(path, bind_resolved=False)
    assert manifest["scenarios"]["A"]["inventory_phase"] == "preapply"  # type: ignore[index]
    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_unresolved"):
        acceptance.scenario_load(path, scenario_id="A", now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC))
    assert "SCENARIO_A_TF_DIR=/iac/scenario-a" in acceptance.control_manifest_load_cleanup(
        path, now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC)
    )

    resolved_path = tmp_path / "scenario-a.json"

    def bind_now() -> datetime:
        return datetime(2026, 7, 14, 1, 1, tzinfo=UTC)

    bound = acceptance.control_manifest_bind_scenario(path, scenario_id="A", inventory_path=str(resolved_path), now=bind_now)
    assert bound["scenarios"]["A"]["inventory_phase"] == "resolved"  # type: ignore[index]
    assert "ACTIVE_SCENARIO_ID=A" in acceptance.scenario_load(path, scenario_id="A", now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC))
    assert acceptance.control_manifest_bind_scenario(path, scenario_id="A", inventory_path=str(resolved_path), now=bind_now) == bound

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        acceptance.control_manifest_bind_scenario(
            path,
            scenario_id="A",
            inventory_path=str(tmp_path / "scenario-b.json"),
            now=bind_now,
        )


def test_scenario_inventory_resolves_real_provider_guardrail_profiles(tmp_path: Path) -> None:
    def preapply(inventory: dict[str, object], _scenario: str) -> None:
        values = inventory["values"]
        assert isinstance(values, dict)
        values["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"] = "[]"
        values["ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256"] = acceptance.plugin_policy_binding_sha256(values)

    def resolved(inventory: dict[str, object], _scenario: str) -> None:
        values = inventory["values"]
        orphan = inventory["orphan_sweep"]
        assert isinstance(values, dict)
        assert isinstance(orphan, dict)
        profiles = json.loads(values["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"])
        orphan["bedrock_guardrails"] = [
            {
                "identifier": profile["guardrail_identifier"],
                "versions": [profile["guardrail_version"]],
            }
            for profile in profiles
        ]

    manifest = _init_control_manifest(
        tmp_path / "guardrail-control.json",
        preapply_inventory_mutator=preapply,
        inventory_mutator=resolved,
    )

    assert manifest["scenarios"]["A"]["inventory_phase"] == "resolved"  # type: ignore[index]
    assert manifest["scenarios"]["B"]["inventory_phase"] == "resolved"  # type: ignore[index]


def test_scenario_inventory_rejects_guardrails_not_bound_to_policy_profiles(tmp_path: Path) -> None:
    def mismatched(inventory: dict[str, object], _scenario: str) -> None:
        orphan = inventory["orphan_sweep"]
        assert isinstance(orphan, dict)
        orphan["bedrock_guardrails"] = [{"identifier": "differentguardrail", "versions": ["1"]}]

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        _init_control_manifest(tmp_path / "mismatched-guardrails.json", inventory_mutator=mismatched)


def test_scenario_inventory_rejects_duplicate_profile_guardrail_binding(tmp_path: Path) -> None:
    def duplicate_profile_binding(inventory: dict[str, object], _scenario: str) -> None:
        values = inventory["values"]
        assert isinstance(values, dict)
        profiles = json.loads(values["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"])
        profiles[1]["guardrail_identifier"] = profiles[0]["guardrail_identifier"]
        profiles[1]["guardrail_version"] = profiles[0]["guardrail_version"]
        values["ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES"] = json.dumps(profiles, separators=(",", ":"))
        values["ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256"] = acceptance.plugin_policy_binding_sha256(values)

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        _init_control_manifest(tmp_path / "duplicate-profile-guardrail.json", inventory_mutator=duplicate_profile_binding)


def test_scenario_inventory_bind_requires_apply_evidence_deadline_and_preserved_preapply_contract(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing-evidence-control.json"
    _init_control_manifest(missing_path, bind_resolved=False, prepare_apply_evidence=False)
    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_unresolved"):
        acceptance.control_manifest_bind_scenario(
            missing_path,
            scenario_id="A",
            inventory_path=str(tmp_path / "scenario-a.json"),
            now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
        )

    expired_path = tmp_path / "expired-control.json"
    _init_control_manifest(expired_path, deadline="2026-07-14T02:00:00Z", bind_resolved=False)
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_expired"):
        acceptance.control_manifest_bind_scenario(
            expired_path,
            scenario_id="A",
            inventory_path=str(tmp_path / "scenario-a.json"),
            now=lambda: datetime(2026, 7, 14, 2, 1, tzinfo=UTC),
        )

    def drift_service(inventory: dict[str, object], scenario: str) -> None:
        if scenario == "A":
            values = inventory["values"]
            assert isinstance(values, dict)
            namespace = acceptance.scenario_resource_namespace("4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48", "A")
            values["ECS_SERVICE"] = f"acceptance-{namespace}-changed-service"

    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_conflict"):
        _init_control_manifest(tmp_path / "drift-control.json", inventory_mutator=drift_service)

    preserved_path = tmp_path / "preserved-control.json"
    manifest = _init_control_manifest(preserved_path)
    scenario_a = manifest["scenarios"]["A"]
    assert scenario_a["preapply_inventory_path"].endswith("scenario-a-preapply.json")
    assert scenario_a["preapply_inventory_path"] != scenario_a["inventory_path"]
    assert len(scenario_a["preapply_inventory_sha256"]) == 64
    preapply_path = Path(scenario_a["preapply_inventory_path"])
    preapply = json.loads(preapply_path.read_text())
    preapply["values"]["DB_CLUSTER_IDENTIFIER"] += "-drift"
    preapply_path.write_text(json.dumps(preapply))
    os.chmod(preapply_path, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match="scenario_inventory_binding"):
        acceptance.control_manifest_load_cleanup(
            preserved_path,
            now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
        )


def test_retained_evidence_is_one_way_post_observation_state_and_detects_drift(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path)
    evidence = manifest["evidence"]
    assert evidence["retained_evidence_path"].endswith("retained-evidence.json")
    for scenario in ("A", "B"):
        inventory = json.loads(Path(manifest["scenarios"][scenario]["inventory_path"]).read_text())
        assert inventory["orphan_sweep"]["cloudwatch_retained_metrics"] == []
        assert inventory["orphan_sweep"]["xray_retained_trace_ids"] == []

    second_receipt = tmp_path / "second-retained.json"
    second_receipt.write_text(Path(evidence["retained_evidence_path"]).read_text())
    os.chmod(second_receipt, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match="retained_evidence_conflict"):
        acceptance.control_manifest_bind_retained_evidence(
            manifest_path,
            receipt_path=str(second_receipt),
            now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
        )

    retained_path = Path(evidence["retained_evidence_path"])
    retained = json.loads(retained_path.read_text())
    retained["captured_at"] = "2026-07-14T01:02:00Z"
    retained_path.write_text(json.dumps(retained))
    os.chmod(retained_path, 0o600)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 2, 10, tzinfo=UTC),
    )
    with pytest.raises(acceptance.AcceptanceCheckError, match="retained_evidence_binding"):
        acceptance.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=_empty_orphan_clients(),
            environ={},
        )


def _retained_checkpoint(run_id: str, included: set[str], captured_at: str) -> dict[str, object]:
    return {
        "schema": "elspeth.aws-ecs-retained-evidence.v1",
        "acceptance_run_id": run_id,
        "candidate_sha": "c" * 40,
        "scenarios": {
            scenario: {
                "cloudwatch_retained_metrics": (
                    [
                        {
                            "namespace": "ELSPETH/Acceptance",
                            "metric_name": "CompletedRuns",
                            "dimensions": [{"name": "elspeth.acceptance.namespace", "value": f"{run_id}-{scenario.lower()}"}],
                        }
                    ]
                    if scenario in included
                    else []
                ),
                "xray_retained_trace_ids": (
                    [f"1-1234567{0 if scenario == 'A' else 1}-{'a' if scenario == 'A' else 'b'}" + "0" * 23] if scenario in included else []
                ),
                "expected_retained_metric_series": 1 if scenario in included else 0,
                "expected_retained_trace_ids": 1 if scenario in included else 0,
            }
            for scenario in ("A", "B")
        },
        "captured_at": captured_at,
    }


def test_complete_retained_evidence_requires_paired_metric_and_trace_counts(tmp_path: Path) -> None:
    run_id = "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48"
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_retained=False)
    checkpoint = _retained_checkpoint(run_id, {"A", "B"}, "2026-07-14T01:01:20Z")
    scenarios = checkpoint["scenarios"]
    assert isinstance(scenarios, dict)
    scenario_a = scenarios["A"]
    assert isinstance(scenario_a, dict)
    metrics = scenario_a["cloudwatch_retained_metrics"]
    assert isinstance(metrics, list)
    metrics.append(
        {
            "namespace": "ELSPETH/Acceptance",
            "metric_name": "CompletedRunsDuplicate",
            "dimensions": [
                {"name": "elspeth.acceptance.namespace", "value": f"{run_id}-a"},
            ],
        }
    )
    scenario_a["expected_retained_metric_series"] = 2
    receipt_path = tmp_path / "mismatched-retained.json"
    receipt_path.write_text(json.dumps(checkpoint))
    os.chmod(receipt_path, 0o600)

    with pytest.raises(acceptance.AcceptanceCheckError, match="retained_evidence_schema"):
        acceptance.control_manifest_bind_retained_evidence(
            manifest_path,
            receipt_path=str(receipt_path),
            require_complete=True,
            now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
        )


def test_retained_evidence_checkpoints_grow_monotonically_and_cover_mid_failure(tmp_path: Path) -> None:
    run_id = "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48"
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_retained=False)
    partial_path = tmp_path / "retained-a.json"
    partial_path.write_text(json.dumps(_retained_checkpoint(run_id, {"A"}, "2026-07-14T01:01:20Z")))
    os.chmod(partial_path, 0o600)
    acceptance.control_manifest_bind_retained_evidence(
        manifest_path,
        receipt_path=str(partial_path),
        now=lambda: datetime(2026, 7, 14, 1, 1, 30, tzinfo=UTC),
    )
    with pytest.raises(acceptance.AcceptanceCheckError, match="retained_evidence_incomplete"):
        acceptance.control_manifest_bind_retained_evidence(
            manifest_path,
            receipt_path=str(partial_path),
            require_complete=True,
            now=lambda: datetime(2026, 7, 14, 1, 1, 40, tzinfo=UTC),
        )
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag=f"acceptance-{run_id}-baseline",
        ecr_candidate_tag=f"acceptance-{run_id}-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )
    partial_receipt = acceptance.orphan_sweep(
        manifest_path,
        acceptance_run_id=run_id,
        clients=_empty_orphan_clients(),
        environ={},
        now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
    )
    assert partial_receipt["expected_retained"] == {"metric_series": 1, "trace_ids": 1}
    assert partial_receipt["observed_retained"] == {"metric_series": 1, "trace_ids": 1}

    complete_path = tmp_path / "retained-ab.json"
    complete_path.write_text(json.dumps(_retained_checkpoint(run_id, {"A", "B"}, "2026-07-14T01:04:00Z")))
    os.chmod(complete_path, 0o600)
    acceptance.control_manifest_bind_retained_evidence(
        manifest_path,
        receipt_path=str(complete_path),
        require_complete=True,
        now=lambda: datetime(2026, 7, 14, 1, 4, tzinfo=UTC),
    )
    assert acceptance.control_manifest_get(manifest_path, "evidence.retained_evidence_path") == str(complete_path)

    with pytest.raises(acceptance.AcceptanceCheckError, match="retained_evidence_conflict"):
        acceptance.control_manifest_bind_retained_evidence(
            manifest_path,
            receipt_path=str(partial_path),
            now=lambda: datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
        )


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


class _FakeOrphanClient:
    def __init__(self, responses: Mapping[str, object]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def __getattr__(self, name: str) -> Callable[..., object]:
        def call(**kwargs: object) -> object:
            self.calls.append((name, kwargs))
            response = self.responses[name]
            if callable(response):
                return response(**kwargs)
            if isinstance(response, list):
                if not response:
                    raise AssertionError(f"unexpected extra {name} call")
                return response.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response

        return call

    def close(self) -> None:
        self.closed = True


class _OrphanNotFound(RuntimeError):
    response: ClassVar[dict[str, object]] = {"Error": {"Code": "ResourceNotFoundException"}}


class _OrphanListenerNotFound(RuntimeError):
    response: ClassVar[dict[str, object]] = {"Error": {"Code": "ListenerNotFound"}}


class _OrphanRepositoryNotFound(RuntimeError):
    response: ClassVar[dict[str, object]] = {"Error": {"Code": "RepositoryNotFoundException"}}


class _OrphanNoSuchEntity(RuntimeError):
    response: ClassVar[dict[str, object]] = {"Error": {"Code": "NoSuchEntity"}}


def _empty_orphan_clients(*, tagged: list[dict[str, object]] | None = None) -> acceptance.OrphanSweepClients:
    return acceptance.OrphanSweepClients(
        tagging=_FakeOrphanClient({"get_resources": {"ResourceTagMappingList": tagged or []}}),
        ecs=_FakeOrphanClient(
            {
                "describe_services": {"services": [], "failures": []},
                "list_tasks": {"taskArns": []},
                "list_task_definitions": {"taskDefinitionArns": []},
            }
        ),
        elbv2=_FakeOrphanClient(
            {
                "describe_load_balancers": {"LoadBalancers": []},
                "describe_listeners": {"Listeners": []},
                "describe_rules": {"Rules": []},
                "describe_target_groups": {"TargetGroups": []},
            }
        ),
        rds=_FakeOrphanClient({"describe_db_clusters": {"DBClusters": []}, "describe_db_instances": {"DBInstances": []}}),
        efs=_FakeOrphanClient(
            {
                "describe_file_systems": {"FileSystems": []},
                "describe_access_points": {"AccessPoints": []},
                "describe_mount_targets": {"MountTargets": []},
            }
        ),
        secretsmanager=_FakeOrphanClient({"describe_secret": _OrphanNotFound()}),
        iam=_FakeOrphanClient({"get_role": _OrphanNoSuchEntity()}),
        logs=_FakeOrphanClient({"describe_log_groups": {"logGroups": []}, "describe_resource_policies": {"resourcePolicies": []}}),
        cloudwatch=_FakeOrphanClient(
            {
                "list_dashboards": {"DashboardEntries": []},
                "describe_alarms": {"MetricAlarms": [], "CompositeAlarms": [], "LogAlarms": []},
                "list_metrics": lambda **kwargs: {
                    "Metrics": [
                        {
                            "Namespace": kwargs["Namespace"],
                            "MetricName": kwargs["MetricName"],
                            "Dimensions": kwargs["Dimensions"],
                        }
                    ]
                },
            }
        ),
        xray=_FakeOrphanClient(
            {
                "get_groups": {"Groups": []},
                "get_sampling_rules": {"SamplingRuleRecords": []},
                "batch_get_traces": lambda **kwargs: {
                    "Traces": [{"Id": trace_id} for trace_id in kwargs["TraceIds"]],
                    "UnprocessedTraceIds": [],
                },
                "get_trace_segment_destination": {"Destination": None},
                "get_indexing_rules": {"IndexingRules": []},
            }
        ),
        events=_FakeOrphanClient({"describe_rule": _OrphanNotFound(), "list_targets_by_rule": {"Targets": []}}),
        bedrock=_FakeOrphanClient({"list_guardrails": {"guardrails": []}}),
        cognito=_FakeOrphanClient({"describe_user_pool": _OrphanNotFound(), "list_users": {"Users": []}}),
        ecr=_FakeOrphanClient({"describe_images": {"imageDetails": []}, "batch_delete_image": {"imageIds": [], "failures": []}}),
    )


def test_orphan_sweep_closes_all_clients_emits_only_counts_and_accepts_zero_survivors(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    receipt = acceptance.orphan_sweep(
        manifest_path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        clients=clients,
        environ={},
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )

    assert receipt["schema"] == "elspeth.aws-ecs-orphan-sweep.v1"
    assert receipt["total_unapproved_survivors"] == 0
    assert receipt["ok"] is True
    assert "4adf8a87" not in json.dumps(receipt)
    assert all(client.closed for client in clients)


@pytest.mark.parametrize("surface", ["guardrail-draft", "iam-role", "logs-resource-policy"])
def test_orphan_sweep_rejects_non_taggable_or_unlisted_owned_survivors(tmp_path: Path, surface: str) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    if surface == "guardrail-draft":
        clients.bedrock.responses["list_guardrails"] = {"guardrails": [{"version": "DRAFT"}]}  # type: ignore[union-attr]
    elif surface == "iam-role":
        clients.iam.responses["get_role"] = lambda **kwargs: {"Role": {"RoleName": kwargs["RoleName"]}}  # type: ignore[union-attr]
    else:
        namespace = acceptance.scenario_resource_namespace("4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48", "A")
        clients.logs.responses["describe_resource_policies"] = {  # type: ignore[union-attr]
            "resourcePolicies": [{"policyName": f"{namespace}-delivery-policy"}]
        }

    with pytest.raises(acceptance.AcceptanceCheckError, match="orphan_sweep_survivors"):
        acceptance.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=clients,
            environ={},
            now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
        )


def test_orphan_sweep_accepts_listener_already_removed_by_terraform(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    assert isinstance(clients.elbv2, _FakeOrphanClient)
    clients.elbv2.responses["describe_listeners"] = _OrphanListenerNotFound()

    receipt = acceptance.orphan_sweep(
        manifest_path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        clients=clients,
        environ={},
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )

    assert receipt["ok"] is True
    listener_calls = [kwargs for method, kwargs in clients.elbv2.calls if method == "describe_listeners"]
    assert len(listener_calls) == 2
    assert all("listener-rule/" not in str(call["ListenerArns"][0]) for call in listener_calls)


def test_orphan_sweep_accepts_bootstrap_repository_not_created_or_already_removed(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    assert isinstance(clients.ecr, _FakeOrphanClient)
    clients.ecr.responses["describe_images"] = _OrphanRepositoryNotFound()

    receipt = acceptance.orphan_sweep(
        manifest_path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        clients=clients,
        environ={},
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )

    assert receipt["ok"] is True


@pytest.mark.parametrize("bind_resolved", [False, True])
def test_orphan_sweep_accepts_early_or_mid_failure_before_retained_evidence_is_bound(tmp_path: Path, bind_resolved: bool) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path, bind_resolved=bind_resolved, bind_retained=False)
    assert manifest["evidence"]["retained_evidence_path"] is None  # type: ignore[index]
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )

    receipt = acceptance.orphan_sweep(
        manifest_path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        clients=_empty_orphan_clients(),
        environ={},
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )

    assert receipt["ok"] is True
    assert receipt["expected_retained"] == {"metric_series": 0, "trace_ids": 0}
    assert receipt["observed_retained"] == {"metric_series": 0, "trace_ids": 0}


def test_orphan_sweep_counts_log_alarms_as_unapproved_survivors(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    clients.cloudwatch.responses["describe_alarms"] = {  # type: ignore[union-attr]
        "MetricAlarms": [],
        "CompositeAlarms": [],
        "LogAlarms": [{"AlarmName": "unexpected-log-alarm"}],
    }
    with pytest.raises(acceptance.AcceptanceCheckError, match="orphan_sweep_survivors"):
        acceptance.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=clients,
            environ={},
        )
    assert all(client.closed for client in clients)


def test_transaction_search_projection_accepts_aws_response_without_optional_actual_percentage() -> None:
    assert acceptance._transaction_search_projection(
        destination="CloudWatchLogs",
        indexing_rules=[
            {
                "Name": "Default",
                "Rule": {"Probabilistic": {"DesiredSamplingPercentage": 1.0}},
            }
        ],
        spans_log_group_present=True,
    ) == {
        "destination": "CloudWatchLogs",
        "indexing_rules": [{"name": "Default", "desired_sampling_percentage": 1.0}],
        "spans_log_group_present": True,
    }


def test_orphan_sweep_accepts_aws_response_without_optional_empty_indexing_rules(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    clients.xray.responses["get_indexing_rules"] = {}  # type: ignore[union-attr]

    receipt = acceptance.orphan_sweep(
        manifest_path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        clients=clients,
        environ={},
    )

    assert receipt["total_unapproved_survivors"] == 0
    assert all(client.closed for client in clients)


def test_orphan_sweep_queries_exact_retained_metric_trace_and_transaction_search_identities(tmp_path: Path) -> None:
    trace_id = f"1-12345678-{'a' * 24}"

    def add_retained_identities(receipt: dict[str, object]) -> None:
        scenarios = receipt["scenarios"]
        assert isinstance(scenarios, dict)
        scenario_a = scenarios["A"]
        assert isinstance(scenario_a, dict)
        scenario_a["cloudwatch_retained_metrics"] = [
            {
                "namespace": "ELSPETH/Acceptance",
                "metric_name": "CompletedRuns",
                "dimensions": [
                    {
                        "name": "elspeth.acceptance.namespace",
                        "value": "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-a",
                    },
                ],
            }
        ]
        scenario_a["xray_retained_trace_ids"] = [trace_id]
        scenario_a["expected_retained_metric_series"] = 1
        scenario_a["expected_retained_trace_ids"] = 1

    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, retained_mutator=add_retained_identities)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    receipt = acceptance.orphan_sweep(
        manifest_path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        clients=clients,
        environ={},
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )

    assert receipt["expected_retained"] == {"metric_series": 2, "trace_ids": 2}
    assert receipt["observed_retained"] == {"metric_series": 2, "trace_ids": 2}
    assert (
        "list_metrics",
        {
            "Namespace": "ELSPETH/Acceptance",
            "MetricName": "CompletedRuns",
            "Dimensions": [
                {
                    "Name": "elspeth.acceptance.namespace",
                    "Value": "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-a",
                },
            ],
            "IncludeLinkedAccounts": False,
        },
    ) in clients.cloudwatch.calls  # type: ignore[union-attr]
    assert ("batch_get_traces", {"TraceIds": [trace_id]}) in clients.xray.calls  # type: ignore[union-attr]
    assert [method for method, _kwargs in clients.xray.calls].count("get_trace_segment_destination") == 2  # type: ignore[union-attr]
    assert [method for method, _kwargs in clients.xray.calls].count("get_indexing_rules") == 2  # type: ignore[union-attr]
    assert all(
        kwargs == {}
        for method, kwargs in clients.xray.calls  # type: ignore[union-attr]
        if method == "get_indexing_rules"
    )
    assert any(
        method == "describe_log_groups" and kwargs.get("logGroupNamePrefix") == "aws/spans"
        for method, kwargs in clients.logs.calls  # type: ignore[union-attr]
    )


def test_orphan_sweep_rejects_transaction_search_drift(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    clients.xray.responses["get_trace_segment_destination"] = {"Destination": "CloudWatchLogs"}  # type: ignore[union-attr]

    with pytest.raises(acceptance.AcceptanceCheckError, match="orphan_sweep_survivors"):
        acceptance.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=clients,
            environ={},
        )
    assert all(client.closed for client in clients)


def test_orphan_sweep_rejects_same_count_transaction_rule_drift_and_extra_retained_series(tmp_path: Path) -> None:
    def configure(inventory: dict[str, object], _scenario: str) -> None:
        orphan = inventory["orphan_sweep"]
        assert isinstance(orphan, dict)
        orphan["transaction_search_baseline_sha256"] = hashlib.sha256(
            json.dumps(
                {
                    "destination": None,
                    "indexing_rules": [{"name": "Default", "desired_sampling_percentage": 1.0}],
                    "spans_log_group_present": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    manifest_path = tmp_path / "control.json"
    _init_control_manifest(
        manifest_path,
        inventory_mutator=configure,
        preapply_inventory_mutator=configure,
    )
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    clients.xray.responses["get_indexing_rules"] = {  # type: ignore[union-attr]
        "IndexingRules": [
            {
                "Name": "Default",
                "Rule": {"Probabilistic": {"DesiredSamplingPercentage": 2.0, "ActualSamplingPercentage": 2.0}},
            }
        ]
    }
    clients.cloudwatch.responses["list_metrics"] = {  # type: ignore[union-attr]
        "Metrics": [
            {"Namespace": "ELSPETH/Acceptance", "MetricName": "CompletedRuns", "Dimensions": []},
            {"Namespace": "ELSPETH/Acceptance", "MetricName": "CompletedRuns", "Dimensions": [{"Name": "Extra"}]},
        ]
    }
    with pytest.raises(acceptance.AcceptanceCheckError, match="orphan_sweep_survivors"):
        acceptance.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=clients,
            environ={},
        )


def test_orphan_sweep_rejects_tagged_survivor_and_endpoint_override_without_leaking_identity(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients(tagged=[{"ResourceARN": "arn:aws:ecs:region:account:secret-survivor"}])
    with pytest.raises(acceptance.AcceptanceCheckError, match="orphan_sweep_survivors") as raised:
        acceptance.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=clients,
            environ={},
        )
    assert "secret-survivor" not in str(raised.value)
    assert all(client.closed for client in clients)

    with pytest.raises(acceptance.AcceptanceCheckError, match="orphan_sweep_environment"):
        acceptance.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=_empty_orphan_clients(),
            environ={"AWS_ENDPOINT_URL_ECS": "https://example.invalid"},
        )


def test_orphan_sweep_deletes_ecr_tags_and_moves_owned_active_task_definition_to_tracked_deletion(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    namespace = acceptance.scenario_resource_namespace("4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48", "A")
    task_definition_arn = f"arn:aws:ecs:ap-southeast-2:123456789012:task-definition/acceptance-{namespace}:1"
    clients = _empty_orphan_clients(tagged=[{"ResourceARN": task_definition_arn}])
    clients.ecs.responses.update(  # type: ignore[union-attr]
        {
            "list_task_definitions": [
                {"taskDefinitionArns": [task_definition_arn]},
                {"taskDefinitionArns": []},
                {"taskDefinitionArns": []},
                {"taskDefinitionArns": []},
                {"taskDefinitionArns": []},
                {"taskDefinitionArns": [task_definition_arn]},
                *[{"taskDefinitionArns": []} for _ in range(6)],
            ],
            "describe_task_definition": {
                "taskDefinition": {"taskDefinitionArn": task_definition_arn},
                "tags": [{"key": "ACCEPTANCE_RUN_ID", "value": "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48"}],
            },
            "deregister_task_definition": {"taskDefinition": {"status": "INACTIVE"}},
            "delete_task_definitions": {"taskDefinitions": [{"status": "DELETE_IN_PROGRESS"}], "failures": []},
        }
    )
    clients.ecr.responses.update(  # type: ignore[union-attr]
        {
            "describe_images": [
                {"imageDetails": [{"imageTags": ["baseline"]}]},
                {"imageDetails": []},
                {"imageDetails": [{"imageTags": ["candidate"]}]},
                {"imageDetails": []},
            ],
            "batch_delete_image": {"imageIds": [{"imageDigest": "sha256:opaque"}], "failures": []},
        }
    )

    receipt = acceptance.orphan_sweep(
        manifest_path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        clients=clients,
        environ={},
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )

    assert receipt["total_unapproved_survivors"] == 0
    deletion_receipts = receipt["delete_in_progress_receipts"]
    assert isinstance(deletion_receipts, list) and len(deletion_receipts) == 1
    assert task_definition_arn not in json.dumps(receipt)
    ecr_methods = [method for method, _kwargs in clients.ecr.calls]  # type: ignore[union-attr]
    assert ecr_methods == [
        "describe_images",
        "batch_delete_image",
        "describe_images",
        "describe_images",
        "batch_delete_image",
        "describe_images",
    ]


def test_orphan_sweep_rejects_task_definition_family_prefix_collision(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    clients.ecs.responses["list_task_definitions"] = [  # type: ignore[union-attr]
        {
            "taskDefinitionArns": [
                "arn:aws:ecs:ap-southeast-2:123456789012:task-definition/acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-a-foreign:1"
            ]
        }
    ]

    with pytest.raises(acceptance.AcceptanceCheckError, match="orphan_sweep_binding"):
        acceptance.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=clients,
            environ={},
        )
    assert all(client.closed for client in clients)


def test_orphan_sweep_rejects_repeated_pagination_token_and_closes_clients(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    clients = _empty_orphan_clients()
    clients.tagging.responses["get_resources"] = [  # type: ignore[union-attr]
        {"ResourceTagMappingList": [], "PaginationToken": "repeat"},
        {"ResourceTagMappingList": [], "PaginationToken": "repeat"},
    ]

    with pytest.raises(acceptance.AcceptanceCheckError, match="orphan_sweep_api"):
        acceptance.orphan_sweep(
            manifest_path,
            acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
            clients=clients,
            environ={},
        )
    assert all(client.closed for client in clients)


def _terraform_receipt(*, kind: str = "terraform-plan", deletes: int = 0) -> dict[str, object]:
    return {
        "schema": "elspeth.aws-ecs-sanitized-evidence.v1",
        "kind": kind,
        "projection": {
            "resource_change_count": deletes,
            "create_count": 0,
            "update_count": 0,
            "delete_count": deletes,
            "replace_count": 0,
            "no_op_count": 0,
            "has_delete": deletes > 0,
            "has_replace": False,
        },
    }


def _store_receipt_in_process(
    manifest_path: str,
    receipt_path: str,
    scenario_id: str,
    subject_id: str,
    ready_queue: Any,
    start_event: Any,
    result_queue: Any,
) -> None:
    ready_queue.put(scenario_id)
    if not start_event.wait(timeout=10):
        result_queue.put(("error", scenario_id, "start_timeout"))
        return
    try:
        receipt_hash = acceptance.receipt_store(
            Path(manifest_path),
            scenario_id=scenario_id,
            kind="terraform-plan",
            subject_id=subject_id,
            receipt_file=Path(receipt_path),
        )
    except BaseException as exc:
        result_queue.put(("error", scenario_id, type(exc).__name__))
    else:
        result_queue.put(("ok", scenario_id, receipt_hash))


def test_receipt_store_serializes_processes_and_preserves_both_updates(tmp_path: Path) -> None:
    fcntl = pytest.importorskip("fcntl")
    proc_locks = Path("/proc/locks")
    if not proc_locks.exists():
        pytest.skip("requires Linux /proc/locks waiter visibility")
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_resolved=False, prepare_apply_evidence=False)
    receipt_path = tmp_path / "terraform-receipt.json"
    receipt_path.write_text(json.dumps(_terraform_receipt()))
    os.chmod(receipt_path, 0o600)
    lock_path = manifest_path.with_name(f".{manifest_path.name}.lock")
    lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock_descriptor, fcntl.LOCK_EX)

    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    result_queue = context.Queue()
    start_event = context.Event()
    processes = [
        context.Process(
            target=_store_receipt_in_process,
            args=(
                str(manifest_path),
                str(receipt_path),
                scenario_id,
                subject_id,
                ready_queue,
                start_event,
                result_queue,
            ),
        )
        for scenario_id, subject_id in (("A", "a" * 64), ("B", "b" * 64))
    ]
    try:
        for process in processes:
            process.start()
        assert {ready_queue.get(timeout=20) for _ in processes} == {"A", "B"}
        start_event.set()
        process_ids = {process.pid for process in processes}
        assert None not in process_ids
        lock_inode = lock_path.stat().st_ino
        deadline = time.monotonic() + 10
        waiting_process_ids: set[int] = set()
        while time.monotonic() < deadline:
            waiting_process_ids = {
                int(match.group("pid"))
                for line in proc_locks.read_text().splitlines()
                if (
                    match := re.match(
                        r"^\d+:\s+->\s+FLOCK\s+ADVISORY\s+WRITE\s+(?P<pid>\d+)\s+\S+:(?P<inode>\d+)\s+",
                        line,
                    )
                )
                and int(match.group("inode")) == lock_inode
            }
            if process_ids <= waiting_process_ids:
                break
            if any(process.exitcode is not None for process in processes):
                break
            time.sleep(0.01)
        assert process_ids <= waiting_process_ids
        with pytest.raises(Empty):
            result_queue.get_nowait()
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)
        for process in processes:
            if process.pid is None:
                continue
            process.join(timeout=20)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    for process in processes:
        assert process.exitcode == 0
    outcomes = [result_queue.get(timeout=5) for _ in processes]
    assert {(status, scenario_id) for status, scenario_id, _detail in outcomes} == {("ok", "A"), ("ok", "B")}
    receipts = json.loads(manifest_path.read_text())["evidence"]["receipts"]
    assert {(receipt["scenario_id"], receipt["subject_sha256"]) for receipt in receipts} == {
        ("A", hashlib.sha256(("a" * 64).encode()).hexdigest()),
        ("B", hashlib.sha256(("b" * 64).encode()).hexdigest()),
    }


def test_receipt_store_creates_lock_with_exact_mode_under_restrictive_umask(tmp_path: Path) -> None:
    pytest.importorskip("fcntl")
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_resolved=False, prepare_apply_evidence=False)
    receipt_path = tmp_path / "terraform-receipt.json"
    receipt_path.write_text(json.dumps(_terraform_receipt()))
    os.chmod(receipt_path, 0o600)

    previous_umask = os.umask(0o777)
    try:
        with acceptance._receipt_manifest_write_lock(manifest_path, check="receipt_store_write"):
            pass
    finally:
        os.umask(previous_umask)

    lock_path = manifest_path.with_name(f".{manifest_path.name}.lock")
    assert lock_path.stat().st_mode & 0o777 == 0o600
    acceptance.receipt_store(
        manifest_path,
        scenario_id="A",
        kind="terraform-plan",
        subject_id="a" * 64,
        receipt_file=receipt_path,
    )


def test_receipt_store_persists_only_canonical_sanitized_content_and_checkpoints_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_resolved=False, prepare_apply_evidence=False)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_terraform_receipt()))
    os.chmod(receipt_path, 0o600)

    receipt_hash = acceptance.receipt_store(
        manifest_path,
        scenario_id="A",
        kind="terraform-plan",
        subject_id="d" * 64,
        receipt_file=receipt_path,
        now=lambda: datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
    )
    assert len(receipt_hash) == 64
    stored = manifest_path.parent / f"{manifest_path.name}.receipts" / f"{receipt_hash}.json"
    assert stored.stat().st_mode & 0o777 == 0o600
    assert "d" * 64 not in manifest_path.read_text()
    evidence = json.loads(manifest_path.read_text())["evidence"]
    assert evidence["receipts"] == [
        {
            "scenario_id": "A",
            "kind": "terraform-plan",
            "subject_sha256": hashlib.sha256(("d" * 64).encode()).hexdigest(),
            "receipt_sha256": receipt_hash,
            "stored_at": "2026-07-14T01:05:00Z",
        }
    ]
    assert (
        acceptance.receipt_store(
            manifest_path,
            scenario_id="A",
            kind="terraform-plan",
            subject_id="d" * 64,
            receipt_file=receipt_path,
            now=lambda: datetime(2026, 7, 14, 1, 6, tzinfo=UTC),
        )
        == receipt_hash
    )
    assert len(json.loads(manifest_path.read_text())["evidence"]["receipts"]) == 1


def test_receipt_store_accepts_bootstrap_terraform_but_rejects_application_receipts(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_resolved=False, prepare_apply_evidence=False)
    receipt_path = tmp_path / "bootstrap-plan.json"
    receipt_path.write_text(json.dumps(_terraform_receipt()))
    os.chmod(receipt_path, 0o600)

    receipt_hash = acceptance.receipt_store(
        manifest_path,
        scenario_id="bootstrap",
        kind="terraform-plan",
        subject_id="a" * 64,
        receipt_file=receipt_path,
    )

    assert len(receipt_hash) == 64
    with pytest.raises(acceptance.AcceptanceCheckError, match="receipt_store_binding"):
        acceptance.receipt_store(
            manifest_path,
            scenario_id="bootstrap",
            kind="verify-s3",
            subject_id="task",
            receipt_bytes=json.dumps(_s3_receipt_details()).encode(),
        )

    approval_path = tmp_path / "bootstrap-approval.json"
    approval_path.write_text(
        json.dumps(
            {
                "schema": "elspeth.aws-ecs-approval.v1",
                "acceptance_run_id": "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
                "scenario_id": "bootstrap",
                "kind": "terraform-plan",
                "plan_receipt_hash": receipt_hash,
                "approver_identity": "infrastructure-owner",
                "authority": "terraform-apply",
                "decision": "approved",
                "approved_at": "2026-07-14T01:00:00Z",
                "expires_at": "2026-07-14T02:00:00Z",
                "key_id": "owner-key-1",
                "signature": "opaque-signature",
            }
        )
    )
    os.chmod(approval_path, 0o600)
    approval_hash = acceptance.approval_verify(
        manifest_path,
        scenario_id="bootstrap",
        kind="terraform-plan",
        plan_receipt_hash=receipt_hash,
        approval_file=approval_path,
        signature_verifier=lambda _payload, _signature, _key: True,
        now=lambda: datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
    )
    acceptance.approval_require_current(
        manifest_path,
        scenario_id="bootstrap",
        kind="terraform-plan",
        plan_receipt_hash=receipt_hash,
        approval_hash=approval_hash,
        now=lambda: datetime(2026, 7, 14, 1, 6, tzinfo=UTC),
    )


def test_compatibility_record_is_bound_to_resolved_scenario_and_stored_by_hash(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag=f"acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline-{'a' * 40}",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )
    acceptance.control_manifest_update(
        manifest_path,
        ecr_baseline_digest="sha256:" + "b" * 64,
        ecr_candidate_digest="sha256:" + "d" * 64,
        now=lambda: datetime(2026, 7, 14, 1, 2, 10, tzinfo=UTC),
    )
    inventory = json.loads((tmp_path / "scenario-b.json").read_text())
    record = {
        "schema": "elspeth.aws-ecs-compatibility-record.v2",
        "record_id": "change-123",
        "acceptance_run_id": inventory["acceptance_run_id"],
        "scenario_id": "B",
        "candidate_sha": inventory["candidate_sha"],
        "candidate_image_digest": "sha256:" + "d" * 64,
        "candidate_task_definition": inventory["values"]["CANDIDATE_TASK_DEFINITION"],
        "candidate_doctor_task_definition": inventory["values"]["DOCTOR_TASK_DEFINITION"],
        "candidate_package_version": "0.7.1",
        "previous_source_sha": "a" * 40,
        "previous_image_digest": "sha256:" + "b" * 64,
        "previous_task_definition": inventory["values"]["PREVIOUS_TASK_DEFINITION"],
        "rollback_doctor_task_definition": inventory["values"]["ROLLBACK_DOCTOR_TASK_DEFINITION"],
        "previous_package_version": "0.7.0",
        "schema_facts": {
            "candidate": {
                "session_epoch": SESSION_SCHEMA_EPOCH,
                "landscape_epoch": SQLITE_SCHEMA_EPOCH,
                "run_web_plugin_policy_present": True,
            },
            "previous": {
                "session_epoch": 27,
                "landscape_epoch": 23,
                "run_web_plugin_policy_present": True,
            },
            "structural_changes": (
                "landscape_epoch_23_to_29_token_ownership_artifact_idempotency_sink_effect_ledger_coalesce_receipts_"
                "per_member_failsink_provenance_output_contract_hash_run_scoped_validation_errors_and_token_ancestry_"
                "batch_expansion_claim_and_sidecar_journal_outbox"
            ),
            "semantics_only_changes": "none",
            "archive_export_decision": "required_before_forward_migration",
            "destructive_reset_required": False,
        },
        "forward_compatible": True,
        "backward_compatible": False,
        "rollback_permitted": False,
        "decision": "approved",
        "approver_identity": "database-operator",
        "countersigner_identity": "release-operator",
        "approved_at": "2026-07-14T01:00:00Z",
        "countersigned_at": "2026-07-14T01:01:00Z",
        "expires_at": "2026-07-14T03:00:00Z",
    }
    record_path = tmp_path / "compatibility-b.json"
    record_path.write_text(json.dumps(record))
    os.chmod(record_path, 0o600)

    receipt = acceptance.validate_compatibility_record(
        record_path,
        manifest_path=manifest_path,
        scenario_id="B",
        now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
    )
    claimed_safe_rollback = json.loads(json.dumps(record))
    claimed_safe_rollback["backward_compatible"] = True
    claimed_safe_rollback["rollback_permitted"] = True
    record_path.write_text(json.dumps(claimed_safe_rollback))
    with pytest.raises(acceptance.AcceptanceCheckError, match="compatibility_record_binding"):
        acceptance.validate_compatibility_record(
            record_path,
            manifest_path=manifest_path,
            scenario_id="B",
            now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
        )
    claimed_safe_rollback_receipt = json.loads(json.dumps(receipt))
    claimed_safe_rollback_receipt["backward_compatible"] = True
    claimed_safe_rollback_receipt["rollback_permitted"] = True
    with pytest.raises(acceptance.AcceptanceCheckError, match="receipt_store_schema"):
        acceptance.receipt_store(
            manifest_path,
            scenario_id="B",
            kind="compatibility-record",
            subject_id=receipt["record_sha256"],  # type: ignore[arg-type]
            receipt_bytes=json.dumps(claimed_safe_rollback_receipt).encode(),
            now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
        )
    for path, replacement in (
        (("candidate_doctor_task_definition",), inventory["values"]["CANDIDATE_TASK_DEFINITION"]),
        (("rollback_doctor_task_definition",), inventory["values"]["PREVIOUS_TASK_DEFINITION"]),
        (("previous_source_sha",), "f" * 40),
        (("previous_image_digest",), "sha256:" + "f" * 64),
        (("previous_package_version",), "0.7.1"),
        (("schema_facts", "candidate", "landscape_epoch"), 22),
        (("schema_facts", "candidate", "session_epoch"), SESSION_SCHEMA_EPOCH - 1),
        (("schema_facts", "previous", "session_epoch"), 26),
        (
            ("schema_facts", "structural_changes"),
            "landscape_epoch_23_to_25_token_ownership_and_artifact_idempotency",
        ),
    ):
        mutated = json.loads(json.dumps(record))
        target = mutated
        for segment in path[:-1]:
            target = target[segment]
        target[path[-1]] = replacement
        record_path.write_text(json.dumps(mutated))
        with pytest.raises(acceptance.AcceptanceCheckError, match="compatibility_record_binding"):
            acceptance.validate_compatibility_record(
                record_path,
                manifest_path=manifest_path,
                scenario_id="B",
                now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
            )
    record_path.write_text(json.dumps(record))
    receipt_hash = acceptance.receipt_store(
        manifest_path,
        scenario_id="B",
        kind="compatibility-record",
        subject_id=receipt["record_sha256"],  # type: ignore[arg-type]
        receipt_bytes=json.dumps(receipt).encode(),
        now=lambda: datetime(2026, 7, 14, 1, 4, tzinfo=UTC),
    )

    assert len(receipt_hash) == 64
    assert receipt["approvals_present"] is True
    assert receipt["previous_package_version"] == "0.7.0"
    assert "database-operator" not in json.dumps(receipt)


def test_receipt_store_rejects_unprotected_or_raw_secret_shaped_documents(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps({"schema": "elspeth.test.v1", "password": "raw-secret"}))
    os.chmod(receipt_path, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match="receipt_store_schema") as raised:
        acceptance.receipt_store(
            manifest_path,
            scenario_id="A",
            kind="terraform-plan",
            subject_id="a" * 64,
            receipt_file=receipt_path,
        )
    assert "raw-secret" not in str(raised.value)


def test_receipt_store_binds_exec_receipts_and_allows_shared_content_for_distinct_logical_identities(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_resolved=False, prepare_apply_evidence=False)
    task_arn = "arn:aws:ecs:ap-southeast-2:123456789012:task/cluster/private-task-id"
    env = {
        "ELSPETH_ACCEPTANCE_CANDIDATE_SHA": "c" * 40,
        "ELSPETH_ACCEPTANCE_TASK_ARN": task_arn,
        "ELSPETH_ACCEPTANCE_SCENARIO_ID": "A",
    }
    encoded = acceptance.encode_exec_receipt("verify-s3", _s3_receipt_details(), env)
    receipt = acceptance.extract_exec_receipt(
        encoded,
        expected_candidate_sha="c" * 40,
        expected_task_arn=task_arn,
        expected_scenario_id="A",
        expected_check="verify-s3",
    )
    exec_path = tmp_path / "exec-receipt.json"
    exec_path.write_text(json.dumps(receipt))
    os.chmod(exec_path, 0o600)
    assert (
        len(
            acceptance.receipt_store(
                manifest_path,
                scenario_id="A",
                kind="verify-s3",
                subject_id=task_arn,
                receipt_file=exec_path,
            )
        )
        == 64
    )

    terraform_path = tmp_path / "terraform-receipt.json"
    terraform_path.write_text(json.dumps(_terraform_receipt()))
    os.chmod(terraform_path, 0o600)
    hashes = {
        acceptance.receipt_store(
            manifest_path,
            scenario_id=scenario,
            kind="terraform-noop",
            subject_id="d" * 64,
            receipt_file=terraform_path,
        )
        for scenario in ("A", "B")
    }
    assert len(hashes) == 1
    assert len(json.loads(manifest_path.read_text())["evidence"]["receipts"]) == 3


def test_receipt_store_binds_guardrail_policy_receipt_to_protected_scenario_inventory(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    task_arn = "arn:aws:ecs:ap-southeast-2:123456789012:task/cluster/private-task-id"
    env = {
        "ELSPETH_ACCEPTANCE_CANDIDATE_SHA": "c" * 40,
        "ELSPETH_ACCEPTANCE_TASK_ARN": task_arn,
        "ELSPETH_ACCEPTANCE_SCENARIO_ID": "A",
    }
    details = _guardrail_receipt_details()
    policy = details["plugin_policy"]
    assert isinstance(policy, dict)
    policy["binding_sha256"] = "4" * 64
    encoded = acceptance.encode_exec_receipt("verify-bedrock-guardrails", details, env)
    receipt = acceptance.extract_exec_receipt(
        encoded,
        expected_candidate_sha="c" * 40,
        expected_task_arn=task_arn,
        expected_scenario_id="A",
        expected_check="verify-bedrock-guardrails",
        expected_plugin_policy_binding_sha256="4" * 64,
    )
    receipt_path = tmp_path / "guardrail-receipt.json"
    receipt_path.write_text(json.dumps(receipt))
    os.chmod(receipt_path, 0o600)

    with pytest.raises(acceptance.AcceptanceCheckError, match="receipt_store_binding"):
        acceptance.receipt_store(
            manifest_path,
            scenario_id="A",
            kind="verify-bedrock-guardrails",
            subject_id=task_arn,
            receipt_file=receipt_path,
        )


@pytest.mark.parametrize(
    "document",
    [
        {"schema": "x", "api_key": "secret", "url": "https://user:pass@example.invalid", "payload": "raw"},
        {"schema": "elspeth.aws-ecs-sanitized-evidence.v1", "kind": "terraform-plan", "projection": {"message": "raw"}},
        {"version": 1, "check": "verify-s3", "ok": True, "candidate_sha": "d" * 40},
    ],
)
def test_receipt_store_rejects_open_or_wrongly_bound_receipt_documents(tmp_path: Path, document: dict[str, object]) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(document))
    os.chmod(receipt_path, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match=r"receipt_store_(?:schema|binding)"):
        acceptance.receipt_store(
            manifest_path,
            scenario_id="A",
            kind="terraform-plan",
            subject_id="d" * 64,
            receipt_file=receipt_path,
        )


def test_receipt_store_accepts_closed_event_delivery_canary_receipt(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    receipt = {
        "schema": "elspeth.aws-ecs-event-canary.v1",
        "delivered": True,
        "removed": True,
    }

    receipt_hash = acceptance.receipt_store(
        manifest_path,
        scenario_id="A",
        kind="deployment-event-canary",
        subject_id="a-0123456789abcdef0123-deployments",
        receipt_bytes=json.dumps(receipt).encode(),
    )

    assert receipt_hash == hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_approval_verify_binds_receipt_run_scenario_authority_decision_and_expiry_with_injected_verifier(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_resolved=False, prepare_apply_evidence=False)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_terraform_receipt()))
    os.chmod(receipt_path, 0o600)
    receipt_hash = acceptance.receipt_store(
        manifest_path,
        scenario_id="A",
        kind="terraform-plan",
        subject_id="a" * 64,
        receipt_file=receipt_path,
        now=lambda: datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
    )
    approval_path = tmp_path / "approval.json"
    approval = {
        "schema": "elspeth.aws-ecs-approval.v1",
        "acceptance_run_id": "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        "scenario_id": "A",
        "kind": "terraform-plan",
        "plan_receipt_hash": receipt_hash,
        "approver_identity": "infrastructure-owner",
        "authority": "terraform-apply",
        "decision": "approved",
        "approved_at": "2026-07-14T01:06:00Z",
        "expires_at": "2026-07-14T02:06:00Z",
        "key_id": "owner-key-1",
        "signature": "opaque-signature",
    }
    approval_path.write_text(json.dumps(approval))
    os.chmod(approval_path, 0o600)
    verified: list[tuple[bytes, str, str]] = []

    def verifier(payload: bytes, signature: str, key_id: str) -> bool:
        verified.append((payload, signature, key_id))
        return True

    approval_hash = acceptance.approval_verify(
        manifest_path,
        scenario_id="A",
        kind="terraform-plan",
        plan_receipt_hash=receipt_hash,
        approval_file=approval_path,
        signature_verifier=verifier,
        now=lambda: datetime(2026, 7, 14, 1, 7, tzinfo=UTC),
    )
    assert len(approval_hash) == 64
    assert verified and b"opaque-signature" not in verified[0][0]
    assert verified[0][1:] == ("opaque-signature", "owner-key-1")
    acceptance.approval_require_current(
        manifest_path,
        scenario_id="A",
        kind="terraform-plan",
        plan_receipt_hash=receipt_hash,
        approval_hash=approval_hash,
        now=lambda: datetime(2026, 7, 14, 1, 7, 5, tzinfo=UTC),
    )
    with pytest.raises(acceptance.AcceptanceCheckError, match="approval_expired"):
        acceptance.approval_require_current(
            manifest_path,
            scenario_id="A",
            kind="terraform-plan",
            plan_receipt_hash=receipt_hash,
            approval_hash=approval_hash,
            now=lambda: datetime(2026, 7, 14, 2, 7, tzinfo=UTC),
        )

    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_update"):
        acceptance.control_manifest_update(
            manifest_path,
            terraform_plan_receipt=f"A:{'a' * 64}:{'f' * 64}:{approval_hash}",
            now=lambda: datetime(2026, 7, 14, 1, 7, 10, tzinfo=UTC),
        )
    plan_binding = f"A:{'a' * 64}:{receipt_hash}:{approval_hash}"
    acceptance.control_manifest_update(
        manifest_path,
        terraform_plan_receipt=plan_binding,
        now=lambda: datetime(2026, 7, 14, 1, 7, 20, tzinfo=UTC),
    )
    with pytest.raises(acceptance.AcceptanceCheckError, match="approval_expired"):
        acceptance.control_manifest_update(
            manifest_path,
            terraform_applied=plan_binding,
            now=lambda: datetime(2026, 7, 14, 2, 7, tzinfo=UTC),
        )
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_update"):
        acceptance.control_manifest_update(
            manifest_path,
            terraform_applied=f"A:{'b' * 64}:{receipt_hash}:{approval_hash}",
            now=lambda: datetime(2026, 7, 14, 1, 7, 30, tzinfo=UTC),
        )
    acceptance.control_manifest_update(
        manifest_path,
        terraform_applied=plan_binding,
        now=lambda: datetime(2026, 7, 14, 1, 7, 40, tzinfo=UTC),
    )
    noop_path = tmp_path / "noop.json"
    noop_path.write_text(json.dumps(_terraform_receipt()))
    os.chmod(noop_path, 0o600)
    noop_hash = acceptance.receipt_store(
        manifest_path,
        scenario_id="A",
        kind="terraform-noop",
        subject_id="b" * 64,
        receipt_file=noop_path,
        now=lambda: datetime(2026, 7, 14, 1, 7, 50, tzinfo=UTC),
    )
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_update"):
        acceptance.control_manifest_update(
            manifest_path,
            terraform_noop_receipt=f"A:{'e' * 64}",
            now=lambda: datetime(2026, 7, 14, 1, 8, tzinfo=UTC),
        )
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_update"):
        acceptance.control_manifest_update(
            manifest_path,
            terraform_noop_receipt=f"A:{'c' * 64}:{noop_hash}",
            now=lambda: datetime(2026, 7, 14, 1, 8, 5, tzinfo=UTC),
        )
    acceptance.control_manifest_update(
        manifest_path,
        terraform_noop_receipt=f"A:{'b' * 64}:{noop_hash}",
        now=lambda: datetime(2026, 7, 14, 1, 8, 10, tzinfo=UTC),
    )

    with pytest.raises(acceptance.AcceptanceCheckError, match="approval_expired"):
        acceptance.approval_verify(
            manifest_path,
            scenario_id="A",
            kind="terraform-plan",
            plan_receipt_hash=receipt_hash,
            approval_file=approval_path,
            signature_verifier=verifier,
            now=lambda: datetime(2026, 7, 14, 2, 7, tzinfo=UTC),
        )


def test_approval_verify_fails_closed_without_configured_signature_verifier(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_resolved=False, prepare_apply_evidence=False)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text("{}")
    os.chmod(approval_path, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match="approval_verifier"):
        acceptance.approval_verify(
            manifest_path,
            scenario_id="A",
            kind="terraform-plan",
            plan_receipt_hash="a" * 64,
            approval_file=approval_path,
            environ={},
        )


def test_approval_verify_uses_protected_ed25519_keyring_when_no_verifier_is_injected(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_resolved=False, prepare_apply_evidence=False)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_terraform_receipt()))
    os.chmod(receipt_path, 0o600)
    receipt_hash = acceptance.receipt_store(
        manifest_path,
        scenario_id="A",
        kind="terraform-plan",
        subject_id="a" * 64,
        receipt_file=receipt_path,
        now=lambda: datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
    )
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    keyring_path = tmp_path / "approval-keyring.json"
    keyring_path.write_text(
        json.dumps(
            {
                "schema": "elspeth.aws-ecs-approval-keyring.v1",
                "keys": {"owner-key-1": base64.urlsafe_b64encode(public_key).decode().rstrip("=")},
            }
        )
    )
    os.chmod(keyring_path, 0o600)
    approval = {
        "schema": "elspeth.aws-ecs-approval.v1",
        "acceptance_run_id": "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        "scenario_id": "A",
        "kind": "terraform-plan",
        "plan_receipt_hash": receipt_hash,
        "approver_identity": "infrastructure-owner",
        "authority": "terraform-apply",
        "decision": "approved",
        "approved_at": "2026-07-14T01:06:00Z",
        "expires_at": "2026-07-14T02:06:00Z",
        "key_id": "owner-key-1",
    }
    canonical = json.dumps(approval, sort_keys=True, separators=(",", ":")).encode()
    approval["signature"] = base64.urlsafe_b64encode(private_key.sign(canonical)).decode().rstrip("=")
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(approval))
    os.chmod(approval_path, 0o600)

    approval_hash = acceptance.approval_verify(
        manifest_path,
        scenario_id="A",
        kind="terraform-plan",
        plan_receipt_hash=receipt_hash,
        approval_file=approval_path,
        environ={"ELSPETH_ACCEPTANCE_APPROVAL_KEYRING": str(keyring_path)},
        now=lambda: datetime(2026, 7, 14, 1, 7, tzinfo=UTC),
    )

    assert len(approval_hash) == 64


def test_sanitize_evidence_projects_logs_task_definitions_and_terraform_without_free_form_content() -> None:
    secret = "credential://user:password@provider.invalid/raw-request-id"
    logs = acceptance.sanitize_evidence(
        "web-log",
        {
            "events": [
                {
                    "timestamp": 1234,
                    "message": json.dumps(
                        {
                            "event_name": "startup_complete",
                            "severity": "info",
                            "ok": True,
                            "message": secret,
                            "url": secret,
                        }
                    ),
                }
            ],
            "nextToken": secret,
        },
    )
    assert logs == {
        "schema": "elspeth.aws-ecs-sanitized-evidence.v1",
        "kind": "web-log",
        "records": [{"timestamp": 1234, "event_name": "startup_complete", "severity": "info", "ok": True}],
        "counts": {"input": 1, "projected": 1},
    }

    task_definition = acceptance.sanitize_evidence(
        "task-definition",
        {
            "taskDefinition": {
                "taskDefinitionArn": secret,
                "revision": 17,
                "networkMode": "awsvpc",
                "containerDefinitions": [{"environment": [{"value": secret}]}, {}],
                "volumes": [{}],
                "requiresCompatibilities": ["FARGATE"],
            }
        },
    )
    assert task_definition["projection"] == {
        "revision": 17,
        "network_mode": "awsvpc",
        "container_count": 2,
        "volume_count": 1,
        "fargate_required": True,
    }

    terraform = acceptance.sanitize_evidence(
        "terraform-plan",
        {
            "resource_changes": [
                {"address": secret, "change": {"actions": ["create"]}},
                {"address": secret, "change": {"actions": ["delete", "create"]}},
                {"address": secret, "change": {"actions": ["no-op"]}},
            ],
            "planned_values": {"root_module": {"resources": [{"values": {"password": secret}}]}},
        },
    )
    assert terraform["projection"] == {
        "resource_change_count": 3,
        "create_count": 1,
        "update_count": 0,
        "delete_count": 0,
        "replace_count": 1,
        "no_op_count": 1,
        "has_delete": False,
        "has_replace": True,
    }
    assert secret not in json.dumps([logs, task_definition, terraform])


@pytest.mark.parametrize("kind", sorted(acceptance.EVIDENCE_KINDS))
def test_sanitize_evidence_rejects_malformed_top_level_for_every_kind(kind: str) -> None:
    with pytest.raises(acceptance.AcceptanceCheckError, match="sanitize_evidence_schema"):
        acceptance.sanitize_evidence(kind, ["raw-provider-response"])


def _bind_gate_ledger_candidate(ledger_path: Path) -> None:
    ledger = json.loads(ledger_path.read_text())
    if ledger["candidate_sha"] is None:
        existing = {record["check_id"] for record in ledger["records"]}
        for check_id in acceptance._TASK1_GATE_CHECK_ORDER:
            if check_id in existing:
                continue
            acceptance.gate_ledger_record(
                ledger_path,
                check_id=check_id,
                exit_status=0,
                receipt_hash=hashlib.sha256(check_id.encode()).hexdigest(),
                candidate_sha="c" * 40,
                now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
            )
        acceptance.gate_ledger_bind_candidate(
            ledger_path,
            candidate_sha="c" * 40,
            now=lambda: datetime(2026, 7, 14, 1, 1, 30, tzinfo=UTC),
        )


def _fill_gate_ledger_prefix(ledger_path: Path) -> None:
    _bind_gate_ledger_candidate(ledger_path)
    existing = {record["check_id"] for record in json.loads(ledger_path.read_text())["records"]}
    for check_id in acceptance._SUCCESS_GATE_CHECK_ORDER:
        if check_id in existing:
            continue
        acceptance.gate_ledger_record(
            ledger_path,
            check_id=check_id,
            exit_status=0,
            receipt_hash=hashlib.sha256(check_id.encode()).hexdigest(),
            candidate_sha="c" * 40,
            now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
        )


def _fill_cleanup_gate_prefix(ledger_path: Path) -> None:
    existing = {record["check_id"] for record in json.loads(ledger_path.read_text())["cleanup_records"]}
    for check_id in acceptance._CLEANUP_GATE_CHECK_ORDER[:-1]:
        if check_id in existing:
            continue
        acceptance.gate_ledger_record_cleanup(
            ledger_path,
            check_id=check_id,
            exit_status=0,
            receipt_hash=hashlib.sha256(check_id.encode()).hexdigest(),
            candidate_sha="c" * 40,
            now=lambda: datetime(2026, 7, 14, 1, 2, 10, tzinfo=UTC),
        )


def _gate_ledger_init(ledger_path: Path) -> dict[str, object]:
    return acceptance.gate_ledger_init(
        ledger_path,
        branch="feat/aws-ecs-program",
        starting_sha="a" * 40,
        plan_sha256="1" * 64,
        program_base_sha="2" * 40,
        reconciled_release_sha="3" * 40,
        now=lambda: datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
    )


def _checkpoint_export_phase(manifest_path: Path, ledger_path: Path, *, final: bool) -> None:
    manifest = json.loads(manifest_path.read_text())
    ledger = json.loads(ledger_path.read_text())
    receipts_sha256 = hashlib.sha256(
        json.dumps(
            {
                "receipts": manifest["evidence"]["receipts"],
                "approvals": manifest["evidence"]["approvals"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    ledger_records_sha256 = acceptance._gate_ledger_records_hash(ledger)
    suffix = "final-export-receipt" if final else "export-receipt"
    receipt_path = manifest_path.with_name(f"{manifest_path.name}.{suffix}.json")
    receipt_path.write_text(
        json.dumps(
            {
                "schema": "elspeth.aws-ecs-evidence-export.v1",
                "acceptance_run_id": manifest["acceptance_run_id"],
                "destination_sha256": manifest["evidence"]["destination_sha256"],
                "receipts_sha256": receipts_sha256,
                "ledger_records_sha256": ledger_records_sha256,
                "artifact_count": 1,
                "exported_at": "2026-07-14T01:02:30Z",
                "verified": True,
            }
        )
    )
    os.chmod(receipt_path, 0o600)
    if final:
        acceptance.control_manifest_update(
            manifest_path,
            final_evidence_export_receipt=str(receipt_path),
            now=lambda: datetime(2026, 7, 14, 1, 2, 31, tzinfo=UTC),
        )
    else:
        acceptance.control_manifest_update(
            manifest_path,
            evidence_export_receipt=str(receipt_path),
            now=lambda: datetime(2026, 7, 14, 1, 2, 30, tzinfo=UTC),
        )


def _checkpoint_evidence_export(manifest_path: Path, ledger_path: Path) -> None:
    _checkpoint_export_phase(manifest_path, ledger_path, final=False)
    _checkpoint_export_phase(manifest_path, ledger_path, final=True)


def test_create_evidence_export_receipt_derives_current_manifest_and_ledger_hashes(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path)
    ledger_path = Path(manifest["gate_ledger_path"])
    _gate_ledger_init(ledger_path)
    _fill_gate_ledger_prefix(ledger_path)
    output_path = tmp_path / "initial-export.json"

    receipt = acceptance.create_evidence_export_receipt(
        manifest_path,
        ledger_path=ledger_path,
        output_path=output_path,
        artifact_count=10,
        now=lambda: datetime(2026, 7, 14, 1, 2, 30, tzinfo=UTC),
    )

    assert receipt["verified"] is True
    assert receipt["artifact_count"] == 10
    assert receipt["acceptance_run_id"] == manifest["acceptance_run_id"]
    assert output_path.stat().st_mode & 0o777 == 0o600
    acceptance.control_manifest_update(
        manifest_path,
        evidence_export_receipt=str(output_path),
        now=lambda: datetime(2026, 7, 14, 1, 2, 31, tzinfo=UTC),
    )


def test_final_evidence_export_refreshes_receipts_created_during_cleanup(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path)
    ledger_path = Path(manifest["gate_ledger_path"])
    _gate_ledger_init(ledger_path)
    _fill_gate_ledger_prefix(ledger_path)
    _checkpoint_export_phase(manifest_path, ledger_path, final=False)
    baseline_evidence = json.loads(manifest_path.read_text())["evidence"]
    baseline_evidence_count = len(baseline_evidence["receipts"]) + len(baseline_evidence["approvals"])

    receipt_path = tmp_path / "destroy-receipt.json"
    receipt_path.write_text(json.dumps(_terraform_receipt(kind="terraform-destroy-plan", deletes=1)))
    os.chmod(receipt_path, 0o600)
    acceptance.receipt_store(
        manifest_path,
        scenario_id="A",
        kind="terraform-destroy-plan",
        subject_id="d" * 64,
        receipt_file=receipt_path,
    )
    _fill_cleanup_gate_prefix(ledger_path)
    with pytest.raises(acceptance.AcceptanceCheckError, match="cleanup_finalize_export"):
        acceptance.cleanup_evidence_finalize(
            manifest_path,
            ledger_path=ledger_path,
            phase="prepare",
            clear_cleanup_required=False,
        )

    _checkpoint_export_phase(manifest_path, ledger_path, final=True)
    prepared = acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="prepare",
        clear_cleanup_required=False,
    )
    assert prepared["final_evidence"]["receipt_count"] == baseline_evidence_count + 1  # type: ignore[index]


def test_initial_evidence_export_binding_replays_after_cleanup_evidence_advances(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path)
    ledger_path = Path(manifest["gate_ledger_path"])
    _gate_ledger_init(ledger_path)
    _fill_gate_ledger_prefix(ledger_path)
    _checkpoint_export_phase(manifest_path, ledger_path, final=False)
    checkpointed = json.loads(manifest_path.read_text())
    initial_path = checkpointed["evidence"]["export_receipt_path"]
    initial_hash = checkpointed["evidence"]["export_receipt_sha256"]

    receipt_path = tmp_path / "destroy-plan-receipt.json"
    receipt_path.write_text(json.dumps(_terraform_receipt(kind="terraform-destroy-plan", deletes=1)))
    os.chmod(receipt_path, 0o600)
    acceptance.receipt_store(
        manifest_path,
        scenario_id="A",
        kind="terraform-destroy-plan",
        subject_id="d" * 64,
        receipt_file=receipt_path,
    )

    replayed = acceptance.control_manifest_update(
        manifest_path,
        evidence_export_receipt=initial_path,
        now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
    )

    assert replayed["evidence"]["export_receipt_path"] == initial_path  # type: ignore[index]
    assert replayed["evidence"]["export_receipt_sha256"] == initial_hash  # type: ignore[index]


def test_final_evidence_export_requires_distinct_path_and_preserves_initial_receipt(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path)
    ledger_path = Path(manifest["gate_ledger_path"])
    _gate_ledger_init(ledger_path)
    _fill_gate_ledger_prefix(ledger_path)
    _checkpoint_export_phase(manifest_path, ledger_path, final=False)
    checkpointed = json.loads(manifest_path.read_text())
    initial_path = Path(checkpointed["evidence"]["export_receipt_path"])

    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_conflict"):
        acceptance.control_manifest_update(
            manifest_path,
            final_evidence_export_receipt=str(initial_path),
            now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
        )

    overwritten = json.loads(initial_path.read_text())
    overwritten["exported_at"] = "2026-07-14T01:03:10Z"
    initial_path.write_text(json.dumps(overwritten))
    os.chmod(initial_path, 0o600)
    final_path = tmp_path / "distinct-final-export.json"
    final_path.write_text(json.dumps(overwritten))
    os.chmod(final_path, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match="evidence_export_binding"):
        acceptance.control_manifest_update(
            manifest_path,
            final_evidence_export_receipt=str(final_path),
            now=lambda: datetime(2026, 7, 14, 1, 3, 20, tzinfo=UTC),
        )


def test_cleanup_evidence_finalize_is_two_phase_refuses_pending_and_clears_only_after_all_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path)
    ledger_path = Path(manifest["gate_ledger_path"])
    _gate_ledger_init(ledger_path)
    acceptance.gate_ledger_record(
        ledger_path,
        check_id="candidate",
        exit_status=0,
        receipt_hash="b" * 64,
        candidate_sha="c" * 40,
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )
    _fill_gate_ledger_prefix(ledger_path)
    _fill_cleanup_gate_prefix(ledger_path)
    _checkpoint_evidence_export(manifest_path, ledger_path)
    prepared = acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="prepare",
        clear_cleanup_required=False,
        now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
    )
    assert prepared["cleanup_required"] is True
    with pytest.raises(acceptance.AcceptanceCheckError, match="cleanup_finalize_pending"):
        acceptance.cleanup_evidence_finalize(
            manifest_path,
            ledger_path=ledger_path,
            phase="commit",
            clear_cleanup_required=True,
            now=lambda: datetime(2026, 7, 14, 1, 4, tzinfo=UTC),
        )

    for surface in acceptance.CLEANUP_SURFACES:
        if surface != "coordinator":
            acceptance.control_manifest_update(
                manifest_path,
                cleanup_checkpoint=f"{surface}:confirmed",
                now=lambda: datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
            )
    original_require_mutable = acceptance._require_mutable_control_manifest
    mutation_inside_lock = threading.Event()
    release_mutation = threading.Event()
    finalizer_started = threading.Event()
    finalizer_finished = threading.Event()
    results: dict[str, dict[str, object]] = {}
    errors: list[BaseException] = []

    def pause_late_mutation(manifest: Mapping[str, object]) -> None:
        original_require_mutable(manifest)
        if threading.current_thread().name == "late-manifest-mutator":
            mutation_inside_lock.set()
            if not release_mutation.wait(timeout=5):
                raise AssertionError("timed out waiting to release manifest mutation")

    def mutate_manifest() -> None:
        try:
            results["mutation"] = acceptance.control_manifest_update(
                manifest_path,
                cleanup_checkpoint="coordinator:confirmed",
                now=lambda: datetime(2026, 7, 14, 1, 5, 30, tzinfo=UTC),
            )
        except BaseException as exc:
            errors.append(exc)

    def finalize_manifest() -> None:
        finalizer_started.set()
        try:
            results["finalizer"] = acceptance.cleanup_evidence_finalize(
                manifest_path,
                ledger_path=ledger_path,
                phase="commit",
                clear_cleanup_required=True,
                now=lambda: datetime(2026, 7, 14, 1, 6, tzinfo=UTC),
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            finalizer_finished.set()

    monkeypatch.setattr(acceptance, "_require_mutable_control_manifest", pause_late_mutation)
    mutation_thread = threading.Thread(target=mutate_manifest, name="late-manifest-mutator")
    finalizer_thread = threading.Thread(target=finalize_manifest, name="final-receipt-writer")
    mutation_thread.start()
    assert mutation_inside_lock.wait(timeout=5)
    finalizer_thread.start()
    assert finalizer_started.wait(timeout=5)
    assert not finalizer_finished.wait(timeout=0.1)
    release_mutation.set()
    mutation_thread.join(timeout=5)
    finalizer_thread.join(timeout=5)
    assert not mutation_thread.is_alive()
    assert not finalizer_thread.is_alive()
    assert errors == []
    committed = results["finalizer"]
    assert committed["cleanup_required"] is False
    final_receipt = manifest_path.with_name(f"{manifest_path.name}.final-receipt.json")
    committed_manifest_bytes = manifest_path.read_bytes()
    committed_receipt_bytes = final_receipt.read_bytes()
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_finalized"):
        acceptance.control_manifest_update(
            path=manifest_path,
            cleanup_checkpoint="coordinator:confirmed",
            now=lambda: datetime(2026, 7, 14, 1, 6, 30, tzinfo=UTC),
        )
    assert manifest_path.read_bytes() == committed_manifest_bytes
    assert final_receipt.read_bytes() == committed_receipt_bytes

    evidence = committed["evidence"]
    scenarios = committed["scenarios"]
    assert isinstance(evidence, dict) and isinstance(scenarios, dict)
    scenario_a = scenarios["A"]
    assert isinstance(scenario_a, dict)
    sealed_mutations = (
        lambda: acceptance.control_manifest_bind_retained_evidence(
            manifest_path,
            receipt_path=str(evidence["retained_evidence_path"]),
        ),
        lambda: acceptance.control_manifest_checkpoint_operator_evidence(
            manifest_path,
            exec_receipt_path=str(tmp_path / "unused-exec-receipt.json"),
            checkpoint_path=str(tmp_path / "unused-checkpoint.json"),
        ),
        lambda: acceptance.control_manifest_bind_scenario(
            manifest_path,
            scenario_id="A",
            inventory_path=str(scenario_a["inventory_path"]),
        ),
        lambda: acceptance.receipt_store(
            manifest_path,
            scenario_id="A",
            kind="terraform-plan",
            subject_id="1" * 64,
            receipt_file=tmp_path / "a-plan-receipt.json",
        ),
        lambda: acceptance.approval_verify(
            manifest_path,
            scenario_id="A",
            kind="terraform-plan",
            plan_receipt_hash="1" * 64,
            approval_file=tmp_path / "unused-approval.json",
            signature_verifier=lambda _payload, _signature, _key_id: True,
        ),
    )
    for mutate in sealed_mutations:
        with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_finalized"):
            mutate()
        assert manifest_path.read_bytes() == committed_manifest_bytes
        assert final_receipt.read_bytes() == committed_receipt_bytes

    version_directory = tmp_path / "version-2"
    version_directory.mkdir()
    versioned_manifest_path = version_directory / "control.json"
    _init_control_manifest(
        versioned_manifest_path,
        run_id="4b735e5b-3037-4a3f-938b-69135ef9cd62",
    )
    acceptance.control_manifest_update(
        versioned_manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4b735e5b-3037-4a3f-938b-69135ef9cd62-baseline",
        ecr_candidate_tag="acceptance-4b735e5b-3037-4a3f-938b-69135ef9cd62-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance-v2",
        now=lambda: datetime(2026, 7, 14, 1, 6, 40, tzinfo=UTC),
    )
    assert manifest_path.read_bytes() == committed_manifest_bytes
    assert final_receipt.read_bytes() == committed_receipt_bytes
    assert acceptance.control_manifest_get(versioned_manifest_path, "cleanup_required") == "true"
    assert acceptance.control_manifest_get(manifest_path, "cleanup_states.coordinator") == "confirmed"
    cleanup_ledger = json.loads(ledger_path.read_text())
    assert cleanup_ledger["finalized"] is None
    assert cleanup_ledger["cleanup_records"][-1]["check_id"] == acceptance._TERMINAL_GATE_CHECK_ID
    acceptance.control_manifest_validate(
        manifest_path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        candidate_sha="c" * 40,
        cleanup_only=True,
        require_cleanup_cleared=True,
        now=lambda: datetime(2026, 7, 14, 1, 7, tzinfo=UTC),
    )
    acceptance.gate_ledger_finalize(
        ledger_path,
        candidate_sha="c" * 40,
        now=lambda: datetime(2026, 7, 14, 1, 7, 10, tzinfo=UTC),
    )
    acceptance.control_manifest_validate(
        manifest_path,
        cleanup_only=True,
        require_cleanup_cleared=True,
        now=lambda: datetime(2026, 7, 14, 1, 7, 20, tzinfo=UTC),
    )
    committed_bytes = manifest_path.read_bytes()
    assert "CLEANUP_REQUIRED=0" in acceptance.control_manifest_load_cleanup(
        manifest_path,
        now=lambda: datetime(2026, 7, 14, 6, 0, tzinfo=UTC),
    )
    assert manifest_path.read_bytes() == committed_bytes
    acceptance.control_manifest_validate(
        manifest_path,
        cleanup_only=True,
        require_cleanup_cleared=True,
        now=lambda: datetime(2026, 7, 14, 6, 1, tzinfo=UTC),
    )
    assert final_receipt.stat().st_mode & 0o777 == 0o600
    final_payload = json.loads(final_receipt.read_text())
    assert len(final_payload["manifest_sha256"]) == 64
    assert len(final_payload["ledger_sha256"]) == 64
    final_receipt.unlink()
    with pytest.raises(acceptance.AcceptanceCheckError, match="cleanup_finalize_receipt"):
        acceptance.control_manifest_validate(
            manifest_path,
            cleanup_only=True,
            require_cleanup_cleared=True,
            now=lambda: datetime(2026, 7, 14, 1, 7, 30, tzinfo=UTC),
        )
    resumed = acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="commit",
        clear_cleanup_required=True,
        now=lambda: datetime(2026, 7, 14, 1, 8, tzinfo=UTC),
    )
    assert resumed == committed
    assert json.loads(final_receipt.read_text()) == final_payload
    final_receipt.write_text(json.dumps({**final_payload, "receipts_sha256": "f" * 64}))
    os.chmod(final_receipt, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match="cleanup_finalize_conflict"):
        acceptance.cleanup_evidence_finalize(
            manifest_path,
            ledger_path=ledger_path,
            phase="commit",
            clear_cleanup_required=True,
            now=lambda: datetime(2026, 7, 14, 1, 9, tzinfo=UTC),
        )


def test_cleanup_evidence_finalize_recovers_after_terminal_row_precedes_manifest_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path)
    ledger_path = Path(manifest["gate_ledger_path"])
    _gate_ledger_init(ledger_path)
    _fill_gate_ledger_prefix(ledger_path)
    _fill_cleanup_gate_prefix(ledger_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )
    _checkpoint_evidence_export(manifest_path, ledger_path)
    acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="prepare",
        clear_cleanup_required=False,
        now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
    )
    for surface in acceptance.CLEANUP_SURFACES:
        if surface != "coordinator":
            acceptance.control_manifest_update(
                manifest_path,
                cleanup_checkpoint=f"{surface}:confirmed",
                now=lambda: datetime(2026, 7, 14, 1, 4, tzinfo=UTC),
            )

    original_write = acceptance._write_protected_document

    def interrupt_manifest_commit(path: Path, payload: Mapping[str, object], **kwargs: object) -> None:
        if path == manifest_path and payload.get("cleanup_required") is False:
            raise acceptance.AcceptanceCheckError("simulated_manifest_commit_interrupt")
        original_write(path, payload, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(acceptance, "_write_protected_document", interrupt_manifest_commit)
    with pytest.raises(acceptance.AcceptanceCheckError, match="simulated_manifest_commit_interrupt"):
        acceptance.cleanup_evidence_finalize(
            manifest_path,
            ledger_path=ledger_path,
            phase="commit",
            clear_cleanup_required=True,
            now=lambda: datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
        )
    monkeypatch.setattr(acceptance, "_write_protected_document", original_write)

    interrupted_manifest = json.loads(manifest_path.read_text())
    interrupted_ledger = json.loads(ledger_path.read_text())
    assert interrupted_manifest["cleanup_required"] is True
    assert interrupted_manifest["final_evidence"]["phase"] == "prepared"
    assert interrupted_ledger["cleanup_records"][-1]["check_id"] == acceptance._TERMINAL_GATE_CHECK_ID

    acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="prepare",
        clear_cleanup_required=False,
        now=lambda: datetime(2026, 7, 14, 1, 6, tzinfo=UTC),
    )
    recovered = acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="commit",
        clear_cleanup_required=True,
        now=lambda: datetime(2026, 7, 14, 1, 7, tzinfo=UTC),
    )
    assert recovered["cleanup_required"] is False
    assert recovered["final_evidence"]["phase"] == "committed"  # type: ignore[index]


def test_cleanup_evidence_finalize_preserves_failed_deadline_as_a_valid_cleanup_terminal_state(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path, deadline="2026-07-14T02:00:00Z")
    ledger_path = Path(manifest["gate_ledger_path"])
    _gate_ledger_init(ledger_path)
    acceptance.gate_ledger_record(
        ledger_path,
        check_id="candidate",
        exit_status=0,
        receipt_hash="b" * 64,
        candidate_sha="c" * 40,
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )
    _bind_gate_ledger_candidate(ledger_path)
    _fill_cleanup_gate_prefix(ledger_path)
    _checkpoint_evidence_export(manifest_path, ledger_path)
    acceptance.control_manifest_load_cleanup(
        manifest_path,
        now=lambda: datetime(2026, 7, 14, 2, 1, tzinfo=UTC),
    )
    for surface in acceptance.CLEANUP_SURFACES:
        if surface not in {"coordinator", "teardown_deadline"}:
            acceptance.control_manifest_update(
                manifest_path,
                cleanup_checkpoint=f"{surface}:confirmed",
                now=lambda: datetime(2026, 7, 14, 2, 2, tzinfo=UTC),
            )
    acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="prepare",
        clear_cleanup_required=False,
        now=lambda: datetime(2026, 7, 14, 2, 3, tzinfo=UTC),
    )
    committed = acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="commit",
        clear_cleanup_required=True,
        now=lambda: datetime(2026, 7, 14, 2, 4, tzinfo=UTC),
    )

    assert committed["cleanup_states"]["teardown_deadline"] == "failed"  # type: ignore[index]
    assert json.loads(ledger_path.read_text())["finalized"] is None
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_incomplete"):
        acceptance.gate_ledger_finalize(ledger_path, candidate_sha="c" * 40)
    acceptance.control_manifest_validate(
        manifest_path,
        cleanup_only=True,
        require_cleanup_cleared=True,
        now=lambda: datetime(2026, 7, 14, 2, 5, tzinfo=UTC),
    )


def test_gate_ledger_records_idempotent_closed_checks_and_finalizes_checksum(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    initialized = _gate_ledger_init(path)
    assert initialized["plan_sha256"] == "1" * 64
    assert initialized["program_base_sha"] == "2" * 40
    assert initialized["reconciled_release_sha"] == "3" * 40
    assert initialized["cleanup_records"] == []
    assert initialized["success_record_count_at_cleanup_start"] is None
    assert acceptance.gate_ledger_get(path, "reconciled_release_sha") == "3" * 40
    assert _gate_ledger_init(path) == initialized
    first = acceptance.gate_ledger_record(
        path,
        check_id="candidate",
        exit_status=0,
        receipt_hash="b" * 64,
        candidate_sha="c" * 40,
        started_at="2026-07-14T01:01:00Z",
        ended_at="2026-07-14T01:01:02Z",
        now=lambda: datetime(2026, 7, 14, 1, 1, 2, tzinfo=UTC),
    )
    resumed = acceptance.gate_ledger_record(
        path,
        check_id="candidate",
        exit_status=0,
        receipt_hash="b" * 64,
        candidate_sha="c" * 40,
        started_at="2026-07-14T01:01:00Z",
        ended_at="2026-07-14T01:01:02Z",
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )
    assert first == resumed
    assert len(first["records"]) == 1  # type: ignore[arg-type]
    _fill_gate_ledger_prefix(path)
    bound = json.loads(path.read_text())
    assert bound["candidate_sha"] == "c" * 40
    assert bound["candidate_bound_record_count"] == 1
    _fill_cleanup_gate_prefix(path)
    acceptance.gate_ledger_record_cleanup(
        path,
        check_id=acceptance._TERMINAL_GATE_CHECK_ID,
        exit_status=0,
        receipt_hash="e" * 64,
        candidate_sha="c" * 40,
    )

    finalized = acceptance.gate_ledger_finalize(
        path,
        candidate_sha="c" * 40,
        now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
    )
    final = finalized["finalized"]
    assert isinstance(final, dict)
    assert final["record_count"] == len(acceptance._REQUIRED_GATE_CHECK_IDS)
    assert isinstance(final["records_sha256"], str) and len(final["records_sha256"]) == 64
    rendered = path.read_text()
    assert "expanded command" not in rendered
    assert "raw stdout" not in rendered

    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_finalized"):
        acceptance.gate_ledger_record_cleanup(
            path,
            check_id="cleanup",
            exit_status=0,
            receipt_hash="d" * 64,
            candidate_sha="c" * 40,
        )


def test_gate_ledger_rejects_conflicting_resume_and_invalid_or_secret_shaped_fields(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    _gate_ledger_init(path)
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_conflict"):
        acceptance.gate_ledger_init(
            path,
            branch="feat/aws-ecs-program",
            starting_sha="a" * 40,
            plan_sha256="1" * 64,
            program_base_sha="2" * 40,
            reconciled_release_sha="4" * 40,
        )
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_get"):
        acceptance.gate_ledger_get(path, "records")
    acceptance.gate_ledger_record(
        path,
        check_id="candidate",
        exit_status=0,
        receipt_hash="b" * 64,
        candidate_sha="c" * 40,
    )
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_conflict"):
        acceptance.gate_ledger_record(
            path,
            check_id="candidate",
            exit_status=1,
            receipt_hash="b" * 64,
            candidate_sha="c" * 40,
        )
    _fill_gate_ledger_prefix(path)
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_schema"):
        acceptance.gate_ledger_record(
            path,
            check_id="cleanup",
            exit_status=0,
            receipt_hash="b" * 64,
            candidate_sha="c" * 40,
        )
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_schema"):
        acceptance.gate_ledger_record_cleanup(
            path,
            check_id="candidate",
            exit_status=0,
            receipt_hash="b" * 64,
            candidate_sha="c" * 40,
        )
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_candidate"):
        acceptance.gate_ledger_record_cleanup(
            path,
            check_id="cleanup",
            exit_status=0,
            receipt_hash="b" * 64,
            candidate_sha="d" * 40,
        )
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_schema"):
        acceptance.gate_ledger_record(
            path,
            check_id="curl https://user:password@example.invalid",
            exit_status=0,
            receipt_hash="b" * 64,
            candidate_sha="c" * 40,
        )

    failed_path = tmp_path / "failed-ledger.json"
    _gate_ledger_init(failed_path)
    acceptance.gate_ledger_record(
        failed_path,
        check_id="candidate",
        exit_status=1,
        receipt_hash="b" * 64,
        candidate_sha="c" * 40,
        now=lambda: datetime(2026, 7, 14, 1, 1, 15, tzinfo=UTC),
    )
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_failed"):
        _fill_gate_ledger_prefix(failed_path)


def test_gate_ledger_enforces_candidate_bind_and_cleanup_phase_boundaries(tmp_path: Path) -> None:
    unbound_path = tmp_path / "unbound-ledger.json"
    _gate_ledger_init(unbound_path)
    for check_id in acceptance._TASK1_GATE_CHECK_ORDER:
        acceptance.gate_ledger_record(
            unbound_path,
            check_id=check_id,
            exit_status=0,
            receipt_hash=hashlib.sha256(check_id.encode()).hexdigest(),
            candidate_sha="c" * 40,
        )
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_phase"):
        acceptance.gate_ledger_record(
            unbound_path,
            check_id="static",
            exit_status=0,
            receipt_hash="b" * 64,
            candidate_sha="c" * 40,
        )

    acceptance.gate_ledger_bind_candidate(unbound_path, candidate_sha="c" * 40)
    acceptance.gate_ledger_record(
        unbound_path,
        check_id="static",
        exit_status=0,
        receipt_hash="b" * 64,
        candidate_sha="c" * 40,
    )
    acceptance.gate_ledger_record_cleanup(
        unbound_path,
        check_id="cleanup",
        exit_status=0,
        receipt_hash="d" * 64,
        candidate_sha="c" * 40,
    )
    sealed = json.loads(unbound_path.read_text())
    assert sealed["success_record_count_at_cleanup_start"] == len(sealed["records"])
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_phase"):
        acceptance.gate_ledger_record(
            unbound_path,
            check_id="tests",
            exit_status=0,
            receipt_hash="e" * 64,
            candidate_sha="c" * 40,
        )

    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_conflict"):
        _gate_ledger_init(unbound_path)
