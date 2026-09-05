"""Projections of platform JSON onto the closed detail sets, and the receipt store.

This is the **re-targeted** module (plan §8.2): every function here meets the
real ``az`` / KQL / Resource Graph output shape for the first time at the live
run, so each projection admits only the members it needs, bounds them, and
constructs an owned value — nothing from a platform response is carried
through by reference. The receipt store is deliberately thin: a mode-0700
directory of canonical receipt documents plus one index whose rows are the
shared :class:`ReceiptIndexRow` shape, so the shared ``testcontainer_run_gate``
reads it exactly as it reads the ECS control manifest.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast

from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web._acceptance_common.errors import AcceptanceCheckError, AcceptanceInputError
from elspeth.web._acceptance_common.receipt_validation import (
    _SHA256_PATTERN,
    _receipt_number,
    _sha256,
    _utc_timestamp,
    validate_connection_budget_receipt,
)
from elspeth.web._acceptance_common.replica_probes import (
    PROBES,
    CrossReplicaProgressObservation,
    LeaseTakeoverObservation,
    MembershipRow,
    ProbeResult,
    ReplicaResponse,
    replica_response_from_envelope,
)
from elspeth.web._acceptance_common.secure_documents import MAX_CONTROL_DOCUMENT_BYTES, _read_protected_document
from elspeth.web._acceptance_common.testcontainer_run import (
    TESTCONTAINER_RUN_GATE_REASONS,
    TESTCONTAINER_RUN_RECEIPT_KIND,
    ReceiptIndexRow,
    testcontainer_run_gate,
)

from .controller import LabelWeight
from .receipt_contracts import (
    BLOB_JOB_NAME,
    CONNECTION_BUDGET_SCHEMA,
    DEPLOYMENT_TARGET,
    DOCTOR_JOB_NAMES,
    PROBE_KINDS,
    PROVIDER,
    STORAGE_DIRECTORIES,
    STORAGE_JOB_NAME,
    STORED_RECEIPT_KINDS,
    BlobManagedIdentityDetails,
    BudgetPoint,
    BudgetReceipt,
    ConnectionBudgetDetails,
    JobDetails,
    ResourceGraphCleanupDetails,
    RevisionRolloutDetails,
    StorageJobDetails,
    StoredReceipt,
    validate_stored_receipt,
)

MAX_PLATFORM_DOCUMENT_BYTES: Final = 2 * 1024 * 1024
"""Upper bound on one captured ``az`` document (the driver's ``ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES``)."""
_MAX_ROWS: Final = 100_000
_IDENTIFIER: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


def _identifier(value: object) -> str:
    if type(value) is not str or not 0 < len(value) <= 128 or set(value) - _IDENTIFIER or value[0] == "-":
        raise AcceptanceCheckError("platform_document_schema")
    return value


# --------------------------------------------------------------------------- az JSON projections


@dataclass(frozen=True, slots=True)
class JobExecution:
    name: str
    status: str


@trust_boundary(
    tier=3,
    source="the JSON document `az containerapp job execution show` printed for one Job execution",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('platform_document_schema') before use unless the payload is a dict whose name is "
        "a bounded lowercase identifier and whose properties.status is a bounded string; returns only the owned JobExecution"
    ),
    test_ref="tests/unit/web/azure_container_apps_acceptance/test_evidence_projection.py::test_job_execution_projection_rejects_malformed_documents",
    test_fingerprint="32fddf8127961573db70c8a21307c3ae1e09190989f3f29da8033d905765734d",
)
def project_job_execution(payload: object) -> JobExecution:
    if type(payload) is not dict or type(payload.get("properties")) is not dict:
        raise AcceptanceCheckError("platform_document_schema")
    status = payload["properties"].get("status")
    if type(status) is not str or not 0 < len(status) <= 32 or not status.isalpha():
        raise AcceptanceCheckError("platform_document_schema")
    return JobExecution(name=_identifier(payload.get("name")), status=status)


@trust_boundary(
    tier=3,
    source="the JSON list `az containerapp replica list` printed for one revision",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('platform_document_schema') before use unless the payload is a bounded list of "
        "dicts each naming one bounded lowercase replica identifier; returns only an owned tuple of those names"
    ),
    test_ref="tests/unit/web/azure_container_apps_acceptance/test_evidence_projection.py::test_replica_list_projection_rejects_malformed_documents",
    test_fingerprint="02a2cc115a0df8e61a5f7813073647724a791af01261265af874aff60111cd5a",
)
def project_replica_names(payload: object) -> tuple[str, ...]:
    if type(payload) is not list or not 0 < len(payload) <= 100:
        raise AcceptanceCheckError("platform_document_schema")
    names: list[str] = []
    for entry in payload:
        if type(entry) is not dict:
            raise AcceptanceCheckError("platform_document_schema")
        names.append(_identifier(entry.get("name")))
    if len(set(names)) != len(names):
        raise AcceptanceCheckError("platform_document_schema")
    return tuple(names)


@dataclass(frozen=True, slots=True)
class ActiveRevision:
    name: str
    traffic_weight: int
    running_state: str


@trust_boundary(
    tier=3,
    source='the JSON list `az containerapp revision list --query "[?properties.active].{name,traffic,state}"` printed',
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('platform_document_schema') before use unless the payload is a bounded list of dicts "
        "each carrying a lowercase revision name, an integer traffic weight in 0..100 and a bounded alphabetic state; "
        "returns only owned ActiveRevision values"
    ),
    test_ref="tests/unit/web/azure_container_apps_acceptance/test_evidence_projection.py::test_revision_list_projection_rejects_malformed_documents",
    test_fingerprint="fd5ac54ad1acd671885874d3a63fe592d05a5ab189cf03f18511f96b775fc9a4",
)
def project_active_revisions(payload: object) -> tuple[ActiveRevision, ...]:
    if type(payload) is not list or len(payload) > 100:
        raise AcceptanceCheckError("platform_document_schema")
    revisions: list[ActiveRevision] = []
    for entry in payload:
        if type(entry) is not dict:
            raise AcceptanceCheckError("platform_document_schema")
        weight = entry.get("traffic")
        state = entry.get("state")
        if type(weight) is not int or not 0 <= weight <= 100 or type(state) is not str or not 0 < len(state) <= 32 or not state.isalpha():
            raise AcceptanceCheckError("platform_document_schema")
        revisions.append(ActiveRevision(name=_identifier(entry.get("name")), traffic_weight=weight, running_state=state))
    return tuple(revisions)


_CHECK_NAME: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def _succeeded(execution: JobExecution, *, job_name: str, job_names: frozenset[str]) -> None:
    if job_name not in job_names:
        raise AcceptanceInputError("job_name is not one of the acceptance Jobs")
    if execution.status != "Succeeded":
        raise AcceptanceCheckError("job_execution_failed")


@trust_boundary(
    tier=3,
    source="the `elspeth doctor deployment --json` report (a bare ordered check list) retrieved from Log Analytics for one Job execution",
    source_param="report",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('platform_document_schema' or 'doctor_check_failed') before use unless the report "
        "bytes decode to a bounded list of {name, ok, detail} checks with bounded snake_case names and boolean ok, every "
        "one true; returns only the owned JobDetails naming the checks, never a detail string"
    ),
    test_ref="tests/unit/web/azure_container_apps_acceptance/test_evidence_projection.py::test_doctor_report_projection_rejects_malformed_or_failing_reports",
    test_fingerprint="cb67127bc194538ab95a36efc1ce980c158cc366eddbaf14d07906fb997673c0",
)
def doctor_job_details(report: bytes, *, execution: JobExecution, job_name: str) -> JobDetails:
    _succeeded(execution, job_name=job_name, job_names=DOCTOR_JOB_NAMES)
    if type(report) is not bytes or not 0 < len(report) <= MAX_PLATFORM_DOCUMENT_BYTES:
        raise AcceptanceCheckError("platform_document_schema")
    try:
        checks = json.loads(report)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise AcceptanceCheckError("platform_document_schema") from None
    if type(checks) is not list or not 0 < len(checks) <= 64:
        raise AcceptanceCheckError("platform_document_schema")
    names: set[str] = set()
    for check in checks:
        if type(check) is not dict or set(check) != {"name", "ok", "detail"}:
            raise AcceptanceCheckError("platform_document_schema")
        name = check["name"]
        if type(name) is not str or not 0 < len(name) <= 64 or set(name) - _CHECK_NAME or type(check["ok"]) is not bool:
            raise AcceptanceCheckError("platform_document_schema")
        if check["ok"] is not True:
            raise AcceptanceCheckError("doctor_check_failed")
        names.add(name)
    return {
        "mechanism": "container_apps_job",
        "job_name": job_name,
        "execution_name": execution.name,
        "execution_status": execution.status,
        "report_sha256": _sha256(report),
        "checks_ok": sorted(names),
        "init_schema": job_name == "doctor-schema-init",
    }


def storage_job_details(execution: JobExecution, *, owner_uid: int, owner_gid: int, mode: str) -> StorageJobDetails:
    """The ``provision-storage`` outcome as the driver measured it inside a Job (``stat`` on the NFS directories)."""

    _succeeded(execution, job_name=STORAGE_JOB_NAME, job_names=frozenset({STORAGE_JOB_NAME}))
    return {
        "mechanism": "container_apps_job",
        "job_name": STORAGE_JOB_NAME,
        "execution_name": execution.name,
        "execution_status": execution.status,
        "directories": list(STORAGE_DIRECTORIES),
        "owner_uid": owner_uid,
        "owner_gid": owner_gid,
        "mode": mode,
    }


@trust_boundary(
    tier=3,
    source="the JSON report the verify-blob-managed-identity Job printed, retrieved from Log Analytics",
    source_param="report",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('platform_document_schema') before use unless the report is a dict with exactly "
        "cases_total, cases_passed, blob_sha256, collision_rejected and cleanup_succeeded of the right types; returns only "
        "the owned BlobManagedIdentityDetails (the receipt validator then demands 2/2 and both flags true)"
    ),
    test_ref="tests/unit/web/azure_container_apps_acceptance/test_evidence_projection.py::test_blob_report_projection_rejects_malformed_reports",
    test_fingerprint="53d0c4082e6150fafb6c5cbb979b0e4a037a894ff2c9faf38ccfe4074631eadc",
)
def blob_managed_identity_details(report: object, *, execution: JobExecution) -> BlobManagedIdentityDetails:
    _succeeded(execution, job_name=BLOB_JOB_NAME, job_names=frozenset({BLOB_JOB_NAME}))
    if type(report) is not dict or set(report) != {"cases_total", "cases_passed", "blob_sha256", "collision_rejected", "cleanup_succeeded"}:
        raise AcceptanceCheckError("platform_document_schema")
    total = report["cases_total"]
    passed = report["cases_passed"]
    blob_sha256 = report["blob_sha256"]
    if (
        type(total) is not int
        or type(passed) is not int
        or type(blob_sha256) is not str
        or _SHA256_PATTERN.fullmatch(blob_sha256) is None
        or type(report["collision_rejected"]) is not bool
        or type(report["cleanup_succeeded"]) is not bool
    ):
        raise AcceptanceCheckError("platform_document_schema")
    return {
        "mechanism": "container_apps_job",
        "job_name": BLOB_JOB_NAME,
        "execution_name": execution.name,
        "execution_status": execution.status,
        "cases_total": total,
        "cases_passed": passed,
        "blob_sha256": blob_sha256,
        "collision_rejected": report["collision_rejected"],
        "cleanup_succeeded": report["cleanup_succeeded"],
    }


def revision_rollout_details(
    revisions: Sequence[ActiveRevision],
    *,
    expected_revision: str,
    image_digest: str,
    running_replicas: int,
    health_status: int,
    ready_status: int,
) -> RevisionRolloutDetails:
    """The single-revision-mode proof: one active revision, at 100 %, the candidate, healthy through the ingress."""

    if len(revisions) != 1 or revisions[0].name != expected_revision or revisions[0].traffic_weight != 100:
        raise AcceptanceCheckError("revision_rollout")
    if revisions[0].running_state.lower() != "running":
        raise AcceptanceCheckError("revision_rollout")
    return {
        "mechanism": "single_revision_mode",
        "revision_name": expected_revision,
        "active_revisions": 1,
        "traffic_weight": 100,
        "image_digest": image_digest,
        "running_replicas": running_replicas,
        "health_status": health_status,
        "ready_status": ready_status,
        "deployment_target": DEPLOYMENT_TARGET,
    }


def _metric_timestamp(value: object) -> datetime:
    if type(value) is not str or len(value) > 40:
        raise AcceptanceCheckError("platform_document_schema")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AcceptanceCheckError("platform_document_schema") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise AcceptanceCheckError("platform_document_schema")
    return parsed


@trust_boundary(
    tier=3,
    source="the JSON document `az monitor metrics list --metric active_connections --interval PT1M --aggregation Maximum` printed",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('platform_document_schema' or 'receipt_store_schema') before use unless the payload "
        "carries exactly one active_connections metric with one timeseries of UTC minute points whose maxima are finite "
        "non-negative numbers, and the shared budget validator admits the ten-point window built from them; returns "
        "only the owned ConnectionBudgetDetails"
    ),
    test_ref="tests/unit/web/azure_container_apps_acceptance/test_evidence_projection.py::test_active_connections_projection_rejects_malformed_documents",
    test_fingerprint="c81d301e3f2e5ac63be98c00d89e73d6e16739e33f31b93ab3755863f78e7886",
)
def connection_budget_details(
    payload: object,
    *,
    window_start: datetime,
    acceptance_run_id: str,
    server_id: str,
    max_connections: int,
    approved_budget: int,
    safety_margin: int,
) -> ConnectionBudgetDetails:
    """Project the Azure Monitor series onto the shared budget receipt and validate it there."""

    if type(payload) is not dict or type(payload.get("value")) is not list or len(payload["value"]) != 1:
        raise AcceptanceCheckError("platform_document_schema")
    metric = payload["value"][0]
    if type(metric) is not dict or type(metric.get("name")) is not dict or metric["name"].get("value") != "active_connections":
        raise AcceptanceCheckError("platform_document_schema")
    series = metric.get("timeseries")
    if type(series) is not list or len(series) != 1 or type(series[0]) is not dict or type(series[0].get("data")) is not list:
        raise AcceptanceCheckError("platform_document_schema")
    data = series[0]["data"]
    if len(data) > _MAX_ROWS:
        raise AcceptanceCheckError("platform_document_schema")
    if window_start.tzinfo is None or window_start.utcoffset() is None:
        raise AcceptanceInputError("window_start must be an aware datetime")
    start = window_start.astimezone(UTC)
    expected = [start + timedelta(minutes=offset) for offset in range(10)]
    by_timestamp: dict[datetime, float] = {}
    for point in data:
        if type(point) is not dict:
            raise AcceptanceCheckError("platform_document_schema")
        by_timestamp[_metric_timestamp(point.get("timeStamp"))] = _receipt_number(point.get("maximum"))
    if any(timestamp not in by_timestamp for timestamp in expected):
        raise AcceptanceCheckError("platform_document_schema")
    points: list[BudgetPoint] = [{"timestamp": _utc_timestamp(timestamp), "count": by_timestamp[timestamp]} for timestamp in expected]
    high_water = max(point["count"] for point in points)
    budget: BudgetReceipt = {
        "schema": CONNECTION_BUDGET_SCHEMA,
        "acceptance_run_id_sha256": _sha256(acceptance_run_id.encode("utf-8")),
        "cluster_id_sha256": _sha256(server_id.encode("utf-8")),
        "window_start": _utc_timestamp(start),
        "window_end": _utc_timestamp(start + timedelta(minutes=10)),
        "period_seconds": 60,
        "expected_points": 10,
        "points": points,
        "high_water": high_water,
        "max_connections": max_connections,
        "approved_budget": approved_budget,
        "safety_margin": safety_margin,
        "ok": high_water <= approved_budget <= max_connections - safety_margin and max_connections - high_water >= safety_margin,
    }
    validate_connection_budget_receipt(budget, schema_id=CONNECTION_BUDGET_SCHEMA)
    return {"mechanism": "azure_monitor_metrics", "budget": budget}


@dataclass(frozen=True, slots=True)
class LogAnalyticsRows:
    row_count: int
    canary_absent: bool


@trust_boundary(
    tier=3,
    source="the JSON rows `az monitor log-analytics query` printed for one checked-in KQL file, plus the driver's canary token",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('platform_document_schema' or 'log_analytics_canary') before use unless the payload "
        "is a bounded list of row dicts whose serialisation nowhere contains the canary token; returns only the owned "
        "row count and the canary verdict, never a row"
    ),
    test_ref="tests/unit/web/azure_container_apps_acceptance/test_evidence_projection.py::test_log_analytics_projection_rejects_malformed_rows_and_canary_leaks",
    test_fingerprint="970259f132bd657fc680f03ff2cfb87e90bb96bf583f762aa4f2c4123d0e8241",
)
def project_log_analytics_rows(payload: object, *, canary_token: str) -> LogAnalyticsRows:
    if type(canary_token) is not str or not 16 <= len(canary_token) <= 256:
        raise AcceptanceInputError("canary_token must be a bounded string")
    if type(payload) is not list or len(payload) > _MAX_ROWS or any(type(row) is not dict for row in payload):
        raise AcceptanceCheckError("platform_document_schema")
    serialised = json.dumps(payload, sort_keys=True)
    if len(serialised) > MAX_PLATFORM_DOCUMENT_BYTES:
        raise AcceptanceCheckError("platform_document_schema")
    if canary_token in serialised:
        raise AcceptanceCheckError("log_analytics_canary")
    return LogAnalyticsRows(row_count=len(payload), canary_absent=True)


@trust_boundary(
    tier=3,
    source="the JSON document `az graph query -q \"Resources | where resourceGroup =~ '<rg>' | count\"` printed",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('platform_document_schema') before use unless the payload is a bare non-negative "
        "integer or a dict whose data member is a one-element list carrying a non-negative integer Count; returns that integer"
    ),
    test_ref="tests/unit/web/azure_container_apps_acceptance/test_evidence_projection.py::test_resource_graph_projection_rejects_malformed_documents",
    test_fingerprint="b607786c9cc5aa1f118efb787ee058cc3155d7a8a02ba288d978c32efc9c1530",
)
def project_resource_graph_count(payload: object) -> int:
    count = payload
    if type(payload) is dict:
        data = payload.get("data")
        if type(data) is not list or len(data) != 1 or type(data[0]) is not dict:
            raise AcceptanceCheckError("platform_document_schema")
        count = data[0].get("Count")
    if type(count) is not int or count < 0:
        raise AcceptanceCheckError("platform_document_schema")
    return count


def resource_graph_cleanup_details(
    *, resource_group: str, remaining_resources: int, key_vault_purged: bool, scheduled_purge_date: datetime | None
) -> ResourceGraphCleanupDetails:
    if remaining_resources != 0:
        raise AcceptanceCheckError("resource_graph_cleanup")
    if key_vault_purged == (scheduled_purge_date is not None):
        raise AcceptanceInputError("a vault is purged or tombstoned with a scheduled purge date, never both or neither")
    return {
        "mechanism": "resource_graph_query",
        "resource_group_sha256": _sha256(resource_group.encode("utf-8")),
        "remaining_resources": 0,
        "key_vault_purged": key_vault_purged,
        "key_vault_tombstoned": not key_vault_purged,
        "scheduled_purge_date": None if scheduled_purge_date is None else _utc_timestamp(scheduled_purge_date),
    }


@trust_boundary(
    tier=3,
    source="the JSON list `az containerapp ingress traffic show` printed (label / weight entries)",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('platform_document_schema') before use unless the payload is a bounded list of dicts "
        "each carrying a lowercase label and an integer weight in 0..100; returns only owned LabelWeight values"
    ),
    test_ref="tests/unit/web/azure_container_apps_acceptance/test_evidence_projection.py::test_traffic_projection_rejects_malformed_documents",
    test_fingerprint="b51ffbf4257f1060c6e994de5f188834d2b1c392272c0be420c15b1bf16ff388",
)
def project_label_weights(payload: object) -> tuple[LabelWeight, ...]:
    if type(payload) is not list or len(payload) > 100:
        raise AcceptanceCheckError("platform_document_schema")
    weights: list[LabelWeight] = []
    for entry in payload:
        if type(entry) is not dict:
            raise AcceptanceCheckError("platform_document_schema")
        weight = entry.get("weight")
        if type(weight) is not int or not 0 <= weight <= 100:
            raise AcceptanceCheckError("platform_document_schema")
        weights.append(LabelWeight(label=_identifier(entry.get("label")), weight=weight))
    return tuple(weights)


def _observed_response(value: object) -> ReplicaResponse:
    if type(value) is not dict or set(value) != {"addressed_to", "status", "instance_id", "body"}:
        raise AcceptanceCheckError("probe_observation_schema")
    addressed_to = value["addressed_to"]
    status = value["status"]
    instance_id = value["instance_id"]
    if type(addressed_to) is not str or not 0 < len(addressed_to) <= 64 or type(status) is not int or not 100 <= status <= 599:
        raise AcceptanceCheckError("probe_observation_schema")
    if instance_id is not None and (type(instance_id) is not str or not 0 < len(instance_id) <= 128):
        raise AcceptanceCheckError("probe_observation_schema")
    return replica_response_from_envelope(addressed_to=addressed_to, status=status, instance_id=instance_id, body=value["body"])


def _observed_timestamp(value: object) -> datetime:
    try:
        return _metric_timestamp(value)
    except AcceptanceCheckError:
        raise AcceptanceCheckError("probe_observation_schema") from None


def _observed_seconds(value: object) -> float:
    try:
        return _receipt_number(value)
    except AcceptanceCheckError:
        raise AcceptanceCheckError("probe_observation_schema") from None


def _observed_text(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or not 0 < len(value) <= 256:
        raise AcceptanceCheckError("probe_observation_schema")
    return value


@trust_boundary(
    tier=3,
    source="the P3 observation document the acceptance driver assembled from its psql reads and label-URL responses",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('probe_observation_schema') before use unless the document carries exactly the P3 "
        "fields — a known primitive, bounded owner and survivor ids, a membership row or null, two closed response "
        "records, a UTC takeover timestamp, an optional cancellation reason, an optional fence owner and a non-negative "
        "duplicate count; returns only the owned LeaseTakeoverObservation the shared decision scores"
    ),
    test_ref="tests/unit/web/azure_container_apps_acceptance/test_evidence_projection.py::test_lease_takeover_observation_rejects_malformed_documents",
    test_fingerprint="a10fc66e28b6ac4511c49b8655f08af4357a15b99772680ca04bf345a9e524f1",
)
def lease_takeover_observation(payload: object) -> LeaseTakeoverObservation:
    fields = {
        "primitive",
        "owner_instance_id",
        "survivor_instance_id",
        "owner_row",
        "before_expiry",
        "after_expiry",
        "takeover_observed_at",
        "cancelled_run_reason",
        "fence_owner_after",
        "duplicate_sink_effects",
    }
    if type(payload) is not dict or set(payload) != fields or payload["primitive"] not in {"role_revocation", "revision_deactivate"}:
        raise AcceptanceCheckError("probe_observation_schema")
    row = payload["owner_row"]
    owner_row: MembershipRow | None = None
    if row is not None:
        if type(row) is not dict or set(row) != {"instance_id", "state", "lease_expires_at"}:
            raise AcceptanceCheckError("probe_observation_schema")
        owner_row = MembershipRow(
            instance_id=cast(str, _observed_text(row["instance_id"])),
            state=cast(str, _observed_text(row["state"])),
            lease_expires_at=_observed_timestamp(row["lease_expires_at"]),
        )
    duplicates = payload["duplicate_sink_effects"]
    if type(duplicates) is not int or duplicates < 0:
        raise AcceptanceCheckError("probe_observation_schema")
    return LeaseTakeoverObservation(
        primitive=payload["primitive"],
        owner_instance_id=cast(str, _observed_text(payload["owner_instance_id"])),
        survivor_instance_id=cast(str, _observed_text(payload["survivor_instance_id"])),
        owner_row=owner_row,
        before_expiry=_observed_response(payload["before_expiry"]),
        after_expiry=_observed_response(payload["after_expiry"]),
        takeover_observed_at=_observed_timestamp(payload["takeover_observed_at"]),
        cancelled_run_reason=_observed_text(payload["cancelled_run_reason"], optional=True),
        fence_owner_after=_observed_text(payload["fence_owner_after"], optional=True),
        duplicate_sink_effects=duplicates,
    )


@trust_boundary(
    tier=3,
    source="the P4a observation document the acceptance driver assembled from its reads through the non-owner label URL and NFS",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('probe_observation_schema') before use unless the document carries exactly the P4a "
        "fields — bounded owner and reader ids, finite non-negative seconds, two sha256 blob digests and an optional "
        "terminal status; returns only the owned CrossReplicaProgressObservation the shared decision scores"
    ),
    test_ref="tests/unit/web/azure_container_apps_acceptance/test_evidence_projection.py::test_progress_observation_rejects_malformed_documents",
    test_fingerprint="22c0c97b5099e3d3c9aedaaa1e5a02232fc958193217bd18569f2d79f6857569",
)
def cross_replica_progress_observation(payload: object) -> CrossReplicaProgressObservation:
    seconds_fields = (
        "poll_interval_seconds",
        "status_visible_after_seconds",
        "outputs_visible_after_seconds",
        "messages_visible_after_seconds",
    )
    fields = {
        "owner_instance_id",
        "reader_instance_id",
        "blob_sha256_via_owner",
        "blob_sha256_via_reader",
        "terminal_status_on_reader",
        *seconds_fields,
    }
    if type(payload) is not dict or set(payload) != fields:
        raise AcceptanceCheckError("probe_observation_schema")
    seconds = {name: _observed_seconds(payload[name]) for name in seconds_fields}
    for digest in ("blob_sha256_via_owner", "blob_sha256_via_reader"):
        if type(payload[digest]) is not str or _SHA256_PATTERN.fullmatch(payload[digest]) is None:
            raise AcceptanceCheckError("probe_observation_schema")
    return CrossReplicaProgressObservation(
        owner_instance_id=cast(str, _observed_text(payload["owner_instance_id"])),
        reader_instance_id=cast(str, _observed_text(payload["reader_instance_id"])),
        poll_interval_seconds=seconds["poll_interval_seconds"],
        status_visible_after_seconds=seconds["status_visible_after_seconds"],
        outputs_visible_after_seconds=seconds["outputs_visible_after_seconds"],
        messages_visible_after_seconds=seconds["messages_visible_after_seconds"],
        blob_sha256_via_owner=payload["blob_sha256_via_owner"],
        blob_sha256_via_reader=payload["blob_sha256_via_reader"],
        terminal_status_on_reader=_observed_text(payload["terminal_status_on_reader"], optional=True),
    )


def lease_takeover_for_receipt(result: ProbeResult) -> ProbeResult:
    """P3 as this provider records it: a graceful-stop takeover is never a pass.

    The shared decision downgrades the mechanism when the owner's row reads
    ``stopped`` or ``draining`` but still scores the takeover itself; here that
    becomes a failed probe with the reason on record, because nothing about a
    *dead* owner was proven and the receipt validator refuses a passing
    ``graceful_stop`` outright.
    """

    if result.probe != "P3":
        raise AcceptanceInputError("lease_takeover_for_receipt takes a P3 result")
    if result.mechanism == "graceful_stop" and result.outcome == "pass":
        return ProbeResult(
            probe="P3",
            outcome="fail",
            mechanism="graceful_stop",
            reasons=("owner_reached_its_release_path:graceful_stop_is_not_a_dead_owner",),
            evidence=result.evidence,
        )
    return result


# --------------------------------------------------------------------------- receipt store

_INDEX_NAME: Final = "index.json"
_INDEX_ROW_FIELDS: Final[frozenset[str]] = frozenset(ReceiptIndexRow.__required_keys__)


def _write_protected(path: Path, content: bytes, *, check: str) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise AcceptanceCheckError(check) from None
    try:
        os.write(descriptor, content)
    except OSError:
        raise AcceptanceCheckError(check) from None
    finally:
        os.close(descriptor)


def _require_store_directory(store_dir: Path) -> None:
    try:
        store_dir.mkdir(mode=0o700, exist_ok=True)
        directory = store_dir.lstat()
    except OSError:
        raise AcceptanceCheckError("receipt_store_write") from None
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or directory.st_mode & 0o077:
        raise AcceptanceCheckError("receipt_store_write")


@trust_boundary(
    tier=3,
    source="the receipt index document (index.json) read back from the Container Apps receipt store directory",
    source_param="document",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('receipt_store_index') before use unless the document is a dict whose receipts "
        "member is a bounded list of rows with exactly the index fields, a known kind, sha256 subject and receipt "
        "hashes and a bounded scenario id; returns only owned ReceiptIndexRow values"
    ),
    test_ref="tests/unit/web/azure_container_apps_acceptance/test_evidence_projection.py::test_receipt_index_rejects_malformed_rows",
    test_fingerprint="d58e675aee757a5b229de0a8900b8048ed93e4f2db544a5daa2418498dd4a273",
)
def _index_rows(document: object) -> list[ReceiptIndexRow]:
    if type(document) is not dict or type(document.get("receipts")) is not list or len(document["receipts"]) > 10_000:
        raise AcceptanceCheckError("receipt_store_index")
    rows: list[ReceiptIndexRow] = []
    for row in document["receipts"]:
        if type(row) is not dict or set(row) != _INDEX_ROW_FIELDS or any(type(value) is not str for value in row.values()):
            raise AcceptanceCheckError("receipt_store_index")
        if (
            row["kind"] not in STORED_RECEIPT_KINDS
            or _SHA256_PATTERN.fullmatch(row["subject_sha256"]) is None
            or _SHA256_PATTERN.fullmatch(row["receipt_sha256"]) is None
            or not 0 < len(row["scenario_id"]) <= 64
            or not 0 < len(row["stored_at"]) <= 32
        ):
            raise AcceptanceCheckError("receipt_store_index")
        rows.append(
            ReceiptIndexRow(
                scenario_id=row["scenario_id"],
                kind=row["kind"],
                subject_sha256=row["subject_sha256"],
                receipt_sha256=row["receipt_sha256"],
                stored_at=row["stored_at"],
            )
        )
    return rows


def read_receipt_index(store_dir: Path) -> list[ReceiptIndexRow]:
    index_path = store_dir / _INDEX_NAME
    if not index_path.exists():
        return []
    return _index_rows(_read_protected_document(index_path, check="receipt_store_index"))


def _write_index(store_dir: Path, rows: Sequence[ReceiptIndexRow]) -> None:
    content = json.dumps({"receipts": list(rows)}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(content) > MAX_CONTROL_DOCUMENT_BYTES:
        raise AcceptanceCheckError("receipt_store_write")
    staging = store_dir / f"{_INDEX_NAME}.{os.getpid()}.tmp"
    _write_protected(staging, content, check="receipt_store_write")
    try:
        os.replace(staging, store_dir / _INDEX_NAME)
    except OSError:
        raise AcceptanceCheckError("receipt_store_write") from None


def receipt_store(
    store_dir: Path,
    *,
    kind: str,
    scenario_id: str,
    subject_id: str,
    candidate_sha: str,
    document: object,
    now: datetime,
) -> StoredReceipt:
    """Validate one receipt for ``kind`` and persist it under its canonical hash; re-storing the same bytes is idempotent."""

    if (
        type(subject_id) is not str
        or not 0 < len(subject_id) <= 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in subject_id)
    ):
        raise AcceptanceCheckError("receipt_store_binding")
    subject_sha256 = _sha256(subject_id.encode("utf-8"))
    receipt = validate_stored_receipt(
        document, kind=kind, scenario_id=scenario_id, subject_sha256=subject_sha256, candidate_sha=candidate_sha
    )
    _require_store_directory(store_dir)
    stored_path = store_dir / f"{receipt.receipt_sha256}.json"
    if stored_path.exists():
        existing = _read_protected_document(stored_path, check="receipt_store_file")
        if json.dumps(existing, sort_keys=True, separators=(",", ":")) != receipt.canonical_json:
            raise AcceptanceCheckError("receipt_store_conflict")
    else:
        _write_protected(stored_path, receipt.canonical_json.encode("utf-8"), check="receipt_store_write")
    rows = read_receipt_index(store_dir)
    matching = [
        row for row in rows if row["scenario_id"] == scenario_id and row["kind"] == kind and row["subject_sha256"] == subject_sha256
    ]
    if matching:
        if any(row["receipt_sha256"] != receipt.receipt_sha256 for row in matching) and kind != TESTCONTAINER_RUN_RECEIPT_KIND:
            raise AcceptanceCheckError("receipt_store_conflict")
        if any(row["receipt_sha256"] == receipt.receipt_sha256 for row in matching):
            return receipt
    rows.append(
        ReceiptIndexRow(
            scenario_id=scenario_id,
            kind=kind,
            subject_sha256=subject_sha256,
            receipt_sha256=receipt.receipt_sha256,
            stored_at=_utc_timestamp(now),
        )
    )
    _write_index(store_dir, rows)
    return receipt


# --------------------------------------------------------------------------- bundle check

MUST_PASS_PROBES: Final[frozenset[str]] = frozenset({"P1", "P2", "P3", "P4a"})
"""P4b is recorded and structurally cannot pass; every other probe must, and an ``unreachable`` P3 is not waived."""


@dataclass(frozen=True, slots=True)
class InvalidReceipt:
    """One indexed receipt the bundle check could not re-admit, and the static check that refused it."""

    receipt_sha256: str
    check: str


@dataclass(frozen=True, slots=True)
class BundleVerdict:
    passed: bool
    missing_kinds: tuple[str, ...]
    invalid_receipts: tuple[InvalidReceipt, ...]
    failed_probes: tuple[str, ...]
    testcontainer_reason: str | None
    testcontainer_receipt_sha256: str | None

    def __post_init__(self) -> None:
        refusals = self.missing_kinds or self.invalid_receipts or self.failed_probes or self.testcontainer_reason is not None
        if self.passed == bool(refusals):
            raise ValueError("a bundle passes exactly when nothing was missing, invalid, failed or refused")
        if self.testcontainer_reason is not None and self.testcontainer_reason not in TESTCONTAINER_RUN_GATE_REASONS:
            raise ValueError("testcontainer_reason must be one of the shared gate reasons")
        if any(probe not in PROBES for probe in self.failed_probes):
            raise ValueError("failed_probes must name probes")


def bundle_check(store_dir: Path, *, candidate_sha: str, scenario_id: str) -> BundleVerdict:
    """Every stored kind present, every receipt re-validated and hash-bound, the probes passed, the testcontainer run gated."""

    rows = [row for row in read_receipt_index(store_dir) if row["scenario_id"] == scenario_id]
    present = {row["kind"] for row in rows}
    missing = tuple(sorted(STORED_RECEIPT_KINDS - present))
    invalid: list[InvalidReceipt] = []
    failed_probes: list[str] = []
    for row in rows:
        if row["kind"] == TESTCONTAINER_RUN_RECEIPT_KIND:
            continue
        path = store_dir / f"{row['receipt_sha256']}.json"
        try:
            receipt = validate_stored_receipt(
                _read_protected_document(path, check="receipt_store_file"),
                kind=row["kind"],
                scenario_id=scenario_id,
                subject_sha256=row["subject_sha256"],
                candidate_sha=candidate_sha,
            )
        except AcceptanceCheckError as exc:
            # Recorded, not swallowed: the verdict names the receipt and the check that refused it.
            invalid.append(InvalidReceipt(receipt_sha256=row["receipt_sha256"], check=exc.check))
            continue
        if receipt.receipt_sha256 != row["receipt_sha256"]:
            invalid.append(InvalidReceipt(receipt_sha256=row["receipt_sha256"], check="receipt_store_hash"))
            continue
        if row["kind"] in PROBE_KINDS:
            # Own bytes, already admitted: the details are read directly.
            details = cast(Mapping[str, object], json.loads(receipt.canonical_json)["details"])
            probe = PROBE_KINDS[row["kind"]]
            if details["outcome"] != "pass":
                failed_probes.append(probe)
            if row["kind"] == "replica-progress" and cast(Mapping[str, object], details["owner_affine"])["outcome"] != "cannot_pass":
                failed_probes.append("P4b")
    verdict = testcontainer_run_gate(
        rows,
        provider=PROVIDER,
        candidate_sha=candidate_sha,
        read_receipt=lambda receipt_sha256: _read_protected_document(
            store_dir / f"{receipt_sha256}.json", check="testcontainer_run_receipt"
        ),
    )
    failed = tuple(sorted((set(failed_probes) & MUST_PASS_PROBES) | ({"P4b"} if "P4b" in failed_probes else set())))
    return BundleVerdict(
        passed=not missing and not invalid and not failed and verdict.passed,
        missing_kinds=missing,
        invalid_receipts=tuple(sorted(invalid, key=lambda item: item.receipt_sha256)),
        failed_probes=failed,
        testcontainer_reason=verdict.reason,
        testcontainer_receipt_sha256=verdict.receipt_sha256,
    )
