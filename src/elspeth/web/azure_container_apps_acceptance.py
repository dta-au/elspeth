"""Azure Container Apps acceptance facade: the closed command surface the acceptance driver calls.

Thin over the platform (plan §8.2): ``az``, ``psql``, KQL and the resource
graph are driven by the bash runbook through its protected capture wrappers;
this module validates what they captured, scores the replica probes with the
shared decision tables, encodes receipts bound to a control-plane-verified
replica, and keeps the receipt store. Every failure leaves as one static JSON
envelope on stderr and exit 1; nothing from a platform response, a database
or a credential is ever echoed.

Live database and platform access is injected at exactly two seams —
``SqlSession`` / ``SqlReader`` (SQLAlchemy in autocommit mode, URLs from
``ELSPETH_ACCEPTANCE_PG_*_URL``) and ``PlatformCommands`` (a bounded ``az``
subprocess, argv only, no shell) — so every command is unit-testable against
fakes and no Azure SDK is imported anywhere in the package.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from sqlalchemy import Engine, create_engine, text

from ._acceptance_common.compatibility_gate import compatibility_record_gate
from ._acceptance_common.compatibility_gate import main as _compatibility_gate_main
from ._acceptance_common.errors import (
    ACCEPTANCE_ERROR_CODES,
    AcceptanceCheckError,
    AcceptanceHttpError,
    AcceptanceInputError,
    AcceptanceStateError,
    current_acceptance_step,
    reset_acceptance_step,
)
from ._acceptance_common.http_client import AcceptanceCredentials, AcceptanceHttpClient
from ._acceptance_common.receipt_validation import MAX_EXEC_STREAM_BYTES, _sha256
from ._acceptance_common.replica_probes import (
    DEFAULT_TRIALS,
    ProbeRequest,
    ProbeResult,
    ReplicaAddress,
    ReplicaProbeDriver,
    decide_cross_replica_progress,
    decide_fence_conflict,
    decide_lease_takeover,
    decide_run_start,
    record_owner_affine_progress,
)
from ._acceptance_common.secure_documents import MAX_CONTROL_DOCUMENT_BYTES, _read_protected_document
from ._azure_container_apps_acceptance.controller import (
    INGRESS_REQUEST_TIMEOUT_SECONDS as INGRESS_REQUEST_TIMEOUT_SECONDS,
)
from ._azure_container_apps_acceptance.controller import (
    PROBE_TOPOLOGY as PROBE_TOPOLOGY,
)
from ._azure_container_apps_acceptance.controller import (
    ContainerAppsReplicaController,
    PlatformCommands,
    PostgresEvidenceObserver,
    ProbeReplica,
    RoleRevocationPartition,
    SqlReader,
    SqlSession,
    label_url,
    require_even_label_split,
)
from ._azure_container_apps_acceptance.evidence import (
    BundleVerdict as BundleVerdict,
)
from ._azure_container_apps_acceptance.evidence import (
    blob_managed_identity_details,
    bundle_check,
    connection_budget_details,
    cross_replica_progress_observation,
    doctor_job_details,
    lease_takeover_for_receipt,
    lease_takeover_observation,
    project_active_revisions,
    project_job_execution,
    project_label_weights,
    project_log_analytics_rows,
    project_replica_names,
    project_resource_graph_count,
    receipt_store,
    resource_graph_cleanup_details,
    revision_rollout_details,
    storage_job_details,
)
from ._azure_container_apps_acceptance.receipt_contracts import (
    CHECK_KINDS as CHECK_KINDS,
)
from ._azure_container_apps_acceptance.receipt_contracts import (
    COMPATIBILITY_RECEIPT_SCHEMA as COMPATIBILITY_RECEIPT_SCHEMA,
)
from ._azure_container_apps_acceptance.receipt_contracts import (
    CONNECTION_BUDGET_SCHEMA as CONNECTION_BUDGET_SCHEMA,
)
from ._azure_container_apps_acceptance.receipt_contracts import (
    KQL_QUERY_NAMES,
    STORED_RECEIPT_KINDS,
    CheckDetails,
    CompatibilityRecordBindings,
    CompatibilityRecordDetails,
    LogAnalyticsDetails,
    LogAnalyticsQueryDetails,
    ReplicaBinding,
    ReplicaProgressDetails,
    encode_exec_receipt,
    extract_exec_receipt,
    validate_compatibility_record,
)
from ._azure_container_apps_acceptance.receipt_contracts import (
    MECHANISMS as MECHANISMS,
)

__all__ = [
    "CHECK_KINDS",
    "COMPATIBILITY_RECEIPT_SCHEMA",
    "CONNECTION_BUDGET_SCHEMA",
    "INGRESS_REQUEST_TIMEOUT_SECONDS",
    "MECHANISMS",
    "PLATFORM_COMMAND_TIMEOUT_SECONDS",
    "PROBE_ROLES",
    "PROBE_STATUS_PATH",
    "PROBE_TOPOLOGY",
    "AcceptanceErrorEnvelope",
    "BundleVerdict",
    "acceptance_error_envelope",
    "build_parser",
    "main",
    "probe_topology_check",
]

PLATFORM_COMMAND_TIMEOUT_SECONDS = 120.0
"""One ``az`` call's ceiling; the runbook's ``ELSPETH_AZ_CALL_CEILING_SECONDS`` default."""

PROBE_STATUS_PATH = "/api/system/status"
PROBE_ROLES: Mapping[str, str] = {"a": "elspeth_runtime_a", "b": "elspeth_runtime_b"}
"""Label → runtime role: revision ``a`` runs as ``elspeth_runtime_a`` (bundle ``runtimeRoleLabel``)."""


# --------------------------------------------------------------------------- live seams


class _SqlAlchemySession(SqlSession):
    def __init__(self, engine: Engine) -> None:
        self._connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")

    def execute_scalar(self, statement: str) -> object:
        return self._connection.execute(text(statement)).scalar()

    def close(self) -> None:
        self._connection.close()


class _SqlAlchemyReader(SqlReader):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def scalar(self, statement: str, **parameters: object) -> object:
        with self._engine.connect() as connection:
            return connection.execute(text(statement), parameters).scalar()

    def rows(self, statement: str, **parameters: object) -> tuple[tuple[object, ...], ...]:
        with self._engine.connect() as connection:
            return tuple(tuple(row) for row in connection.execute(text(statement), parameters).all())


class _AzSubprocess(PlatformCommands):
    def run(self, argv: Sequence[str]) -> bytes:
        if not argv or argv[0] != "az":
            raise AcceptanceInputError("only az commands are run through the platform seam")
        try:
            completed = subprocess.run(list(argv), capture_output=True, check=False, timeout=PLATFORM_COMMAND_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            raise AcceptanceCheckError("platform_command") from None
        if completed.returncode != 0:
            raise AcceptanceCheckError("platform_command")
        return completed.stdout


def _required_env(env: Mapping[str, str], name: str) -> str:
    """One required acceptance variable; absence is a named refusal, never a silent default."""

    if name not in env or not env[name]:
        raise AcceptanceCheckError("env_validate", missing=(name,))
    return env[name]


def _engine(env: Mapping[str, str], name: str) -> Engine:
    return create_engine(_required_env(env, name), pool_pre_ping=True)


def _session_factory(engine: Engine) -> Callable[[], SqlSession]:
    def open_session() -> SqlSession:
        return _SqlAlchemySession(engine)

    return open_session


def _partition(env: Mapping[str, str]) -> RoleRevocationPartition:
    admin = _engine(env, "ELSPETH_ACCEPTANCE_PG_ADMIN_URL")
    roles = {role: _engine(env, f"ELSPETH_ACCEPTANCE_PG_RUNTIME_{label.upper()}_URL") for label, role in PROBE_ROLES.items()}
    return RoleRevocationPartition(admin=_session_factory(admin), roles={role: _session_factory(engine) for role, engine in roles.items()})


def _observer(env: Mapping[str, str]) -> PostgresEvidenceObserver:
    return PostgresEvidenceObserver(
        sessions=_SqlAlchemyReader(_engine(env, "ELSPETH_ACCEPTANCE_SESSION_DB_URL")),
        landscape=_SqlAlchemyReader(_engine(env, "ELSPETH_ACCEPTANCE_LANDSCAPE_URL")),
    )


# --------------------------------------------------------------------------- helpers


def _document(path: str) -> object:
    """A protected owner-only JSON file the driver wrote from a capture wrapper."""

    return _read_protected_document(Path(path), check="platform_document_file")


def _list_document(path: str) -> object:
    """Like :func:`_document` but admits a top-level JSON list (``az ... list`` output)."""

    try:
        content = Path(path).read_bytes()
    except OSError:
        raise AcceptanceCheckError("platform_document_file") from None
    if len(content) > MAX_CONTROL_DOCUMENT_BYTES:
        raise AcceptanceCheckError("platform_document_file")
    try:
        return json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise AcceptanceCheckError("platform_document_file") from None


def _timestamp_argument(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AcceptanceInputError("timestamps must be RFC 3339 UTC") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AcceptanceInputError("timestamps must carry a UTC offset")
    return parsed


def _seconds_argument(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise AcceptanceInputError("seconds must be a decimal number") from None
    if seconds != seconds or seconds < 0 or seconds == float("inf"):
        raise AcceptanceInputError("seconds must be finite and non-negative")
    return seconds


def _binding(args: argparse.Namespace) -> ReplicaBinding:
    return ReplicaBinding(container_app_id=args.container_app_id, revision=args.revision, replica=args.replica)


def _receipt_line(args: argparse.Namespace, check: str, details: CheckDetails) -> str:
    return encode_exec_receipt(check, details, candidate_sha=args.candidate_sha, binding=_binding(args), scenario_id=args.scenario_id)


def _print_json(value: object) -> None:
    sys.stdout.write(f"{json.dumps(value, sort_keys=True, separators=(',', ':'))}\n")


def _print_error(value: object) -> None:
    sys.stderr.write(f"{json.dumps(value, sort_keys=True, separators=(',', ':'))}\n")


class AcceptanceErrorEnvelope(TypedDict, total=False):
    """The closed operator envelope: class name, a closed code or static check name, the step; never content."""

    error_class: str
    step: str | None
    check: str
    missing: list[str]
    error_code: str
    status: int


def acceptance_error_envelope(exc: BaseException) -> AcceptanceErrorEnvelope:
    envelope: AcceptanceErrorEnvelope = {"error_class": type(exc).__name__, "step": current_acceptance_step()}
    if isinstance(exc, AcceptanceCheckError):
        envelope["check"] = exc.check
        if exc.missing:
            envelope["missing"] = sorted(exc.missing)
        return envelope
    if isinstance(exc, AcceptanceInputError | AcceptanceHttpError | AcceptanceStateError):
        envelope["error_code"] = exc.error_code if exc.error_code in ACCEPTANCE_ERROR_CODES else "acceptance_internal"
        if type(exc.status) is int:
            envelope["status"] = exc.status
        return envelope
    envelope["error_code"] = "acceptance_internal"
    return envelope


def _probe_pair(
    args: argparse.Namespace, env: Mapping[str, str]
) -> tuple[ContainerAppsReplicaController, Callable[[str], AcceptanceHttpClient]]:
    credentials = AcceptanceCredentials.from_env(env)
    replicas = tuple(
        ProbeReplica(
            address=ReplicaAddress(label, label_url(app_name=args.app_name, label=label, default_domain=args.default_domain)),
            revision=f"{args.app_name}--{args.revision_suffix}-{label}",
            role=PROBE_ROLES[label],
        )
        for label in ("a", "b")
    )
    controller = ContainerAppsReplicaController(
        app_name=args.app_name,
        resource_group=args.resource_group,
        replicas=(replicas[0], replicas[1]),
        partition=_partition(env),
        platform=_AzSubprocess(),
    )

    def client_factory(origin: str) -> AcceptanceHttpClient:
        return AcceptanceHttpClient(origin=origin, credentials=credentials)

    return controller, client_factory


def probe_topology_check(
    controller: ContainerAppsReplicaController, client_factory: Callable[[str], AcceptanceHttpClient], *, traffic: object
) -> tuple[str, str]:
    """Before any trial: 50/50 on the two labels, and each label URL answers with its own instance id (the header proves the route)."""

    first, second = controller.replicas()
    require_even_label_split(project_label_weights(traffic), labels=(first.name, second.name))
    instances: list[str] = []
    for address in (first, second):
        _status, instance_id, _body = client_factory(address.origin).request_json_with_instance(
            "GET", PROBE_STATUS_PATH, expected_statuses={200}
        )
        if instance_id is None:
            raise AcceptanceCheckError("probe_topology", missing=("X-Elspeth-Instance",))
        instances.append(instance_id)
    if instances[0] == instances[1]:
        raise AcceptanceCheckError("probe_topology")
    return instances[0], instances[1]


def _probe_verdict(result: ProbeResult) -> int:
    """Exit 0 iff the probe passed, or is P4b (recorded, cannot pass); an unreachable P3 is not waived."""

    return 0 if result.outcome == "pass" or result.probe == "P4b" else 1


# --------------------------------------------------------------------------- parser


def _add_binding_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--scenario-id", default="A")
    parser.add_argument("--container-app-id", required=True, help="ARM id from `az containerapp show --query id`")
    parser.add_argument("--revision", required=True, help="CONTAINER_APP_REVISION of the replica the evidence was collected against")
    parser.add_argument("--replica", required=True, help="CONTAINER_APP_REPLICA_NAME, cross-checked against `az containerapp replica list`")


def build_parser() -> argparse.ArgumentParser:
    """Build the closed command surface the runbook and ``scripts/acceptance.sh`` call."""

    parser = argparse.ArgumentParser(prog="python -m elspeth.web.azure_container_apps_acceptance")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("verify-doctor-job")
    _add_binding_arguments(doctor)
    doctor.add_argument("--job-name", required=True, choices=("doctor-schema-init", "doctor-runtime-a", "doctor-runtime-b"))
    doctor.add_argument("--execution", required=True, help="`az containerapp job execution show` JSON")
    doctor.add_argument("--report", required=True, help="the `--json` report retrieved from Log Analytics")

    storage = commands.add_parser("verify-storage-job")
    _add_binding_arguments(storage)
    storage.add_argument("--execution", required=True)
    storage.add_argument("--owner-uid", required=True, type=int)
    storage.add_argument("--owner-gid", required=True, type=int)
    storage.add_argument("--mode", required=True)

    blob = commands.add_parser("verify-blob-managed-identity")
    _add_binding_arguments(blob)
    blob.add_argument("--execution", required=True)
    blob.add_argument("--report", required=True)

    logs = commands.add_parser("verify-log-analytics")
    _add_binding_arguments(logs)
    logs.add_argument("--workspace-id", required=True)
    logs.add_argument(
        "--query",
        required=True,
        action="append",
        nargs=4,
        metavar=("NAME", "ROWS_FILE", "KQL_FILE", "INGESTION_LAG_SECONDS_MAX"),
        help="one checked-in KQL file, its captured rows, and max(ingestion_time() - TimeGenerated) over them in seconds",
    )

    budget = commands.add_parser("verify-connection-budget")
    _add_binding_arguments(budget)
    budget.add_argument("--metrics", required=True, help="`az monitor metrics list` JSON")
    budget.add_argument("--window-start", required=True)
    budget.add_argument("--acceptance-run-id", required=True)
    budget.add_argument("--server-id", required=True)
    budget.add_argument("--max-connections", required=True, type=int)
    budget.add_argument("--approved-budget", required=True, type=int)
    budget.add_argument("--safety-margin", required=True, type=int)

    rollout = commands.add_parser("revision-rollout")
    _add_binding_arguments(rollout)
    rollout.add_argument("--revisions", required=True, help='`az containerapp revision list --query "[?properties.active]..."` JSON')
    rollout.add_argument("--replicas", required=True, help="`az containerapp replica list` JSON")
    rollout.add_argument("--image-digest", required=True)
    rollout.add_argument("--health-status", required=True, type=int)
    rollout.add_argument("--ready-status", required=True, type=int)

    record = commands.add_parser("compatibility-record-validate")
    _add_binding_arguments(record)
    record.add_argument("--record", required=True)
    record.add_argument("--acceptance-run-id", required=True)
    record.add_argument("--candidate-image-digest", required=True)
    record.add_argument("--candidate-revision-sha256", required=True)
    record.add_argument("--candidate-doctor-job-sha256", required=True)

    gate = commands.add_parser("compatibility-record-gate")
    gate.add_argument("--record", required=True)
    gate.add_argument("--scenario-id", required=True, choices=("A", "B"))

    cleanup = commands.add_parser("resource-graph-cleanup-validate")
    _add_binding_arguments(cleanup)
    cleanup.add_argument("--count", required=True, help="`az graph query` JSON")
    cleanup.add_argument("--resource-group", required=True)
    vault = cleanup.add_mutually_exclusive_group(required=True)
    vault.add_argument("--key-vault-purged", action="store_true")
    vault.add_argument("--scheduled-purge-date")

    probes = commands.add_parser("replica-probes")
    _add_binding_arguments(probes)
    probes.add_argument("--probe", required=True, choices=("fence-conflict", "run-start", "lease-takeover", "progress"))
    probes.add_argument("--app-name", default="elspeth-web")
    probes.add_argument("--resource-group")
    probes.add_argument("--default-domain")
    probes.add_argument("--revision-suffix")
    probes.add_argument("--traffic", help="`az containerapp ingress traffic show` JSON")
    probes.add_argument("--session-id")
    probes.add_argument("--body", help="JSON request body file for the guided respond pair")
    probes.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    probes.add_argument("--observation", help="P3 / P4a observation document assembled by the driver")

    partition = commands.add_parser("partition-owner")
    partition.add_argument("--role", required=True, choices=tuple(PROBE_ROLES.values()))
    restore = commands.add_parser("restore-owner")
    restore.add_argument("--role", required=True, choices=tuple(PROBE_ROLES.values()))

    store = commands.add_parser("receipt-store")
    store.add_argument("--store-dir", required=True)
    store.add_argument("--kind", required=True, choices=sorted(STORED_RECEIPT_KINDS))
    store.add_argument("--scenario-id", default="A")
    store.add_argument("--subject-id", required=True, help="the replica binding subject, or the junit sha256 for testcontainer-run")
    store.add_argument("--candidate-sha", required=True)
    source = store.add_mutually_exclusive_group(required=True)
    source.add_argument("--receipt-file")
    source.add_argument("--receipt-stdin", action="store_true")

    bundle = commands.add_parser("bundle-validate")
    bundle.add_argument("--store-dir", required=True)
    bundle.add_argument("--candidate-sha", required=True)
    bundle.add_argument("--scenario-id", default="A")

    extract = commands.add_parser("extract-exec-receipt")
    _add_binding_arguments(extract)
    extract.add_argument("--check", required=True, choices=sorted(CHECK_KINDS))
    return parser


# --------------------------------------------------------------------------- dispatch


def _run_probe(args: argparse.Namespace, env: Mapping[str, str]) -> tuple[str, ProbeResult, CheckDetails]:
    if args.probe in {"fence-conflict", "run-start"}:
        if None in (args.resource_group, args.default_domain, args.revision_suffix, args.traffic, args.session_id):
            raise AcceptanceInputError(
                "--resource-group, --default-domain, --revision-suffix, --traffic and --session-id are required for a live probe"
            )
        controller, client_factory = _probe_pair(args, env)
        probe_topology_check(controller, client_factory, traffic=_list_document(args.traffic))
        driver = ReplicaProbeDriver(controller=controller, observer=_observer(env), client_factory=client_factory)
        if args.probe == "fence-conflict":
            if args.body is None:
                raise AcceptanceInputError("--body is required for the guided respond pair")
            request = ProbeRequest("POST", f"/api/sessions/{args.session_id}/guided/respond", _document(args.body))
            trials = [driver.fence_conflict_trial(args.session_id, request) for _ in range(args.trials)]
            result = decide_fence_conflict(trials, required_trials=args.trials)
            return "replica-fence-conflict", result, result.to_receipt_details()
        request = ProbeRequest("POST", f"/api/sessions/{args.session_id}/execute", {})
        run_trials = [driver.run_start_trial(args.session_id, request) for _ in range(args.trials)]
        result = decide_run_start(run_trials, required_trials=args.trials)
        return "replica-run-start", result, result.to_receipt_details()
    if args.observation is None:
        raise AcceptanceInputError("--observation is required for lease-takeover and progress")
    if args.probe == "lease-takeover":
        result = lease_takeover_for_receipt(decide_lease_takeover(lease_takeover_observation(_document(args.observation))))
        return "replica-lease-takeover", result, result.to_receipt_details()
    result = decide_cross_replica_progress(cross_replica_progress_observation(_document(args.observation)))
    owner_affine = record_owner_affine_progress(mitigation="single_revision_sticky_sessions")
    progress: ReplicaProgressDetails = {
        "probe": result.probe,
        "outcome": result.outcome,
        "mechanism": result.mechanism,
        "reasons": list(result.reasons),
        "evidence": dict(result.evidence),
        "owner_affine": owner_affine.to_receipt_details(),
    }
    return "replica-progress", result, progress


def _dispatch(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    if args.command == "verify-doctor-job":
        execution = project_job_execution(_document(args.execution))
        report = Path(args.report).read_bytes()
        sys.stdout.write(
            f"{_receipt_line(args, 'verify-doctor-job', doctor_job_details(report, execution=execution, job_name=args.job_name))}\n"
        )
    elif args.command == "verify-storage-job":
        details = storage_job_details(
            project_job_execution(_document(args.execution)), owner_uid=args.owner_uid, owner_gid=args.owner_gid, mode=args.mode
        )
        sys.stdout.write(f"{_receipt_line(args, 'verify-storage-job', details)}\n")
    elif args.command == "verify-blob-managed-identity":
        blob_details = blob_managed_identity_details(_document(args.report), execution=project_job_execution(_document(args.execution)))
        sys.stdout.write(f"{_receipt_line(args, 'verify-blob-managed-identity', blob_details)}\n")
    elif args.command == "verify-log-analytics":
        canary = _required_env(env, "ELSPETH_ACCEPTANCE_CANARY_TOKEN")
        queries: list[LogAnalyticsQueryDetails] = []
        canary_absent = True
        for name, rows_file, kql_file, lag in args.query:
            if name not in KQL_QUERY_NAMES:
                raise AcceptanceInputError("unknown KQL query name")
            rows = project_log_analytics_rows(_list_document(rows_file), canary_token=canary)
            canary_absent = canary_absent and rows.canary_absent
            queries.append(
                {
                    "name": name,
                    "query_sha256": _sha256(Path(kql_file).read_bytes()),
                    "row_count": rows.row_count,
                    "ingestion_lag_seconds_max": _seconds_argument(lag),
                }
            )
        log_details: LogAnalyticsDetails = {
            "mechanism": "log_analytics_query",
            "workspace_id_sha256": _sha256(args.workspace_id.encode("utf-8")),
            "queries": queries,
            "canary_tokens_absent": canary_absent,
        }
        sys.stdout.write(f"{_receipt_line(args, 'verify-log-analytics', log_details)}\n")
    elif args.command == "verify-connection-budget":
        budget_details = connection_budget_details(
            _document(args.metrics),
            window_start=_timestamp_argument(args.window_start),
            acceptance_run_id=args.acceptance_run_id,
            server_id=args.server_id,
            max_connections=args.max_connections,
            approved_budget=args.approved_budget,
            safety_margin=args.safety_margin,
        )
        sys.stdout.write(f"{_receipt_line(args, 'verify-connection-budget', budget_details)}\n")
    elif args.command == "revision-rollout":
        replicas = project_replica_names(_list_document(args.replicas))
        if args.replica not in replicas:
            raise AcceptanceCheckError("replica_binding")
        rollout_details = revision_rollout_details(
            project_active_revisions(_list_document(args.revisions)),
            expected_revision=args.revision,
            image_digest=args.image_digest,
            running_replicas=len(replicas),
            health_status=args.health_status,
            ready_status=args.ready_status,
        )
        sys.stdout.write(f"{_receipt_line(args, 'revision-rollout', rollout_details)}\n")
    elif args.command == "compatibility-record-validate":
        record = _document(args.record)
        validated = validate_compatibility_record(
            record,
            bindings=CompatibilityRecordBindings(
                acceptance_run_id=args.acceptance_run_id,
                candidate_sha=args.candidate_sha,
                candidate_image_digest=args.candidate_image_digest,
                candidate_revision_sha256=args.candidate_revision_sha256,
                candidate_doctor_job_sha256=args.candidate_doctor_job_sha256,
            ),
            now=datetime.now(UTC),
        )
        gate_verdict = compatibility_record_gate(record, scenario_id="A")
        record_details: CompatibilityRecordDetails = {
            "mechanism": "operator_record",
            "schema": COMPATIBILITY_RECEIPT_SCHEMA,
            "record_sha256": validated.record_sha256,
            "scenario_id": validated.scenario_id,
            "gate_passed": gate_verdict.passed,
            "failed_clauses": list(gate_verdict.failed_clauses),
        }
        sys.stdout.write(f"{_receipt_line(args, 'compatibility-record', record_details)}\n")
    elif args.command == "compatibility-record-gate":
        return _compatibility_gate_main(["--record", args.record, "--scenario-id", args.scenario_id])
    elif args.command == "resource-graph-cleanup-validate":
        cleanup_details = resource_graph_cleanup_details(
            resource_group=args.resource_group,
            remaining_resources=project_resource_graph_count(_document(args.count)),
            key_vault_purged=args.key_vault_purged,
            scheduled_purge_date=None if args.scheduled_purge_date is None else _timestamp_argument(args.scheduled_purge_date),
        )
        sys.stdout.write(f"{_receipt_line(args, 'resource-graph-cleanup', cleanup_details)}\n")
    elif args.command == "replica-probes":
        check, result, probe_details = _run_probe(args, env)
        sys.stdout.write(f"{_receipt_line(args, check, probe_details)}\n")
        return _probe_verdict(result)
    elif args.command == "partition-owner":
        record = _partition(env).partition(args.role)
        _print_json({"role": record.role, "terminated_backends": record.terminated_backends})
    elif args.command == "restore-owner":
        _partition(env).restore(args.role)
        _print_json({"role": args.role, "restored": True})
    elif args.command == "receipt-store":
        if args.receipt_stdin:
            content = sys.stdin.buffer.read(MAX_CONTROL_DOCUMENT_BYTES + 1)
            if len(content) > MAX_CONTROL_DOCUMENT_BYTES:
                raise AcceptanceCheckError("receipt_store_file")
            try:
                document: object = json.loads(content)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise AcceptanceCheckError("receipt_store_schema") from None
        else:
            document = _document(args.receipt_file)
        stored = receipt_store(
            Path(args.store_dir),
            kind=args.kind,
            scenario_id=args.scenario_id,
            subject_id=args.subject_id,
            candidate_sha=args.candidate_sha,
            document=document,
            now=datetime.now(UTC),
        )
        sys.stdout.write(f"{stored.receipt_sha256}\n")
    elif args.command == "bundle-validate":
        verdict = bundle_check(Path(args.store_dir), candidate_sha=args.candidate_sha, scenario_id=args.scenario_id)
        _print_json(
            {
                "passed": verdict.passed,
                "missing_kinds": list(verdict.missing_kinds),
                "invalid_receipts": [{"receipt_sha256": item.receipt_sha256, "check": item.check} for item in verdict.invalid_receipts],
                "failed_probes": list(verdict.failed_probes),
                "testcontainer_reason": verdict.testcontainer_reason,
                "testcontainer_receipt_sha256": verdict.testcontainer_receipt_sha256,
            }
        )
        return 0 if verdict.passed else 1
    else:
        stream = sys.stdin.read(MAX_EXEC_STREAM_BYTES + 1)
        receipt = extract_exec_receipt(
            stream,
            expected_candidate_sha=args.candidate_sha,
            expected_binding=_binding(args),
            expected_scenario_id=args.scenario_id,
            expected_check=args.check,
        )
        sys.stdout.write(f"{receipt.canonical_json}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Dispatch the closed command surface with static failures."""

    args = build_parser().parse_args(argv)
    reset_acceptance_step()
    try:
        return _dispatch(args, os.environ)
    except (AcceptanceCheckError, AcceptanceHttpError, AcceptanceInputError, AcceptanceStateError) as exc:
        _print_error(acceptance_error_envelope(exc))
        return 1
    except Exception:
        _print_error({"error_class": "AcceptanceInternalError", "error_code": "acceptance_internal", "step": current_acceptance_step()})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
