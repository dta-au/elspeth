"""Operator-telemetry and connection-budget AWS ECS acceptance checks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.telemetry.serialization import derive_trace_id
from elspeth.web.config import settings_from_env
from elspeth.web.operator_telemetry import bootstrap_operator_telemetry

from .capture import capture
from .contracts import (
    _MAX_IDENTITY_CHARS,
    _TERMINAL_RUN_STATUSES,
    FORBIDDEN_AWS_OVERRIDE_ENV,
    AcceptanceCheckError,
    AcceptanceInputError,
    OperatorTelemetryAcceptanceError,
    SanitizedResourceIdentity,
    _bounded_identity,
    _canonical_uuid,
    _resolve_aws_region,
    _sha256,
    _utc_timestamp,
)
from .receipt_contracts import (
    _METRIC_NAME,
    _OPERATOR_METRIC_DIMENSION_FIELDS,
    _OPERATOR_METRIC_NAMESPACE,
    _TRACE_NAMES,
)
from .state import AcceptanceState

_MAX_XRAY_SEGMENTS = 32
_MAX_XRAY_DOCUMENT_BYTES = 64 * 1024
_MAX_XRAY_RESPONSE_BYTES = 256 * 1024


class AuditSentinel(Protocol):
    def execute_lifecycle_run(self) -> str: ...

    def verify_run(self, run_id: str) -> bool: ...

    def terminal_status(self, run_id: str) -> str: ...

    def started_at(self, run_id: str) -> datetime: ...


class TelemetrySentinelEmitter(Protocol):
    def emit_web_metric(self, sentinel_value: int, *, acceptance_namespace: str) -> bool: ...

    def health_degraded(self) -> bool: ...

    def close(self) -> None: ...


class TelemetryQueries(Protocol):
    def metric_observed(self, *, metric_name: str, sentinel_value: int, acceptance_namespace: str) -> bool: ...

    def trace_observed(self, *, trace_name: str, run_id: str, started_at: datetime) -> bool: ...

    def trace_terminal_status(self, *, run_id: str) -> str | None: ...


def operator_metric_dimensions(settings: Any) -> tuple[tuple[str, str], ...]:
    """Project the exact non-secret resource dimensions exported in ECS."""

    settings_values = {
        "operator_telemetry_service_name": settings.operator_telemetry_service_name,
        "operator_telemetry_environment": settings.operator_telemetry_environment,
        "operator_telemetry_release": settings.operator_telemetry_release,
        "operator_telemetry_ecs_cluster": settings.operator_telemetry_ecs_cluster,
        "operator_telemetry_ecs_service": settings.operator_telemetry_ecs_service,
        "operator_telemetry_task_definition_family": settings.operator_telemetry_task_definition_family,
        "operator_telemetry_task_definition_revision": settings.operator_telemetry_task_definition_revision,
    }
    dimensions: list[tuple[str, str]] = []
    for dimension_name, field_name in _OPERATOR_METRIC_DIMENSION_FIELDS:
        value = settings_values[field_name]
        try:
            dimensions.append((dimension_name, _bounded_identity(field_name, value)))
        except ValueError:
            raise OperatorTelemetryAcceptanceError("operator telemetry resource identity is invalid") from None
    dimensions.append(("cloud.provider", "aws"))
    return tuple(dimensions)


def xray_trace_id(run_id: str, *, started_at: datetime) -> str:
    """Return an X-Ray trace ID bound to the durable run-start epoch."""

    if type(run_id) is not str or not run_id or len(run_id) > 256:
        raise OperatorTelemetryAcceptanceError("X-Ray run identity is invalid")
    try:
        hexadecimal = f"{derive_trace_id(run_id, started_at=started_at):032x}"
    except (TypeError, ValueError, OverflowError):
        raise OperatorTelemetryAcceptanceError("X-Ray run start is invalid") from None
    return f"1-{hexadecimal[:8]}-{hexadecimal[8:]}"


class AWSOperatorTelemetryQueries:
    """Bounded CloudWatch and X-Ray query adapter with closed projections."""

    def __init__(
        self,
        *,
        cloudwatch: Any,
        xray: Any,
        dimensions: tuple[tuple[str, str], ...],
        start_time: datetime,
        end_time: datetime,
        forbidden_values: tuple[str, ...] = (),
    ) -> None:
        if start_time.tzinfo is None or end_time.tzinfo is None or start_time >= end_time or len(dimensions) > 16 or not dimensions:
            raise OperatorTelemetryAcceptanceError("operator telemetry query window is invalid")
        validated_dimensions: list[tuple[str, str]] = []
        seen_names: set[str] = set()
        for name, value in dimensions:
            if (
                type(name) is not str
                or type(value) is not str
                or not name
                or not value
                or len(name) > 255
                or len(value) > _MAX_IDENTITY_CHARS
                or name in seen_names
            ):
                raise OperatorTelemetryAcceptanceError("operator telemetry dimensions are invalid")
            seen_names.add(name)
            validated_dimensions.append((name, value))
        self._cloudwatch = cloudwatch
        self._xray = xray
        self._dimensions = tuple(validated_dimensions)
        self._start_time = start_time
        self._end_time = end_time
        if any(type(value) is not str or not value or len(value) > 16 * 1024 for value in forbidden_values):
            raise OperatorTelemetryAcceptanceError("operator telemetry forbidden-content inputs are invalid")
        self._forbidden_values = tuple(dict.fromkeys(forbidden_values))
        self._trace_terminal_statuses: dict[str, str] = {}

    def _assert_forbidden_absent(self, value: object) -> None:
        try:
            rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            raise OperatorTelemetryAcceptanceError("operator telemetry projection was invalid") from None
        if any(forbidden in rendered for forbidden in self._forbidden_values):
            raise OperatorTelemetryAcceptanceError("operator telemetry signal contained forbidden content")

    def metric_observed(self, *, metric_name: str, sentinel_value: int, acceptance_namespace: str) -> bool:
        if (
            metric_name != _METRIC_NAME
            or type(sentinel_value) is not int
            or not 0 <= sentinel_value <= 2**48
            or type(acceptance_namespace) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", acceptance_namespace) is None
        ):
            raise OperatorTelemetryAcceptanceError("CloudWatch query input is invalid")
        try:
            raw = self._cloudwatch.get_metric_data(
                MetricDataQueries=[
                    {
                        "Id": "acceptance",
                        "MetricStat": {
                            "Metric": {
                                "Namespace": _OPERATOR_METRIC_NAMESPACE,
                                "MetricName": metric_name,
                                "Dimensions": [
                                    *[{"Name": name, "Value": value} for name, value in self._dimensions],
                                    {"Name": "elspeth.acceptance.namespace", "Value": acceptance_namespace},
                                    {"Name": "elspeth.acceptance.sentinel", "Value": str(sentinel_value)},
                                ],
                            },
                            "Period": 60,
                            "Stat": "Sum",
                        },
                        "ReturnData": True,
                    }
                ],
                StartTime=self._start_time,
                EndTime=self._end_time,
                ScanBy="TimestampAscending",
                MaxDatapoints=100,
            )
        except Exception:
            raise OperatorTelemetryAcceptanceError("CloudWatch query failed") from None
        self._assert_forbidden_absent(raw)
        try:
            if not isinstance(raw, Mapping) or raw.get("NextToken") is not None:
                raise ValueError
            results = raw.get("MetricDataResults")
            if not isinstance(results, list):
                raise ValueError
            if not results:
                return False
            if len(results) != 1:
                raise ValueError
            result = results[0]
            if not isinstance(result, Mapping) or result.get("Id") != "acceptance" or result.get("StatusCode") != "Complete":
                raise ValueError
            values = result.get("Values")
            timestamps = result.get("Timestamps")
            if (
                not isinstance(values, list)
                or not isinstance(timestamps, list)
                or not values
                or len(values) != len(timestamps)
                or len(values) > 100
            ):
                raise ValueError
            matched = False
            for value, timestamp in zip(values, timestamps, strict=True):
                if (
                    type(value) not in {int, float}
                    or not math.isfinite(float(value))
                    or not isinstance(timestamp, datetime)
                    or timestamp.tzinfo is None
                    or not self._start_time <= timestamp <= self._end_time
                ):
                    raise ValueError
                matched = matched or float(value) == float(sentinel_value)
            if not matched:
                return False
        except (TypeError, ValueError):
            raise OperatorTelemetryAcceptanceError("CloudWatch projection was invalid") from None
        return True

    @staticmethod
    def _trace_documents(raw: object, *, expected_trace_id: str) -> list[Mapping[str, object]] | None:
        if not isinstance(raw, Mapping) or raw.get("NextToken") is not None:
            raise ValueError
        unprocessed = raw.get("UnprocessedTraceIds", [])
        traces = raw.get("Traces")
        if not isinstance(unprocessed, list) or unprocessed or not isinstance(traces, list):
            raise ValueError
        if not traces:
            return None
        if len(traces) != 1:
            raise ValueError
        trace = traces[0]
        if not isinstance(trace, Mapping) or trace.get("Id") != expected_trace_id:
            raise ValueError
        segments = trace.get("Segments")
        if not isinstance(segments, list) or not segments or len(segments) > _MAX_XRAY_SEGMENTS:
            raise ValueError
        documents: list[Mapping[str, object]] = []
        total_bytes = 0
        for segment in segments:
            if not isinstance(segment, Mapping):
                raise ValueError
            document = segment.get("Document")
            if type(document) is not str:
                raise ValueError
            size = len(document.encode("utf-8"))
            total_bytes += size
            if size > _MAX_XRAY_DOCUMENT_BYTES or total_bytes > _MAX_XRAY_RESPONSE_BYTES:
                raise ValueError
            parsed = json.loads(document)
            if not isinstance(parsed, Mapping):
                raise ValueError
            documents.append(parsed)
        return documents

    def trace_observed(self, *, trace_name: str, run_id: str, started_at: datetime) -> bool:
        if trace_name not in _TRACE_NAMES:
            raise OperatorTelemetryAcceptanceError("X-Ray trace name is invalid")
        expected_trace_id = xray_trace_id(run_id, started_at=started_at)
        try:
            raw = self._xray.batch_get_traces(TraceIds=[expected_trace_id])
        except Exception:
            raise OperatorTelemetryAcceptanceError("X-Ray query failed") from None
        try:
            documents = self._trace_documents(raw, expected_trace_id=expected_trace_id)
            if documents is None:
                return False
            self._assert_forbidden_absent(documents)
            observed = False
            for document in documents:
                name = document.get("name")
                if name not in _TRACE_NAMES:
                    continue
                annotations = document.get("annotations")
                if not isinstance(annotations, Mapping) or annotations.get("run_id") != run_id:
                    raise ValueError
                if name == "RunFinished":
                    status = annotations.get("status")
                    if type(status) is not str or status not in _TERMINAL_RUN_STATUSES:
                        raise ValueError
                    prior = None
                    if run_id in self._trace_terminal_statuses:
                        prior = self._trace_terminal_statuses[run_id]
                    if prior is not None and prior != status:
                        raise ValueError
                    self._trace_terminal_statuses[run_id] = status
                observed = observed or name == trace_name
            return observed
        except (json.JSONDecodeError, TypeError, UnicodeError, ValueError):
            raise OperatorTelemetryAcceptanceError("X-Ray projection was invalid") from None

    def trace_terminal_status(self, *, run_id: str) -> str | None:
        status = None
        if run_id in self._trace_terminal_statuses:
            status = self._trace_terminal_statuses[run_id]
        return status


@dataclass(frozen=True, slots=True)
class AcceptancePolicy:
    attempts: int = 10
    interval_seconds: float = 3.0

    def __post_init__(self) -> None:
        if type(self.attempts) is not int or not 1 <= self.attempts <= 60:
            raise ValueError("attempts must be an integer from 1 through 60")
        if type(self.interval_seconds) not in {int, float}:
            raise ValueError("interval_seconds must be a finite number")
        interval = float(self.interval_seconds)
        if not math.isfinite(interval) or not 0 <= interval <= 30:
            raise ValueError("interval_seconds must be a finite number from 0 through 30")


_DEFAULT_ACCEPTANCE_POLICY = AcceptancePolicy(attempts=36, interval_seconds=5.0)


@dataclass(frozen=True, slots=True)
class OperatorTelemetryEvidence:
    metric_name: str
    trace_names: tuple[str, str]
    observed_at: float
    resource: SanitizedResourceIdentity
    sentinel_sha256: str
    landscape_status_agrees: bool
    retained_metric_query: Mapping[str, object]
    retained_trace_id: str

    def __post_init__(self) -> None:
        freeze_fields(self, "retained_metric_query")


@dataclass(frozen=True, slots=True)
class OperatorTelemetryOutageEvidence:
    observed_at: float
    sentinel_sha256: str
    landscape_correct: bool
    telemetry_degraded: bool
    cloud_receipt: bool


def _durable_landscape_started_at(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise OperatorTelemetryAcceptanceError("Landscape run start is unavailable")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        raise OperatorTelemetryAcceptanceError("Landscape run start is unavailable") from None


class PublicApiLifecycleAudit:
    """Execute the real public-API pipeline and verify its Landscape record."""

    def __init__(
        self,
        settings: Any,
        env: Mapping[str, str],
        *,
        capture_runner: Callable[..., AcceptanceState] = capture,
        status_reader: Callable[[Any, str], str | None] | None = None,
        started_at_reader: Callable[[Any, str], datetime | None] | None = None,
    ) -> None:
        self._settings = settings
        self._env = dict(env)
        if "ELSPETH_ACCEPTANCE_REGISTER" in self._env:
            del self._env["ELSPETH_ACCEPTANCE_REGISTER"]
        self._capture_runner = capture_runner
        self._status_reader = status_reader or _read_landscape_terminal_status
        self._started_at_reader = started_at_reader or _read_landscape_started_at
        self._state: AcceptanceState | None = None
        self._verified_status: str | None = None
        self._started_at: datetime | None = None

    def execute_lifecycle_run(self) -> str:
        try:
            with tempfile.TemporaryDirectory(prefix="elspeth-operator-telemetry-") as directory:
                self._state = self._capture_runner(
                    self._env,
                    state_file=Path(directory) / "state.json",
                )
        except Exception:
            raise OperatorTelemetryAcceptanceError("public API lifecycle run failed") from None
        return self._state.landscape_run_id

    def verify_run(self, run_id: str) -> bool:
        if self._state is None or run_id != self._state.landscape_run_id:
            return False
        self._verified_status = self._status_reader(self._settings, run_id)
        return self._verified_status == "completed"

    def terminal_status(self, run_id: str) -> str:
        if self._state is None or run_id != self._state.landscape_run_id:
            return ""
        if self._verified_status is None:
            self._verified_status = self._status_reader(self._settings, run_id)
        verified_status = self._verified_status
        if verified_status is None:
            verified_status = ""
        return verified_status

    def started_at(self, run_id: str) -> datetime:
        if self._state is None or run_id != self._state.landscape_run_id:
            raise OperatorTelemetryAcceptanceError("Landscape run start is unavailable")
        if self._started_at is None:
            self._started_at = _durable_landscape_started_at(self._started_at_reader(self._settings, run_id))
        return self._started_at


class ExistingLandscapeLifecycleAudit:
    """Verify a browser-authenticated lifecycle run without handling its bearer token."""

    def __init__(
        self,
        settings: Any,
        run_id: str,
        *,
        status_reader: Callable[[Any, str], str | None] | None = None,
        started_at_reader: Callable[[Any, str], datetime | None] | None = None,
    ) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", run_id) is None:
            raise OperatorTelemetryAcceptanceError("existing Landscape run identity is invalid")
        self._settings = settings
        self._run_id = run_id
        self._status_reader = status_reader or _read_landscape_terminal_status
        self._started_at_reader = started_at_reader or _read_landscape_started_at
        self._verified_status: str | None = None
        self._started_at: datetime | None = None

    def execute_lifecycle_run(self) -> str:
        return self._run_id

    def verify_run(self, run_id: str) -> bool:
        if run_id != self._run_id:
            return False
        self._verified_status = self._status_reader(self._settings, run_id)
        return self._verified_status == "completed"

    def terminal_status(self, run_id: str) -> str:
        if run_id != self._run_id:
            return ""
        if self._verified_status is None:
            self._verified_status = self._status_reader(self._settings, run_id)
        verified_status = self._verified_status
        if verified_status is None:
            verified_status = ""
        return verified_status

    def started_at(self, run_id: str) -> datetime:
        if run_id != self._run_id:
            raise OperatorTelemetryAcceptanceError("Landscape run start is unavailable")
        if self._started_at is None:
            self._started_at = _durable_landscape_started_at(self._started_at_reader(self._settings, run_id))
        return self._started_at


def _read_landscape_terminal_status(settings: Any, run_id: str) -> str | None:
    try:
        with LandscapeDB.from_url(
            settings.get_landscape_url(),
            passphrase=settings.landscape_passphrase,
            create_tables=False,
            read_only=True,
        ) as database:
            run = RecorderFactory.read_only(database).run_lifecycle.get_run(run_id)
    except Exception:
        raise OperatorTelemetryAcceptanceError("Landscape lifecycle query failed") from None
    if run is None or run.completed_at is None:
        return None
    return run.status.value


def _read_landscape_started_at(settings: Any, run_id: str) -> datetime | None:
    try:
        with LandscapeDB.from_url(
            settings.get_landscape_url(),
            passphrase=settings.landscape_passphrase,
            create_tables=False,
            read_only=True,
        ) as database:
            run = RecorderFactory.read_only(database).run_lifecycle.get_run(run_id)
    except Exception:
        raise OperatorTelemetryAcceptanceError("Landscape lifecycle query failed") from None
    if run is None:
        return None
    started_at = run.started_at
    return started_at.replace(tzinfo=UTC) if started_at.tzinfo is None else _durable_landscape_started_at(started_at)


class AWSOperatorMetricEmitter:
    """Emit and synchronously flush one metric through the production provider."""

    def __init__(
        self,
        settings: Any,
        *,
        runtime_factory: Callable[[Any], Any] = bootstrap_operator_telemetry,
    ) -> None:
        try:
            self._runtime = runtime_factory(settings)
        except Exception:
            raise OperatorTelemetryAcceptanceError("operator metric runtime initialization failed") from None

    def emit_web_metric(self, sentinel_value: int, *, acceptance_namespace: str) -> bool:
        if type(acceptance_namespace) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", acceptance_namespace) is None:
            raise OperatorTelemetryAcceptanceError("operator metric acceptance namespace is invalid")
        try:
            get_meter = self._runtime.provider.get_meter
            meter = get_meter("elspeth.web.aws_ecs_acceptance")
            counter = meter.create_counter(
                _METRIC_NAME,
                description="Unique non-content AWS ECS acceptance sentinel.",
                unit="1",
            )
            counter.add(
                sentinel_value,
                attributes={
                    "elspeth.acceptance.namespace": acceptance_namespace,
                    "elspeth.acceptance.sentinel": str(sentinel_value),
                },
            )
            return self._runtime.provider.force_flush(timeout_millis=5_000) is True
        except Exception:
            return False

    def health_degraded(self) -> bool:
        try:
            return int(self._runtime.health.consecutive_failures) > 0
        except Exception:
            raise OperatorTelemetryAcceptanceError("operator telemetry health projection failed") from None

    def close(self) -> None:
        try:
            asyncio.run(self._runtime.shutdown())
        except Exception:
            raise OperatorTelemetryAcceptanceError("operator metric runtime shutdown failed") from None


def _build_aws_observability_client(service: str, region: str) -> Any:
    if service not in {"cloudwatch", "xray"}:
        raise OperatorTelemetryAcceptanceError("AWS observability service is invalid")
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise OperatorTelemetryAcceptanceError("AWS observability dependencies are unavailable") from None
    try:
        return boto3.client(
            service,
            region_name=region,
            config=Config(
                connect_timeout=10,
                read_timeout=30,
                retries={"mode": "standard", "total_max_attempts": 3},
            ),
        )
    except Exception:
        raise OperatorTelemetryAcceptanceError("AWS observability client initialization failed") from None


def _operator_forbidden_values(env: Mapping[str, str]) -> tuple[str, ...]:
    forbidden_names = {
        "ELSPETH_ACCEPTANCE_BASE_URL",
        "ELSPETH_ACCEPTANCE_USERNAME",
        "ELSPETH_ACCEPTANCE_PASSWORD",
        "ELSPETH_ACCEPTANCE_BEARER_TOKEN",
        "ELSPETH_BEDROCK_LIVE_TEST_MODEL",
    }
    return tuple(
        value
        for name, value in env.items()
        if value and (name in forbidden_names or name.startswith("ELSPETH_LIVE_BEDROCK_") or name in FORBIDDEN_AWS_OVERRIDE_ENV)
    )


def _operator_forbidden_content_absent(receipt: Mapping[str, object], env: Mapping[str, str]) -> bool:
    forbidden_values = _operator_forbidden_values(env)
    rendered = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    return not any(value in rendered for value in forbidden_values)


def _operator_resource_identity(settings: Any) -> SanitizedResourceIdentity:
    try:
        return SanitizedResourceIdentity(
            service_name=settings.operator_telemetry_service_name,
            service_version=settings.operator_telemetry_release,
            deployment_environment=settings.operator_telemetry_environment,
            cloud_provider="aws",
        )
    except (AttributeError, TypeError, ValueError):
        raise OperatorTelemetryAcceptanceError("operator telemetry resource identity is invalid") from None


def _operator_receipt(
    *,
    phase: Literal["positive", "outage"],
    evidence: OperatorTelemetryEvidence | OperatorTelemetryOutageEvidence,
    resource: SanitizedResourceIdentity,
    collector_degraded: bool,
) -> dict[str, object]:
    positive = isinstance(evidence, OperatorTelemetryEvidence)
    trace_terminal_agrees = evidence.landscape_status_agrees if isinstance(evidence, OperatorTelemetryEvidence) else None
    return {
        "phase": phase,
        "metric_name": _METRIC_NAME,
        "trace_names": list(_TRACE_NAMES),
        "observed_at": evidence.observed_at,
        "resource": {
            "service_name": resource.service_name,
            "service_version": resource.service_version,
            "deployment_environment": resource.deployment_environment,
            "cloud_provider": resource.cloud_provider,
        },
        "sentinel_sha256": evidence.sentinel_sha256,
        "landscape_terminal": True,
        "trace_terminal_agrees": trace_terminal_agrees,
        "collector_degraded": collector_degraded,
        "cloud_receipt": positive,
        "retained_metric_query": deep_thaw(evidence.retained_metric_query) if isinstance(evidence, OperatorTelemetryEvidence) else None,
        "retained_trace_id": evidence.retained_trace_id if isinstance(evidence, OperatorTelemetryEvidence) else None,
    }


def _sentinel_facts(factory: Callable[[], str]) -> tuple[str, int]:
    raw = factory()
    if type(raw) is not str or not raw or len(raw) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise OperatorTelemetryAcceptanceError("acceptance sentinel generation failed validation")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    # Exactly representable in the IEEE-754 integer range used by metric
    # backends while remaining unique enough for a bounded acceptance window.
    return digest, int(digest[:12], 16)


def verify_operator_telemetry(
    *,
    audit: AuditSentinel,
    emitter: TelemetrySentinelEmitter,
    queries: TelemetryQueries,
    resource: SanitizedResourceIdentity,
    acceptance_namespace: str,
    metric_dimensions: tuple[tuple[str, str], ...],
    policy: AcceptancePolicy = _DEFAULT_ACCEPTANCE_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    sentinel_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    now: Callable[[], float] = time.time,
) -> OperatorTelemetryEvidence:
    """Prove one audit-first metric and lifecycle trace with bounded retries."""

    sentinel_sha256, sentinel_value = _sentinel_facts(sentinel_factory)
    run_id = audit.execute_lifecycle_run()
    if not run_id:
        raise OperatorTelemetryAcceptanceError("Landscape lifecycle run returned no identity")
    if not audit.verify_run(run_id):
        raise OperatorTelemetryAcceptanceError("Landscape lifecycle run was not durable before telemetry")
    landscape_status = audit.terminal_status(run_id)
    if landscape_status != "completed":
        raise OperatorTelemetryAcceptanceError("Landscape terminal status was not completed")
    started_at = audit.started_at(run_id)

    metric_delivery = emitter.emit_web_metric(sentinel_value, acceptance_namespace=acceptance_namespace)
    if not metric_delivery:
        raise OperatorTelemetryAcceptanceError("operator telemetry delivery was unavailable")

    metric_seen = False
    trace_seen = dict.fromkeys(_TRACE_NAMES, False)
    for attempt in range(policy.attempts):
        metric_seen = metric_seen or queries.metric_observed(
            metric_name=_METRIC_NAME,
            sentinel_value=sentinel_value,
            acceptance_namespace=acceptance_namespace,
        )
        for trace_name in _TRACE_NAMES:
            trace_seen[trace_name] = trace_seen[trace_name] or queries.trace_observed(
                trace_name=trace_name,
                run_id=run_id,
                started_at=started_at,
            )
        if metric_seen and all(trace_seen.values()):
            break
        if attempt + 1 < policy.attempts:
            sleep(float(policy.interval_seconds))
    if not metric_seen or not all(trace_seen.values()):
        raise OperatorTelemetryAcceptanceError("bounded CloudWatch/X-Ray observation did not find both signals")
    if queries.trace_terminal_status(run_id=run_id) != landscape_status:
        raise OperatorTelemetryAcceptanceError("Landscape and trace terminal status did not agree")

    return OperatorTelemetryEvidence(
        metric_name=_METRIC_NAME,
        trace_names=_TRACE_NAMES,
        observed_at=now(),
        resource=resource,
        sentinel_sha256=sentinel_sha256,
        landscape_status_agrees=True,
        retained_metric_query={
            "namespace": _OPERATOR_METRIC_NAMESPACE,
            "metric_name": _METRIC_NAME,
            "dimensions": [
                *[{"name": name, "value": value} for name, value in metric_dimensions],
                {"name": "elspeth.acceptance.namespace", "value": acceptance_namespace},
                {"name": "elspeth.acceptance.sentinel", "value": str(sentinel_value)},
            ],
        },
        retained_trace_id=xray_trace_id(run_id, started_at=started_at),
    )


def verify_operator_telemetry_outage(
    *,
    audit: AuditSentinel,
    emitter: TelemetrySentinelEmitter,
    queries: TelemetryQueries,
    acceptance_namespace: str,
    policy: AcceptancePolicy = _DEFAULT_ACCEPTANCE_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    sentinel_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    now: Callable[[], float] = time.time,
) -> OperatorTelemetryOutageEvidence:
    """Prove a collector outage cannot undo or impersonate audit evidence."""

    sentinel_sha256, sentinel_value = _sentinel_facts(sentinel_factory)
    run_id = audit.execute_lifecycle_run()
    if not run_id:
        raise OperatorTelemetryAcceptanceError("Landscape lifecycle run returned no identity")
    started_at = audit.started_at(run_id)

    metric_delivery = emitter.emit_web_metric(sentinel_value, acceptance_namespace=acceptance_namespace)
    landscape_correct = audit.verify_run(run_id)
    telemetry_degraded = emitter.health_degraded()
    cloud_receipt = metric_delivery
    for attempt in range(policy.attempts):
        cloud_receipt = cloud_receipt or queries.metric_observed(
            metric_name=_METRIC_NAME,
            sentinel_value=sentinel_value,
            acceptance_namespace=acceptance_namespace,
        )
        for trace_name in _TRACE_NAMES:
            cloud_receipt = cloud_receipt or queries.trace_observed(
                trace_name=trace_name,
                run_id=run_id,
                started_at=started_at,
            )
        if cloud_receipt:
            break
        if attempt + 1 < policy.attempts:
            sleep(float(policy.interval_seconds))
    if not landscape_correct:
        raise OperatorTelemetryAcceptanceError("Landscape lifecycle run was not durable during telemetry outage")
    if not telemetry_degraded:
        raise OperatorTelemetryAcceptanceError("telemetry outage did not produce degraded health")
    if cloud_receipt:
        raise OperatorTelemetryAcceptanceError("telemetry outage produced a false delivery receipt")

    return OperatorTelemetryOutageEvidence(
        observed_at=now(),
        sentinel_sha256=sentinel_sha256,
        landscape_correct=True,
        telemetry_degraded=True,
        cloud_receipt=False,
    )


def verify_operator_telemetry_live(
    env: Mapping[str, str],
    *,
    phase: Literal["positive", "outage"],
    landscape_run_id: str | None = None,
    settings_loader: Callable[[], Any] = settings_from_env,
    audit_factory: Callable[[Any, Mapping[str, str]], AuditSentinel] = PublicApiLifecycleAudit,
    existing_audit_factory: Callable[[Any, str], AuditSentinel] = ExistingLandscapeLifecycleAudit,
    emitter_factory: Callable[[Any], TelemetrySentinelEmitter] = AWSOperatorMetricEmitter,
    aws_client_factory: Callable[[str, str], Any] = _build_aws_observability_client,
    policy: AcceptancePolicy = _DEFAULT_ACCEPTANCE_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    sentinel_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    now_datetime: Callable[[], datetime] = lambda: datetime.now(UTC),
    now_epoch: Callable[[], float] = time.time,
) -> dict[str, object]:
    """Run one honest positive or externally induced collector-outage phase."""

    if phase not in {"positive", "outage"}:
        raise OperatorTelemetryAcceptanceError("operator telemetry phase is invalid")
    if landscape_run_id is not None and (
        phase != "positive" or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", landscape_run_id) is None
    ):
        raise OperatorTelemetryAcceptanceError("existing Landscape run identity is invalid")
    if any(name in env for name in FORBIDDEN_AWS_OVERRIDE_ENV):
        raise OperatorTelemetryAcceptanceError("operator telemetry AWS override is forbidden")
    try:
        settings = settings_loader()
    except Exception:
        raise OperatorTelemetryAcceptanceError("operator telemetry settings load failed") from None
    if (
        settings.deployment_target != "aws-ecs"
        or settings.operator_telemetry != "aws-otlp"
        or settings.operator_pipeline_telemetry_granularity != "lifecycle"
    ):
        raise OperatorTelemetryAcceptanceError("operator telemetry AWS ECS posture is invalid")
    dimensions = operator_metric_dimensions(settings)
    resource = _operator_resource_identity(settings)
    region = _resolve_aws_region(env, check="operator_telemetry_input")
    try:
        acceptance_run_id = _canonical_uuid(env.get("ELSPETH_ACCEPTANCE_RUN_ID", ""), label="acceptance run ID")
    except AcceptanceInputError:
        raise OperatorTelemetryAcceptanceError("operator telemetry acceptance binding is invalid") from None
    scenario_id = env.get("ELSPETH_ACCEPTANCE_SCENARIO_ID", "")
    if scenario_id not in {"A", "B"}:
        raise OperatorTelemetryAcceptanceError("operator telemetry acceptance binding is invalid")
    acceptance_namespace = f"{acceptance_run_id}-{scenario_id.lower()}"
    window_start = now_datetime()

    cloudwatch: Any | None = None
    xray: Any | None = None
    emitter: TelemetrySentinelEmitter | None = None
    close_failed = False
    try:
        try:
            cloudwatch = aws_client_factory("cloudwatch", region)
            xray = aws_client_factory("xray", region)
            audit = audit_factory(settings, env) if landscape_run_id is None else existing_audit_factory(settings, landscape_run_id)
            emitter = emitter_factory(settings)
        except OperatorTelemetryAcceptanceError:
            raise
        except Exception:
            raise OperatorTelemetryAcceptanceError("operator telemetry dependency initialization failed") from None
        queries = AWSOperatorTelemetryQueries(
            cloudwatch=cloudwatch,
            xray=xray,
            dimensions=dimensions,
            start_time=window_start - timedelta(minutes=1),
            end_time=window_start + timedelta(minutes=10),
            forbidden_values=_operator_forbidden_values(env),
        )
        if phase == "positive":
            evidence: OperatorTelemetryEvidence | OperatorTelemetryOutageEvidence = verify_operator_telemetry(
                audit=audit,
                emitter=emitter,
                queries=queries,
                resource=resource,
                acceptance_namespace=acceptance_namespace,
                metric_dimensions=dimensions,
                policy=policy,
                sleep=sleep,
                sentinel_factory=sentinel_factory,
                now=now_epoch,
            )
            collector_degraded = emitter.health_degraded()
            if collector_degraded:
                raise OperatorTelemetryAcceptanceError("operator telemetry health remained degraded after positive proof")
        else:
            evidence = verify_operator_telemetry_outage(
                audit=audit,
                emitter=emitter,
                queries=queries,
                acceptance_namespace=acceptance_namespace,
                policy=policy,
                sleep=sleep,
                sentinel_factory=sentinel_factory,
                now=now_epoch,
            )
            collector_degraded = evidence.telemetry_degraded
        receipt = _operator_receipt(
            phase=phase,
            evidence=evidence,
            resource=resource,
            collector_degraded=collector_degraded,
        )
        if not _operator_forbidden_content_absent(receipt, env):
            raise OperatorTelemetryAcceptanceError("operator telemetry receipt contained forbidden content")
        receipt["forbidden_content_absent"] = True
        return receipt
    finally:
        for resource_to_close in (xray, cloudwatch, emitter):
            if resource_to_close is None:
                continue
            try:
                resource_to_close.close()
            except Exception:
                close_failed = True
        if close_failed and sys.exc_info()[0] is None:
            raise OperatorTelemetryAcceptanceError("operator telemetry resource close failed")


def _read_postgres_max_connections(settings: Any) -> int:
    try:
        with LandscapeDB.from_url(
            settings.get_landscape_url(),
            passphrase=settings.landscape_passphrase,
            create_tables=False,
            read_only=True,
        ) as database:
            if database.engine.dialect.name != "postgresql":
                raise AcceptanceCheckError("connection_budget_database")
            with database.engine.connect() as connection:
                value = connection.exec_driver_sql("SHOW max_connections").scalar_one()
    except AcceptanceCheckError:
        raise
    except Exception:
        raise AcceptanceCheckError("connection_budget_database") from None
    try:
        maximum = int(value)
    except (TypeError, ValueError):
        raise AcceptanceCheckError("connection_budget_database") from None
    if maximum <= 0:
        raise AcceptanceCheckError("connection_budget_database")
    return maximum


def verify_connection_budget_live(
    env: Mapping[str, str],
    *,
    cluster_id: str,
    start_time: str,
    approved_budget: int,
    safety_margin: int,
    settings_loader: Callable[[], Any] = settings_from_env,
    max_connections_reader: Callable[[Any], int] = _read_postgres_max_connections,
    aws_client_factory: Callable[[str, str], Any] = _build_aws_observability_client,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 10,
) -> dict[str, object]:
    """Verify the observed Aurora connection high-water against an approved budget."""

    if (
        type(cluster_id) is not str
        or len(cluster_id) > 63
        or re.fullmatch(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", cluster_id) is None
        or "--" in cluster_id
        or type(approved_budget) is not int
        or type(safety_margin) is not int
        or approved_budget <= 0
        or safety_margin < 0
        or type(attempts) is not int
        or not 1 <= attempts <= 20
    ):
        raise AcceptanceCheckError("connection_budget_input")
    try:
        acceptance_run_id = _canonical_uuid(env.get("ELSPETH_ACCEPTANCE_RUN_ID", ""), label="acceptance run ID")
    except AcceptanceInputError:
        raise AcceptanceCheckError("connection_budget_input") from None
    try:
        window_start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise AcceptanceCheckError("connection_budget_input") from None
    observed_now = now()
    if (
        window_start.tzinfo is None
        or observed_now.tzinfo is None
        or window_start >= observed_now
        or observed_now - window_start > timedelta(hours=1)
    ):
        raise AcceptanceCheckError("connection_budget_input")
    window_start = window_start.astimezone(UTC)
    if window_start.second != 0 or window_start.microsecond != 0:
        raise AcceptanceCheckError("connection_budget_input")
    window_end = window_start + timedelta(minutes=10)
    observed_now = observed_now.astimezone(UTC)
    if observed_now < window_end:
        raise AcceptanceCheckError("connection_budget_input")
    expected_timestamps = tuple(window_start + timedelta(minutes=offset) for offset in range(10))
    region = _resolve_aws_region(env, check="connection_budget_input")
    try:
        settings = settings_loader()
        maximum = max_connections_reader(settings)
    except AcceptanceCheckError:
        raise
    except Exception:
        raise AcceptanceCheckError("connection_budget_database") from None
    if maximum <= 0 or approved_budget > maximum - safety_margin:
        raise AcceptanceCheckError("connection_budget_limit")

    cloudwatch: Any | None = None
    points: list[dict[str, object]] = []
    try:
        cloudwatch = aws_client_factory("cloudwatch", region)
        for attempt in range(attempts):
            try:
                response = cloudwatch.get_metric_data(
                    MetricDataQueries=[
                        {
                            "Id": "connections",
                            "MetricStat": {
                                "Metric": {
                                    "Namespace": "AWS/RDS",
                                    "MetricName": "DatabaseConnections",
                                    "Dimensions": [{"Name": "DBClusterIdentifier", "Value": cluster_id}],
                                },
                                "Period": 60,
                                "Stat": "Maximum",
                            },
                            "ReturnData": True,
                        }
                    ],
                    StartTime=window_start,
                    EndTime=window_end,
                    ScanBy="TimestampAscending",
                    MaxDatapoints=1000,
                )
            except Exception:
                raise AcceptanceCheckError("connection_budget_cloudwatch") from None
            if not isinstance(response, Mapping) or response.get("NextToken") is not None:
                raise AcceptanceCheckError("connection_budget_cloudwatch")
            results = response.get("MetricDataResults")
            if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], Mapping):
                raise AcceptanceCheckError("connection_budget_cloudwatch")
            result = results[0]
            timestamps = result.get("Timestamps")
            values = result.get("Values")
            if result.get("Id") != "connections" or result.get("StatusCode") not in {"Complete", "PartialData"}:
                raise AcceptanceCheckError("connection_budget_cloudwatch")
            if not isinstance(timestamps, list) or not isinstance(values, list) or len(timestamps) != len(values):
                raise AcceptanceCheckError("connection_budget_cloudwatch")
            candidate_points: list[dict[str, object]] = []
            candidate_timestamps: list[datetime] = []
            for timestamp, value in zip(timestamps, values, strict=True):
                if (
                    not isinstance(timestamp, datetime)
                    or timestamp.tzinfo is None
                    or isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) < 0
                ):
                    raise AcceptanceCheckError("connection_budget_cloudwatch")
                normalized_timestamp = timestamp.astimezone(UTC)
                candidate_timestamps.append(normalized_timestamp)
                candidate_points.append({"timestamp": _utc_timestamp(normalized_timestamp), "count": float(value)})
            if result.get("StatusCode") == "PartialData":
                if attempt + 1 < attempts:
                    sleep(30.0)
                    continue
                raise AcceptanceCheckError("connection_budget_cloudwatch")
            complete_grid = (
                len(candidate_timestamps) == len(expected_timestamps)
                and len(set(candidate_timestamps)) == len(candidate_timestamps)
                and tuple(sorted(candidate_timestamps)) == expected_timestamps
            )
            if complete_grid:
                points = sorted(candidate_points, key=lambda point: cast(str, point["timestamp"]))
                break
            if attempt + 1 < attempts:
                sleep(30.0)
        if not points:
            raise AcceptanceCheckError("connection_budget_cloudwatch")
    finally:
        failure_in_flight = sys.exc_info()[0] is not None
        if cloudwatch is not None:
            try:
                cloudwatch.close()
            except Exception:
                if not failure_in_flight:
                    raise AcceptanceCheckError("connection_budget_cloudwatch") from None

    high_water = max(cast(float, point["count"]) for point in points)
    if high_water > approved_budget or maximum - high_water < safety_margin:
        raise AcceptanceCheckError("connection_budget_exceeded")
    return {
        "schema": "elspeth.rds-connection-budget.v3",
        "acceptance_run_id_sha256": _sha256(acceptance_run_id.encode("utf-8")),
        "cluster_id_sha256": _sha256(cluster_id.encode("utf-8")),
        "window_start": _utc_timestamp(window_start),
        "window_end": _utc_timestamp(window_end),
        "period_seconds": 60,
        "expected_points": len(expected_timestamps),
        "points": points,
        "high_water": high_water,
        "max_connections": maximum,
        "approved_budget": approved_budget,
        "safety_margin": safety_margin,
        "ok": True,
    }
