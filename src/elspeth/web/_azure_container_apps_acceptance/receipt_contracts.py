"""Receipt contracts for the Azure Container Apps acceptance: one provider binding.

The envelope validator, the bounded-document admission, the ``schema_facts``
derivation and the probe vocabulary are the shared core's; this module adds
only what varies per provider:

- **Subject.** ``replica_binding_sha256 = sha256("<container app ARM id>/revisions/"
  + CONTAINER_APP_REVISION + "/replicas/" + CONTAINER_APP_REPLICA_NAME)``. There
  is no metadata endpoint on Container Apps, so the driver takes the revision
  and replica names from ``az containerapp replica list`` (control-plane
  verified) and the ARM id from ``az containerapp show``; every receipt names
  the replica the evidence was collected against (plan §5).
- **Kinds.** Twelve closed check kinds (plan §6.1 item 4), each with a closed
  detail set and a ``mechanism`` from a closed enum: overclaiming is a schema
  violation. The replica kinds carry the shared :class:`ProbeReceiptDetails`
  shape and are re-admitted through :class:`ProbeResult`, so the P4b
  cannot-pass rule and the per-probe mechanism subsets hold by construction.
  ``testcontainer-run`` is the thirteenth *stored* kind, validated by the
  shared validator under the ``azure`` schema id.
- **Compatibility record.** Scenario A only, in the shape the acceptance
  runbook fixes, with ``schema_facts`` byte-equal to the shared derivation.

The closed field sets the validators enforce are derived from the owned
``TypedDict`` shapes below, so the two cannot drift.
"""

from __future__ import annotations

import base64
import binascii
import functools
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final, TypedDict, cast

from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web._acceptance_common.errors import AcceptanceCheckError, AcceptanceInputError
from elspeth.web._acceptance_common.receipt_validation import (
    _GIT_SHA_PATTERN,
    _SCENARIO_ID_PATTERN,
    _SHA256_PATTERN,
    MAX_EXEC_RECEIPT_CHARS,
    MAX_EXEC_STREAM_BYTES,
    ExecReceiptDescriptor,
    _parse_utc_z_timestamp,
    _receipt_number,
    _sha256,
    _validate_bounded_receipt_document,
    validate_connection_budget_receipt,
    validate_exec_receipt_schema,
)
from elspeth.web._acceptance_common.replica_probes import (
    MECHANISMS as PROBE_MECHANISM_SET,
)
from elspeth.web._acceptance_common.replica_probes import (
    PROBE_MECHANISMS,
    Mechanism,
    Probe,
    ProbeOutcome,
    ProbeReceiptDetails,
    ProbeResult,
)
from elspeth.web._acceptance_common.schema_facts import _CANDIDATE_PACKAGE_VERSION, _expected_schema_facts
from elspeth.web._acceptance_common.testcontainer_run import (
    TESTCONTAINER_RUN_RECEIPT_KIND,
    validate_testcontainer_run_receipt,
)

PROVIDER: Final = "azure"
EXEC_RECEIPT_PREFIX: Final = "ELSPETH_ACCEPTANCE_RECEIPT_V1:"
COMPATIBILITY_RECEIPT_SCHEMA: Final = "elspeth.azure-container-apps-compatibility-receipt.v1"
CONNECTION_BUDGET_SCHEMA: Final = "elspeth.postgres-flexible-connection-budget.v1"
DEPLOYMENT_TARGET: Final = "azure-container-apps"

# --------------------------------------------------------------------------- closed vocabularies

PLATFORM_MECHANISMS: Final[frozenset[str]] = frozenset(
    {
        "container_apps_job",
        "log_analytics_query",
        "azure_monitor_metrics",
        "operator_record",
        "single_revision_mode",
        "resource_graph_query",
    }
)
MECHANISMS: Final[frozenset[str]] = PROBE_MECHANISM_SET | PLATFORM_MECHANISMS
"""Every mechanism any Container Apps receipt may name; probe kinds are further narrowed per probe."""

PROBE_KINDS: Final[Mapping[str, Probe]] = MappingProxyType(
    {"replica-fence-conflict": "P1", "replica-run-start": "P2", "replica-lease-takeover": "P3", "replica-progress": "P4a"}
)
KIND_MECHANISMS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "verify-doctor-job": frozenset({"container_apps_job"}),
        "verify-storage-job": frozenset({"container_apps_job"}),
        "verify-blob-managed-identity": frozenset({"container_apps_job"}),
        "verify-log-analytics": frozenset({"log_analytics_query"}),
        "verify-connection-budget": frozenset({"azure_monitor_metrics"}),
        "compatibility-record": frozenset({"operator_record"}),
        "revision-rollout": frozenset({"single_revision_mode"}),
        "replica-fence-conflict": PROBE_MECHANISMS["P1"],
        "replica-run-start": PROBE_MECHANISMS["P2"],
        "replica-lease-takeover": PROBE_MECHANISMS["P3"],
        "replica-progress": PROBE_MECHANISMS["P4a"],
        "resource-graph-cleanup": frozenset({"resource_graph_query"}),
    }
)
"""The twelve check kinds and the mechanisms each may claim."""

CHECK_KINDS: Final[frozenset[str]] = frozenset(KIND_MECHANISMS)
STORED_RECEIPT_KINDS: Final[frozenset[str]] = CHECK_KINDS | {TESTCONTAINER_RUN_RECEIPT_KIND}
"""What the receipt store admits: the twelve exec kinds plus the shared ``testcontainer-run``."""

DOCTOR_JOB_NAMES: Final[frozenset[str]] = frozenset({"doctor-schema-init", "doctor-runtime-a", "doctor-runtime-b"})
DOCTOR_REQUIRED_CHECKS: Final[frozenset[str]] = frozenset(
    {"blob_writable", "data_dir_writable", "landscape_schema", "payload_store_writable", "session_schema"}
)
"""Check names ``elspeth doctor deployment --json`` emits that a passing Job must report ok (``web/doctor.py``).

TLS is enforced by the connection URLs (``sslmode=verify-full``); the
provider-neutral doctor emits no ``session_tls`` / ``landscape_tls`` check —
those exist only under ``doctor aws-ecs`` (measured: ``_collect_deployment_checks``).
"""
STORAGE_JOB_NAME: Final = "provision-storage"
STORAGE_DIRECTORIES: Final[tuple[str, ...]] = ("/mnt/elspeth/data", "/mnt/elspeth/data/blobs", "/mnt/elspeth/payloads")
STORAGE_OWNER_ID: Final = 1654
BLOB_JOB_NAME: Final = "verify-blob-managed-identity"
KQL_QUERY_NAMES: Final[tuple[str, ...]] = ("doctor-report", "fence-conflict-409", "replica-lifecycle", "run-sentinel-by-replica")
MAX_INGESTION_LAG_SECONDS: Final = 600
"""The driver's Log Analytics polling ceiling (platform facts §5.1: 3-10 minutes end to end)."""

_IDENTIFIER_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,127}\Z")
_CHECK_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_IMAGE_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,30}[a-z0-9]--[a-z][a-z0-9-]{0,63}\Z")
_CONTAINER_APP_ID_PATTERN = re.compile(
    r"/subscriptions/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"/resourceGroups/[A-Za-z0-9._()-]{1,90}/providers/Microsoft\.App/containerApps/[a-z][a-z0-9-]{0,30}[a-z0-9]\Z",
    re.IGNORECASE,
)
_OPERATOR_IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}\Z")


# --------------------------------------------------------------------------- owned detail shapes


class JobDetails(TypedDict):
    """``verify-doctor-job``: one doctor Job execution; ``checks_ok`` is every check its ``--json`` report passed (all of them)."""

    mechanism: str
    job_name: str
    execution_name: str
    execution_status: str
    report_sha256: str
    checks_ok: list[str]
    init_schema: bool


class StorageJobDetails(TypedDict):
    """``verify-storage-job``: the ``provision-storage`` Job created the NFS directories as UID 1654 mode 0700."""

    mechanism: str
    job_name: str
    execution_name: str
    execution_status: str
    directories: list[str]
    owner_uid: int
    owner_gid: int
    mode: str


class BlobManagedIdentityDetails(TypedDict):
    """``verify-blob-managed-identity``: the two ``azure_blob@managed_identity`` cases inside the environment."""

    mechanism: str
    job_name: str
    execution_name: str
    execution_status: str
    cases_total: int
    cases_passed: int
    blob_sha256: str
    collision_rejected: bool
    cleanup_succeeded: bool


class LogAnalyticsQueryDetails(TypedDict):
    name: str
    query_sha256: str
    row_count: int
    ingestion_lag_seconds_max: float


class LogAnalyticsDetails(TypedDict):
    """``verify-log-analytics``: the four checked-in KQL files ran, returned rows, and carried no canary token."""

    mechanism: str
    workspace_id_sha256: str
    queries: list[LogAnalyticsQueryDetails]
    canary_tokens_absent: bool


class BudgetPoint(TypedDict):
    timestamp: str
    count: float


class BudgetReceipt(TypedDict):
    """The shared connection-budget receipt under the Flexible Server schema id."""

    schema: str
    acceptance_run_id_sha256: str
    cluster_id_sha256: str
    window_start: str
    window_end: str
    period_seconds: int
    expected_points: int
    points: list[BudgetPoint]
    high_water: float
    max_connections: int
    approved_budget: int
    safety_margin: int
    ok: bool


class ConnectionBudgetDetails(TypedDict):
    """``verify-connection-budget``: ``az monitor metrics list --metric active_connections`` through the shared validator."""

    mechanism: str
    budget: BudgetReceipt


class CompatibilityRecordDetails(TypedDict):
    """``compatibility-record``: the operator record validated and passed through the shared gate."""

    mechanism: str
    schema: str
    record_sha256: str
    scenario_id: str
    gate_passed: bool
    failed_clauses: list[str]


class RevisionRolloutDetails(TypedDict):
    """``revision-rollout``: one active revision at 100 % on the candidate digest, probes answering through the ingress."""

    mechanism: str
    revision_name: str
    active_revisions: int
    traffic_weight: int
    image_digest: str
    running_replicas: int
    health_status: int
    ready_status: int
    deployment_target: str


class ReplicaProgressDetails(ProbeReceiptDetails):
    """``replica-progress``: the P4a result plus the P4b ``owner_affine`` record that cannot pass."""

    owner_affine: ProbeReceiptDetails


class ResourceGraphCleanupDetails(TypedDict):
    """``resource-graph-cleanup``: the group is gone from Resource Graph; the vault was purged or tombstoned."""

    mechanism: str
    resource_group_sha256: str
    remaining_resources: int
    key_vault_purged: bool
    key_vault_tombstoned: bool
    scheduled_purge_date: str | None


CheckDetails = (
    JobDetails
    | StorageJobDetails
    | BlobManagedIdentityDetails
    | LogAnalyticsDetails
    | ConnectionBudgetDetails
    | CompatibilityRecordDetails
    | RevisionRolloutDetails
    | ProbeReceiptDetails
    | ReplicaProgressDetails
    | ResourceGraphCleanupDetails
)

_JOB_FIELDS: Final[frozenset[str]] = frozenset(JobDetails.__required_keys__)
_STORAGE_FIELDS: Final[frozenset[str]] = frozenset(StorageJobDetails.__required_keys__)
_BLOB_FIELDS: Final[frozenset[str]] = frozenset(BlobManagedIdentityDetails.__required_keys__)
_LOG_ANALYTICS_FIELDS: Final[frozenset[str]] = frozenset(LogAnalyticsDetails.__required_keys__)
_LOG_QUERY_FIELDS: Final[frozenset[str]] = frozenset(LogAnalyticsQueryDetails.__required_keys__)
_BUDGET_FIELDS: Final[frozenset[str]] = frozenset(ConnectionBudgetDetails.__required_keys__)
_COMPATIBILITY_FIELDS: Final[frozenset[str]] = frozenset(CompatibilityRecordDetails.__required_keys__)
_ROLLOUT_FIELDS: Final[frozenset[str]] = frozenset(RevisionRolloutDetails.__required_keys__)
_PROBE_FIELDS: Final[frozenset[str]] = frozenset(ProbeReceiptDetails.__required_keys__)
_PROGRESS_FIELDS: Final[frozenset[str]] = frozenset(ReplicaProgressDetails.__required_keys__)
_CLEANUP_FIELDS: Final[frozenset[str]] = frozenset(ResourceGraphCleanupDetails.__required_keys__)


# --------------------------------------------------------------------------- replica binding


@dataclass(frozen=True, slots=True)
class ReplicaBinding:
    """The platform subject every receipt is bound to: one replica of one revision of one Container App."""

    container_app_id: str
    revision: str
    replica: str

    def __post_init__(self) -> None:
        if _CONTAINER_APP_ID_PATTERN.fullmatch(self.container_app_id) is None:
            raise AcceptanceInputError("container_app_id must be a Microsoft.App/containerApps ARM resource id")
        if _REVISION_PATTERN.fullmatch(self.revision) is None or not self.revision.startswith(
            f"{self.container_app_id.rsplit('/', 1)[1].lower()}--"
        ):
            raise AcceptanceInputError("revision must be <container app name>--<suffix>")
        if _IDENTIFIER_PATTERN.fullmatch(self.replica) is None or not self.replica.startswith(f"{self.revision}-"):
            raise AcceptanceInputError("replica must be a replica name of the revision")

    @property
    def subject(self) -> str:
        return f"{self.container_app_id}/revisions/{self.revision}/replicas/{self.replica}"

    @property
    def sha256(self) -> str:
        return _sha256(self.subject.encode("utf-8"))


# --------------------------------------------------------------------------- detail validators

_KIND_FIELDS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "verify-doctor-job": _JOB_FIELDS,
        "verify-storage-job": _STORAGE_FIELDS,
        "verify-blob-managed-identity": _BLOB_FIELDS,
        "verify-log-analytics": _LOG_ANALYTICS_FIELDS,
        "verify-connection-budget": _BUDGET_FIELDS,
        "compatibility-record": _COMPATIBILITY_FIELDS,
        "revision-rollout": _ROLLOUT_FIELDS,
        "replica-fence-conflict": _PROBE_FIELDS,
        "replica-run-start": _PROBE_FIELDS,
        "replica-lease-takeover": _PROBE_FIELDS,
        "replica-progress": _PROGRESS_FIELDS,
        "resource-graph-cleanup": _CLEANUP_FIELDS,
    }
)
"""Each kind's closed field set, derived from its owned TypedDict."""


def _schema_violation() -> AcceptanceCheckError:
    return AcceptanceCheckError("exec_receipt_schema")


def _text(value: object, pattern: re.Pattern[str] = _IDENTIFIER_PATTERN) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise _schema_violation()
    return value


def _sha256_text(value: object) -> str:
    return _text(value, _SHA256_PATTERN)


def _count(value: object, *, minimum: int = 0, maximum: int = 2**31) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _schema_violation()
    return value


def _flag(value: object) -> bool:
    if type(value) is not bool:
        raise _schema_violation()
    return value


def _exact(value: object, expected: object) -> None:
    # ``==`` alone would let ``True`` stand in for ``1``: the type must agree too.
    if type(value) is not type(expected) or value != expected:
        raise _schema_violation()


def _job_execution(*, kind: str, job_name: object, execution_name: object, execution_status: object, job_names: frozenset[str]) -> str:
    name = _text(job_name)
    if name not in job_names:
        raise _schema_violation()
    _text(execution_name)
    _exact(execution_status, "Succeeded")
    return name


def _probe_result(
    *, probe: Probe, recorded_probe: object, outcome: object, mechanism: object, reasons: object, evidence: object
) -> ProbeResult:
    """Re-admit one probe record through the owned type: closed vocabularies, P4b cannot pass, reasons iff not pass."""

    if (
        recorded_probe != probe
        or type(outcome) is not str
        or type(mechanism) is not str
        or type(reasons) is not list
        or type(evidence) is not dict
    ):
        raise _schema_violation()
    # The casts assert nothing: ProbeResult's own construction rejects an
    # outcome or mechanism outside the closed vocabularies with ValueError.
    try:
        return ProbeResult(
            probe=probe,
            outcome=cast(ProbeOutcome, outcome),
            mechanism=cast(Mechanism, mechanism),
            reasons=tuple(reasons),
            evidence=evidence,
        )
    except ValueError:
        raise _schema_violation() from None


def _log_query(query: object) -> str:
    if type(query) is not dict or set(query) != _LOG_QUERY_FIELDS:
        raise _schema_violation()
    name = _text(query["name"])
    _sha256_text(query["query_sha256"])
    _count(query["row_count"], minimum=1)
    if _receipt_number(query["ingestion_lag_seconds_max"]) > MAX_INGESTION_LAG_SECONDS:
        raise _schema_violation()
    return name


def _purge_date(*, purged: object, tombstoned: object, scheduled: object) -> None:
    if _flag(purged) == _flag(tombstoned):
        raise _schema_violation()
    if tombstoned:
        if type(scheduled) is not str:
            raise _schema_violation()
        try:
            _parse_utc_z_timestamp(scheduled)
        except ValueError:
            raise _schema_violation() from None
    elif scheduled is not None:
        raise _schema_violation()


@trust_boundary(
    tier=3,
    source="the details object of one Container Apps exec receipt: decoded from a receipt line the driver captured, or read back from the receipt store",
    source_param="details",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('exec_receipt_schema', or 'receipt_store_schema' from the shared budget validator) "
        "before use unless the key set is exactly the kind's closed field set, the mechanism is one the kind may claim, "
        "every scalar has the exact type the kind demands (a bool never stands in for a number), and the kind's own "
        "facts hold: a succeeded Job, the doctor checks, the three NFS directories as 1654/0700, 2/2 blob cases, the "
        "four KQL queries under the ingestion ceiling with no canary, one revision at 100 %, a passed gate, zero "
        "remaining resources with one vault fate, and a probe record the owned ProbeResult re-admits - where P4b "
        "cannot pass and a graceful_stop P3 cannot record a pass"
    ),
    test_ref="tests/unit/web/azure_container_apps_acceptance/test_receipt_contracts.py::test_every_kind_rejects_open_field_sets",
    test_fingerprint="e74f644ccbaa08ac62a05302d394178da306ed9a1c5117a7fa7b110f94dc0ea6",
)
def validate_check_details(kind: str, details: Mapping[str, object]) -> None:
    """The one detail validator; the descriptor binds it per kind."""

    if kind not in _KIND_FIELDS or set(details) != _KIND_FIELDS[kind]:
        raise _schema_violation()
    mechanism = details["mechanism"]
    if type(mechanism) is not str or mechanism not in KIND_MECHANISMS[kind]:
        raise _schema_violation()
    if kind in PROBE_KINDS:
        result = _probe_result(
            probe=PROBE_KINDS[kind],
            recorded_probe=details["probe"],
            outcome=details["outcome"],
            mechanism=mechanism,
            reasons=details["reasons"],
            evidence=details["evidence"],
        )
        # A ``stopped``/``draining`` owner row downgrades the mechanism to
        # graceful_stop: the owner reached its own release path, so nothing
        # about a dead owner was proven and the receipt cannot record a pass.
        if kind == "replica-lease-takeover" and result.mechanism == "graceful_stop" and result.outcome == "pass":
            raise _schema_violation()
        if kind == "replica-progress":
            owner_affine = details["owner_affine"]
            if type(owner_affine) is not dict or set(owner_affine) != _PROBE_FIELDS:
                raise _schema_violation()
            _probe_result(
                probe="P4b",
                recorded_probe=owner_affine["probe"],
                outcome=owner_affine["outcome"],
                mechanism=owner_affine["mechanism"],
                reasons=owner_affine["reasons"],
                evidence=owner_affine["evidence"],
            )
        return
    if kind == "verify-doctor-job":
        job_name = _job_execution(
            kind=kind,
            job_name=details["job_name"],
            execution_name=details["execution_name"],
            execution_status=details["execution_status"],
            job_names=DOCTOR_JOB_NAMES,
        )
        _sha256_text(details["report_sha256"])
        checks_ok = details["checks_ok"]
        if (
            type(checks_ok) is not list
            or len(checks_ok) > 64
            or [_text(name, _CHECK_NAME_PATTERN) for name in checks_ok] != sorted(set(checks_ok))
        ):
            raise _schema_violation()
        if not DOCTOR_REQUIRED_CHECKS.issubset(checks_ok):
            raise _schema_violation()
        if _flag(details["init_schema"]) != (job_name == "doctor-schema-init"):
            raise _schema_violation()
    elif kind == "verify-storage-job":
        _job_execution(
            kind=kind,
            job_name=details["job_name"],
            execution_name=details["execution_name"],
            execution_status=details["execution_status"],
            job_names=frozenset({STORAGE_JOB_NAME}),
        )
        directories = details["directories"]
        if type(directories) is not list or tuple(directories) != STORAGE_DIRECTORIES:
            raise _schema_violation()
        _exact(details["owner_uid"], STORAGE_OWNER_ID)
        _exact(details["owner_gid"], STORAGE_OWNER_ID)
        _exact(details["mode"], "0700")
    elif kind == "verify-blob-managed-identity":
        _job_execution(
            kind=kind,
            job_name=details["job_name"],
            execution_name=details["execution_name"],
            execution_status=details["execution_status"],
            job_names=frozenset({BLOB_JOB_NAME}),
        )
        _exact(details["cases_total"], 2)
        _exact(details["cases_passed"], 2)
        _sha256_text(details["blob_sha256"])
        _exact(details["collision_rejected"], True)
        _exact(details["cleanup_succeeded"], True)
    elif kind == "verify-log-analytics":
        _sha256_text(details["workspace_id_sha256"])
        queries = details["queries"]
        if type(queries) is not list or len(queries) != len(KQL_QUERY_NAMES):
            raise _schema_violation()
        if tuple(sorted(_log_query(query) for query in queries)) != KQL_QUERY_NAMES:
            raise _schema_violation()
        _exact(details["canary_tokens_absent"], True)
    elif kind == "verify-connection-budget":
        validate_connection_budget_receipt(details["budget"], schema_id=CONNECTION_BUDGET_SCHEMA)
    elif kind == "compatibility-record":
        _exact(details["schema"], COMPATIBILITY_RECEIPT_SCHEMA)
        _sha256_text(details["record_sha256"])
        _exact(details["scenario_id"], "A")
        _exact(details["gate_passed"], True)
        _exact(details["failed_clauses"], [])
    elif kind == "revision-rollout":
        _text(details["revision_name"], _REVISION_PATTERN)
        _exact(details["active_revisions"], 1)
        _exact(details["traffic_weight"], 100)
        _text(details["image_digest"], _IMAGE_DIGEST_PATTERN)
        _count(details["running_replicas"], minimum=2)
        _exact(details["health_status"], 200)
        _exact(details["ready_status"], 200)
        _exact(details["deployment_target"], DEPLOYMENT_TARGET)
    else:
        _sha256_text(details["resource_group_sha256"])
        _exact(details["remaining_resources"], 0)
        _purge_date(
            purged=details["key_vault_purged"], tombstoned=details["key_vault_tombstoned"], scheduled=details["scheduled_purge_date"]
        )


EXEC_RECEIPT_DESCRIPTOR: Final = ExecReceiptDescriptor(
    provider=PROVIDER,
    subject_field="replica_binding_sha256",
    detail_validators=MappingProxyType({kind: functools.partial(validate_check_details, kind) for kind in sorted(_KIND_FIELDS)}),
)
"""The Container Apps binding of the shared envelope validator: twelve kinds, one subject field, one validator."""


# --------------------------------------------------------------------------- exec receipts


@dataclass(frozen=True, slots=True)
class StoredReceipt:
    """One admitted receipt: its bindings, and the canonical bytes the store hashes, persists and indexes."""

    kind: str
    scenario_id: str
    subject_sha256: str
    candidate_sha: str
    canonical_json: str

    @property
    def receipt_sha256(self) -> str:
        return _sha256(self.canonical_json.encode("utf-8"))


def _admit_exec_receipt(payload: object) -> StoredReceipt:
    """Bounded document, then the shared envelope validator under this provider's descriptor; the subject is the replica binding."""

    if type(payload) is not dict:
        raise AcceptanceCheckError("exec_receipt_schema")
    document = _validate_bounded_receipt_document(payload)
    receipt = validate_exec_receipt_schema(document, descriptor=EXEC_RECEIPT_DESCRIPTOR)
    return StoredReceipt(
        kind=cast(str, receipt["check"]),
        scenario_id=cast(str, receipt["scenario_id"]),
        subject_sha256=cast(str, receipt["replica_binding_sha256"]),
        candidate_sha=cast(str, receipt["candidate_sha"]),
        canonical_json=json.dumps(receipt, sort_keys=True, separators=(",", ":")),
    )


def encode_exec_receipt(check: str, details: CheckDetails, *, candidate_sha: str, binding: ReplicaBinding, scenario_id: str) -> str:
    """Encode one closed receipt line; the binding is hashed, never carried."""

    if _GIT_SHA_PATTERN.fullmatch(candidate_sha) is None or _SCENARIO_ID_PATTERN.fullmatch(scenario_id) is None:
        raise AcceptanceCheckError("exec_receipt_binding")
    receipt = _admit_exec_receipt(
        {
            "version": 1,
            "check": check,
            "ok": True,
            "candidate_sha": candidate_sha,
            "replica_binding_sha256": binding.sha256,
            "scenario_id": scenario_id,
            "details": dict(details),
        }
    )
    encoded = base64.urlsafe_b64encode(receipt.canonical_json.encode("utf-8")).decode("ascii").rstrip("=")
    if len(encoded) > MAX_EXEC_RECEIPT_CHARS:
        raise AcceptanceCheckError("exec_receipt")
    return f"{EXEC_RECEIPT_PREFIX}{encoded}"


@trust_boundary(
    tier=3,
    source="the captured stdout of an in-replica check (az containerapp exec) or of the driver's own verify command, holding one receipt line",
    source_param="stream",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('exec_receipt', 'exec_receipt_schema', or a *_binding check) before use unless the "
        "stream is within the byte bound, carries exactly one prefixed base64url receipt line whose decoded envelope the "
        "shared validator admits under the Container Apps descriptor, and that envelope's candidate sha, replica "
        "binding, scenario and check equal the expected ones; returns only the owned StoredReceipt"
    ),
    test_ref="tests/unit/web/azure_container_apps_acceptance/test_receipt_contracts.py::test_extract_exec_receipt_rejects_malformed_streams_and_wrong_bindings",
    test_fingerprint="21bc2830c1da12e7adb31c0e3b83e64b4aac8913a3f8480c1a6896b4e05fc98f",
)
def extract_exec_receipt(
    stream: str,
    *,
    expected_candidate_sha: str,
    expected_binding: ReplicaBinding,
    expected_scenario_id: str,
    expected_check: str,
) -> StoredReceipt:
    """Extract and bind exactly one closed receipt from captured output."""

    if len(stream.encode("utf-8")) > MAX_EXEC_STREAM_BYTES:
        raise AcceptanceCheckError("exec_receipt")
    receipt_lines = [line for line in stream.splitlines() if line.startswith(EXEC_RECEIPT_PREFIX)]
    if len(receipt_lines) != 1:
        raise AcceptanceCheckError("exec_receipt")
    encoded = receipt_lines[0][len(EXEC_RECEIPT_PREFIX) :]
    if not encoded or len(encoded) > MAX_EXEC_RECEIPT_CHARS or re.fullmatch(r"[A-Za-z0-9_-]+", encoded) is None:
        raise AcceptanceCheckError("exec_receipt")
    try:
        decoded = base64.b64decode(f"{encoded}{'=' * (-len(encoded) % 4)}", altchars=b"-_", validate=True)
        receipt = _admit_exec_receipt(json.loads(decoded))
    except AcceptanceCheckError:
        raise
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
        raise AcceptanceCheckError("exec_receipt") from None
    if receipt.candidate_sha != expected_candidate_sha:
        raise AcceptanceCheckError("candidate_binding")
    if receipt.subject_sha256 != expected_binding.sha256:
        raise AcceptanceCheckError("replica_binding")
    if receipt.scenario_id != expected_scenario_id:
        raise AcceptanceCheckError("scenario_binding")
    if receipt.kind != expected_check:
        raise AcceptanceCheckError("check_binding")
    return receipt


@trust_boundary(
    tier=3,
    source="a receipt document read back from the Container Apps acceptance receipt store on the operator host",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('receipt_store_schema', 'exec_receipt_schema', 'exec_receipt' or "
        "'receipt_store_binding') before use unless the payload is a bounded document that the kind's validator admits — the shared testcontainer-run validator under the azure "
        "schema id, or the Container Apps exec envelope whose check equals the kind — bound to the caller's scenario, "
        "candidate sha and subject; returns only the owned StoredReceipt"
    ),
    test_ref="tests/unit/web/azure_container_apps_acceptance/test_receipt_contracts.py::test_validate_stored_receipt_rejects_foreign_kinds_and_mismatched_bindings",
    test_fingerprint="254a74a12e42bbf61766ef787a18afec1d00dd10534be41498d655b7e97c5898",
)
def validate_stored_receipt(payload: object, *, kind: str, scenario_id: str, subject_sha256: str, candidate_sha: str) -> StoredReceipt:
    """Admit one stored receipt of ``kind`` bound to the run's scenario, candidate and subject."""

    if kind not in STORED_RECEIPT_KINDS:
        raise AcceptanceCheckError("receipt_store_schema")
    document = _validate_bounded_receipt_document(payload)
    if kind == TESTCONTAINER_RUN_RECEIPT_KIND:
        run = validate_testcontainer_run_receipt(
            document, provider=PROVIDER, candidate_sha=candidate_sha, scenario_id=scenario_id, subject_sha256=subject_sha256
        )
        canonical = json.dumps(run, sort_keys=True, separators=(",", ":"))
        return StoredReceipt(
            kind=kind, scenario_id=scenario_id, subject_sha256=subject_sha256, candidate_sha=candidate_sha, canonical_json=canonical
        )
    receipt = _admit_exec_receipt(document)
    if receipt != StoredReceipt(
        kind=kind,
        scenario_id=scenario_id,
        subject_sha256=subject_sha256,
        candidate_sha=candidate_sha,
        canonical_json=receipt.canonical_json,
    ):
        raise AcceptanceCheckError("receipt_store_binding")
    return receipt


# --------------------------------------------------------------------------- compatibility record

_COMPATIBILITY_RECORD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema",
        "record_id",
        "acceptance_run_id",
        "scenario_id",
        "candidate_sha",
        "candidate_image_digest",
        "candidate_revision_sha256",
        "candidate_doctor_job_sha256",
        "candidate_package_version",
        "previous_source_sha",
        "previous_image_digest",
        "previous_revision_sha256",
        "rollback_doctor_job_sha256",
        "previous_package_version",
        "schema_facts",
        "forward_compatible",
        "backward_compatible",
        "rollback_permitted",
        "decision",
        "approver_identity",
        "countersigner_identity",
        "approved_at",
        "countersigned_at",
        "expires_at",
    }
)
"""The runbook record's field set (``docs/runbooks/azure-container-apps-deployment.md``), exactly."""
_SCENARIO_A_EMPTY_FIELDS: Final[tuple[str, ...]] = (
    "previous_source_sha",
    "previous_image_digest",
    "previous_revision_sha256",
    "rollback_doctor_job_sha256",
    "previous_package_version",
)


@dataclass(frozen=True, slots=True)
class CompatibilityRecordBindings:
    """What the driver knows independently of the record and the record must agree with."""

    acceptance_run_id: str
    candidate_sha: str
    candidate_image_digest: str
    candidate_revision_sha256: str
    candidate_doctor_job_sha256: str


@dataclass(frozen=True, slots=True)
class ValidatedCompatibilityRecord:
    record_sha256: str
    scenario_id: str
    expires_at: str


def _record_timestamp(value: object) -> datetime:
    if type(value) is not str:
        raise AcceptanceCheckError("compatibility_record_schema")
    try:
        return _parse_utc_z_timestamp(value)
    except ValueError:
        raise AcceptanceCheckError("compatibility_record_schema") from None


@trust_boundary(
    tier=3,
    source="the operator-authored Scenario A compatibility record read from a protected file on the acceptance host",
    source_param="record",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('compatibility_record_schema', 'compatibility_record_binding' or "
        "'compatibility_record_expired') before use unless the record is a dict with exactly the runbook's field set "
        "under the Container Apps receipt schema, Scenario A with every previous-release field empty, schema_facts equal "
        "to the shared derivation, the driver's bindings, two distinct bounded operator identities, boolean verdicts "
        "of forward true / backward false / rollback false, and approved <= countersigned <= now < expires"
    ),
    test_ref="tests/unit/web/azure_container_apps_acceptance/test_receipt_contracts.py::test_compatibility_record_rejects_open_field_sets_and_foreign_facts",
    test_fingerprint="b0f6298fc323446294e59b5c9ba1886c064587f39e94803779dcae676c653ba6",
)
def validate_compatibility_record(record: object, *, bindings: CompatibilityRecordBindings, now: datetime) -> ValidatedCompatibilityRecord:
    """Admit the Scenario A record the runbook fixes and bind it to what the driver measured."""

    if type(record) is not dict or set(record) != _COMPATIBILITY_RECORD_FIELDS:
        raise AcceptanceCheckError("compatibility_record_schema")
    if record["schema"] != COMPATIBILITY_RECEIPT_SCHEMA or record["scenario_id"] != "A":
        raise AcceptanceCheckError("compatibility_record_schema")
    if any(type(record[field]) is not bool for field in ("forward_compatible", "backward_compatible", "rollback_permitted")):
        raise AcceptanceCheckError("compatibility_record_schema")
    for field in ("record_id", "approver_identity", "countersigner_identity"):
        value = record[field]
        if type(value) is not str or _OPERATOR_IDENTITY_PATTERN.fullmatch(value) is None:
            raise AcceptanceCheckError("compatibility_record_schema")
    if record["approver_identity"] == record["countersigner_identity"]:
        raise AcceptanceCheckError("compatibility_record_schema")
    if (
        record["acceptance_run_id"] != bindings.acceptance_run_id
        or record["candidate_sha"] != bindings.candidate_sha
        or record["candidate_image_digest"] != bindings.candidate_image_digest
        or record["candidate_revision_sha256"] != bindings.candidate_revision_sha256
        or record["candidate_doctor_job_sha256"] != bindings.candidate_doctor_job_sha256
        or record["candidate_package_version"] != _CANDIDATE_PACKAGE_VERSION
        or any(record[field] != "" for field in _SCENARIO_A_EMPTY_FIELDS)
        or record["schema_facts"] != _expected_schema_facts("A")
        or record["decision"] != "approved"
        or record["forward_compatible"] is not True
        or record["backward_compatible"] is not False
        or record["rollback_permitted"] is not False
    ):
        raise AcceptanceCheckError("compatibility_record_binding")
    approved_at = _record_timestamp(record["approved_at"])
    countersigned_at = _record_timestamp(record["countersigned_at"])
    expires_at = _record_timestamp(record["expires_at"])
    if now.tzinfo is None or now.utcoffset() is None or not approved_at <= countersigned_at <= now < expires_at:
        raise AcceptanceCheckError("compatibility_record_expired")
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return ValidatedCompatibilityRecord(record_sha256=_sha256(canonical), scenario_id="A", expires_at=cast(str, record["expires_at"]))
