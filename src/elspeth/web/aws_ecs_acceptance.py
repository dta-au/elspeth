"""Sanitized AWS ECS operator-telemetry acceptance coordination.

The live AWS adapters are deliberately injected.  This coordinator owns the
ordering, bounded retry, and evidence projection contracts shared by the
in-task acceptance command: Landscape first, operational telemetry second.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from ._aws_ecs_acceptance.approvals import (
    _require_current_approval,
)
from ._aws_ecs_acceptance.approvals import (
    approval_require_current as approval_require_current,
)
from ._aws_ecs_acceptance.approvals import (
    approval_verify as approval_verify,
)
from ._aws_ecs_acceptance.bedrock import (
    _suppress_process_output,
)
from ._aws_ecs_acceptance.bedrock import (
    build_plugin_policy_acceptance as build_plugin_policy_acceptance,
)
from ._aws_ecs_acceptance.bedrock import (
    run_bedrock_guardrails_live as run_bedrock_guardrails_live,
)
from ._aws_ecs_acceptance.bedrock import (
    verify_bedrock as verify_bedrock,
)
from ._aws_ecs_acceptance.bedrock import (
    verify_bedrock_guardrails as verify_bedrock_guardrails,
)
from ._aws_ecs_acceptance.capture import (
    FIXED_INPUT_BYTES as FIXED_INPUT_BYTES,
)
from ._aws_ecs_acceptance.capture import (
    TUTORIAL_INPUT_BYTES as TUTORIAL_INPUT_BYTES,
)
from ._aws_ecs_acceptance.capture import (
    build_canonical_tutorial_pipeline_yaml as build_canonical_tutorial_pipeline_yaml,
)
from ._aws_ecs_acceptance.capture import (
    build_fixed_pipeline_yaml as build_fixed_pipeline_yaml,
)
from ._aws_ecs_acceptance.capture import (
    capture,
    provision_storage,
    verify_api,
    verify_local_auth,
    verify_payloads,
)
from ._aws_ecs_acceptance.contracts import (
    _EMERGENCY_CLEANUP_SECONDS,
    _EVIDENCE_KINDS,
    _GIT_SHA_PATTERN,
    _SHA256_PATTERN,
    MAX_CONTROL_DOCUMENT_BYTES,
    MAX_EXEC_STREAM_BYTES,
    MAX_JSON_RESPONSE_BYTES,
    AcceptanceCheckError,
    AcceptanceHttpError,
    AcceptanceInputError,
    AcceptanceStateError,
    OperatorTelemetryAcceptanceError,
    _control_timestamp,
    _sha256,
    _utc_timestamp,
    scenario_resource_namespace,
)
from ._aws_ecs_acceptance.contracts import (
    CONNECT_TIMEOUT_SECONDS as CONNECT_TIMEOUT_SECONDS,
)
from ._aws_ecs_acceptance.contracts import (
    EVIDENCE_KINDS as EVIDENCE_KINDS,
)
from ._aws_ecs_acceptance.contracts import (
    FORBIDDEN_AWS_OVERRIDE_ENV as FORBIDDEN_AWS_OVERRIDE_ENV,
)
from ._aws_ecs_acceptance.contracts import (
    MAX_BLOB_RESPONSE_BYTES as MAX_BLOB_RESPONSE_BYTES,
)
from ._aws_ecs_acceptance.contracts import (
    MAX_EXEC_RECEIPT_CHARS as MAX_EXEC_RECEIPT_CHARS,
)
from ._aws_ecs_acceptance.contracts import (
    MAX_STATE_FILE_BYTES as MAX_STATE_FILE_BYTES,
)
from ._aws_ecs_acceptance.contracts import (
    POOL_TIMEOUT_SECONDS as POOL_TIMEOUT_SECONDS,
)
from ._aws_ecs_acceptance.contracts import (
    READ_TIMEOUT_SECONDS as READ_TIMEOUT_SECONDS,
)
from ._aws_ecs_acceptance.contracts import (
    RUN_POLL_DEADLINE_SECONDS as RUN_POLL_DEADLINE_SECONDS,
)
from ._aws_ecs_acceptance.contracts import (
    RUN_POLL_INTERVAL_SECONDS as RUN_POLL_INTERVAL_SECONDS,
)
from ._aws_ecs_acceptance.contracts import (
    WRITE_TIMEOUT_SECONDS as WRITE_TIMEOUT_SECONDS,
)
from ._aws_ecs_acceptance.contracts import (
    SanitizedResourceIdentity as SanitizedResourceIdentity,
)
from ._aws_ecs_acceptance.contracts import (
    normalize_acceptance_origin as normalize_acceptance_origin,
)
from ._aws_ecs_acceptance.contracts import (
    plugin_policy_binding_sha256 as plugin_policy_binding_sha256,
)
from ._aws_ecs_acceptance.http_client import AcceptanceHttpClient as AcceptanceHttpClient
from ._aws_ecs_acceptance.manifest import (
    control_manifest_bind_retained_evidence as control_manifest_bind_retained_evidence,
)
from ._aws_ecs_acceptance.manifest import (
    control_manifest_bind_scenario as control_manifest_bind_scenario,
)
from ._aws_ecs_acceptance.manifest import (
    control_manifest_checkpoint_operator_evidence as control_manifest_checkpoint_operator_evidence,
)
from ._aws_ecs_acceptance.manifest import control_manifest_get as control_manifest_get
from ._aws_ecs_acceptance.manifest import control_manifest_init as control_manifest_init
from ._aws_ecs_acceptance.manifest_schema import (
    CLEANUP_SURFACES as CLEANUP_SURFACES,
)
from ._aws_ecs_acceptance.manifest_schema import (
    _load_retained_evidence as _load_retained_evidence,
)
from ._aws_ecs_acceptance.manifest_schema import (
    _read_control_manifest,
    _require_mutable_control_manifest,
    _validate_control_manifest,
)
from ._aws_ecs_acceptance.manifest_schema import (
    _validate_retained_evidence_receipt as _validate_retained_evidence_receipt,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    AcceptancePolicy as AcceptancePolicy,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    AuditSentinel as AuditSentinel,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    AWSOperatorMetricEmitter as AWSOperatorMetricEmitter,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    AWSOperatorTelemetryQueries as AWSOperatorTelemetryQueries,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    ExistingLandscapeLifecycleAudit as ExistingLandscapeLifecycleAudit,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    OperatorTelemetryEvidence as OperatorTelemetryEvidence,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    OperatorTelemetryOutageEvidence as OperatorTelemetryOutageEvidence,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    PublicApiLifecycleAudit as PublicApiLifecycleAudit,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    TelemetryQueries as TelemetryQueries,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    TelemetrySentinelEmitter as TelemetrySentinelEmitter,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    operator_metric_dimensions as operator_metric_dimensions,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    verify_connection_budget_live as verify_connection_budget_live,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    verify_operator_telemetry as verify_operator_telemetry,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    verify_operator_telemetry_live as verify_operator_telemetry_live,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    verify_operator_telemetry_outage as verify_operator_telemetry_outage,
)
from ._aws_ecs_acceptance.operator_telemetry import (
    xray_trace_id as xray_trace_id,
)
from ._aws_ecs_acceptance.orphan_sweep import (
    OrphanSweepClients as OrphanSweepClients,
)
from ._aws_ecs_acceptance.orphan_sweep import (
    _transaction_search_projection as _transaction_search_projection,
)
from ._aws_ecs_acceptance.orphan_sweep import orphan_sweep as orphan_sweep
from ._aws_ecs_acceptance.receipt_contracts import (
    _CANDIDATE_PACKAGE_VERSION,
    _ROLLBACK_PACKAGE_VERSION,
    _expected_schema_facts,
    _validate_stored_receipt,
    encode_exec_receipt,
    extract_exec_receipt,
    resolve_exec_receipt_env,
)
from ._aws_ecs_acceptance.receipt_store import receipt_store as receipt_store
from ._aws_ecs_acceptance.s3 import verify_s3 as verify_s3
from ._aws_ecs_acceptance.scenario_inventory import (
    PLUGIN_POLICY_ASSIGNMENT_NAMES as PLUGIN_POLICY_ASSIGNMENT_NAMES,
)
from ._aws_ecs_acceptance.scenario_inventory import (
    SCENARIO_ASSIGNMENT_NAMES as SCENARIO_ASSIGNMENT_NAMES,
)
from ._aws_ecs_acceptance.scenario_inventory import (
    _control_path,
    _load_bound_scenario_inventory,
)
from ._aws_ecs_acceptance.scenario_inventory import (
    _load_preapply_scenario_inventory as _load_preapply_scenario_inventory,
)
from ._aws_ecs_acceptance.scenario_inventory import (
    _scenario_inventory_hash as _scenario_inventory_hash,
)
from ._aws_ecs_acceptance.scenario_inventory import (
    _validate_scenario_inventory as _validate_scenario_inventory,
)
from ._aws_ecs_acceptance.scenario_inventory import (
    _validate_scenario_inventory_isolation as _validate_scenario_inventory_isolation,
)
from ._aws_ecs_acceptance.scenario_inventory import (
    _validate_tf_binding_receipt as _validate_tf_binding_receipt,
)
from ._aws_ecs_acceptance.secure_documents import (
    _read_protected_document,
    _serialized_control_manifest_write,
    _write_protected_document,
)
from ._aws_ecs_acceptance.state import (
    AcceptanceCredentials as AcceptanceCredentials,
)
from ._aws_ecs_acceptance.state import (
    AcceptanceState as AcceptanceState,
)
from ._aws_ecs_acceptance.state import (
    _parse_state_timestamp,
)
from ._aws_ecs_acceptance.state import (
    read_acceptance_state as read_acceptance_state,
)
from ._aws_ecs_acceptance.state import (
    write_acceptance_state as write_acceptance_state,
)
from ._aws_ecs_acceptance.task_definition import (
    validate_task_definition_policy_binding as validate_task_definition_policy_binding,
)

_GATE_LEDGER_FIELDS = frozenset(
    {
        "schema",
        "branch",
        "starting_sha",
        "plan_sha256",
        "program_base_sha",
        "reconciled_release_sha",
        "candidate_sha",
        "candidate_bound_record_count",
        "records",
        "cleanup_records",
        "success_record_count_at_cleanup_start",
        "finalized",
        "created_at",
        "updated_at",
    }
)
_GATE_RECORD_FIELDS = frozenset({"check_id", "candidate_sha", "started_at", "ended_at", "exit_status", "receipt_hash"})
_SUCCESS_GATE_CHECK_ORDER = ("candidate", "static", "tests", "image", "live")
_CLEANUP_GATE_CHECK_ORDER = ("cleanup",)
_REQUIRED_GATE_CHECK_ORDER = (*_SUCCESS_GATE_CHECK_ORDER, *_CLEANUP_GATE_CHECK_ORDER)
_REQUIRED_GATE_CHECK_IDS = frozenset(_REQUIRED_GATE_CHECK_ORDER)
_TASK1_GATE_CHECK_ORDER = ("candidate",)
_GATE_LEDGER_GET_FIELDS = frozenset(
    {"branch", "starting_sha", "plan_sha256", "program_base_sha", "reconciled_release_sha", "candidate_sha"}
)
_TERMINAL_GATE_CHECK_ID = "cleanup"
_APPLICATION_SCENARIO_IDS = frozenset({"A", "B"})
# Facts pinned to the Scenario B rollback baseline (the 0.7.0 image), and the
# behaviour-named structural migration from that baseline to the current
# landscape epoch. The label is deliberately a literal: it must be rewritten —
# not silently renumbered — whenever SQLITE_SCHEMA_EPOCH moves, and
# test_compatibility_schema_facts_track_current_epochs enforces that.


def control_manifest_validate(
    path: Path,
    *,
    acceptance_run_id: str | None = None,
    candidate_sha: str | None = None,
    cleanup_only: bool = False,
    require_cleanup_cleared: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    manifest = _read_control_manifest(path)
    current = now()
    if (acceptance_run_id is not None and manifest["acceptance_run_id"] != acceptance_run_id) or (
        candidate_sha is not None and manifest["candidate_sha"] != candidate_sha
    ):
        raise AcceptanceCheckError("control_manifest_binding")
    expired = current >= _control_timestamp(manifest["teardown_deadline_utc"])
    if expired and not (cleanup_only and require_cleanup_cleared):
        raise AcceptanceCheckError("control_manifest_expired")
    if require_cleanup_cleared:
        if not cleanup_only or manifest["cleanup_required"] is not False:
            raise AcceptanceCheckError("control_manifest_cleanup")
        cleanup_states = manifest["cleanup_states"]
        assert isinstance(cleanup_states, dict)
        for surface, state in cleanup_states.items():
            if state == "confirmed":
                continue
            if surface == "teardown_deadline" and state == "failed" and manifest["deadline_failure_recorded"] is True:
                continue
            raise AcceptanceCheckError("control_manifest_cleanup")
        if expired and manifest["deadline_failure_recorded"] is not True:
            final_evidence = manifest["final_evidence"]
            if (
                not isinstance(final_evidence, Mapping)
                or final_evidence.get("phase") != "committed"
                or _control_timestamp(final_evidence.get("committed_at")) > _control_timestamp(manifest["teardown_deadline_utc"])
            ):
                raise AcceptanceCheckError("control_manifest_cleanup")
        _verify_final_cleanup_receipt(path, manifest)
    return manifest


def _validate_evidence_export_receipt(
    path: Path,
    *,
    manifest: Mapping[str, object],
    receipts_sha256: str,
    ledger_records_sha256: str,
) -> tuple[dict[str, object], str]:
    receipt = _read_protected_document(path, check="evidence_export_receipt")
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema",
        "acceptance_run_id",
        "destination_sha256",
        "receipts_sha256",
        "ledger_records_sha256",
        "artifact_count",
        "exported_at",
        "verified",
    }:
        raise AcceptanceCheckError("evidence_export_schema")
    evidence = manifest["evidence"]
    assert isinstance(evidence, Mapping)
    if (
        receipt["schema"] != "elspeth.aws-ecs-evidence-export.v1"
        or receipt["acceptance_run_id"] != manifest["acceptance_run_id"]
        or receipt["destination_sha256"] != evidence["destination_sha256"]
        or receipt["receipts_sha256"] != receipts_sha256
        or receipt["ledger_records_sha256"] != ledger_records_sha256
        or receipt["verified"] is not True
        or type(receipt["artifact_count"]) is not int
        or receipt["artifact_count"] < 1
    ):
        raise AcceptanceCheckError("evidence_export_binding")
    _control_timestamp(receipt["exported_at"])
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return receipt, _sha256(canonical)


def _reverify_bound_evidence_export_receipt(
    path: Path,
    *,
    manifest: Mapping[str, object],
    expected_sha256: str,
) -> None:
    document = _read_protected_document(path, check="evidence_export_receipt")
    receipts_sha256 = document.get("receipts_sha256")
    ledger_records_sha256 = document.get("ledger_records_sha256")
    if (
        type(receipts_sha256) is not str
        or _SHA256_PATTERN.fullmatch(receipts_sha256) is None
        or type(ledger_records_sha256) is not str
        or _SHA256_PATTERN.fullmatch(ledger_records_sha256) is None
    ):
        raise AcceptanceCheckError("evidence_export_schema")
    _receipt, observed_sha256 = _validate_evidence_export_receipt(
        path,
        manifest=manifest,
        receipts_sha256=receipts_sha256,
        ledger_records_sha256=ledger_records_sha256,
    )
    if observed_sha256 != expected_sha256:
        raise AcceptanceCheckError("evidence_export_binding")


def create_evidence_export_receipt(
    manifest_path: Path,
    *,
    ledger_path: Path,
    output_path: Path,
    artifact_count: int,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Record an evidence owner's completed and verified external export."""

    if type(artifact_count) is not int or not 1 <= artifact_count <= 100_000:
        raise AcceptanceCheckError("evidence_export_schema")
    manifest = _read_control_manifest(manifest_path)
    ledger = _read_gate_ledger(ledger_path)
    if ledger.get("candidate_sha") != manifest["candidate_sha"]:
        raise AcceptanceCheckError("evidence_export_binding")
    evidence_record_count, receipts_sha256 = _verify_stored_receipts(manifest_path, manifest)
    if artifact_count < max(1, evidence_record_count):
        raise AcceptanceCheckError("evidence_export_binding")
    receipt = {
        "schema": "elspeth.aws-ecs-evidence-export.v1",
        "acceptance_run_id": manifest["acceptance_run_id"],
        "destination_sha256": cast(Mapping[str, object], manifest["evidence"])["destination_sha256"],
        "receipts_sha256": receipts_sha256,
        "ledger_records_sha256": _gate_ledger_records_hash(ledger),
        "artifact_count": artifact_count,
        "exported_at": _utc_timestamp(now()),
        "verified": True,
    }
    _write_protected_document(
        output_path,
        receipt,
        create=True,
        exists_check="evidence_export_receipt",
        write_check="evidence_export_receipt",
        parent_check="evidence_export_receipt",
    )
    _validate_evidence_export_receipt(
        output_path,
        manifest=manifest,
        receipts_sha256=receipts_sha256,
        ledger_records_sha256=cast(str, receipt["ledger_records_sha256"]),
    )
    return receipt


@_serialized_control_manifest_write
def control_manifest_update(
    path: Path,
    *,
    cleanup_required: bool | None = None,
    ecr_baseline_tag: str | None = None,
    ecr_candidate_tag: str | None = None,
    ecr_registry: str | None = None,
    ecr_repository: str | None = None,
    ecr_baseline_digest: str | None = None,
    ecr_candidate_digest: str | None = None,
    acceptance_state_path: str | None = None,
    oidc_evidence_dir: str | None = None,
    evidence_export_receipt: str | None = None,
    final_evidence_export_receipt: str | None = None,
    terraform_plan_receipt: str | None = None,
    terraform_applied: str | None = None,
    terraform_noop_receipt: str | None = None,
    cleanup_checkpoint: str | None = None,
    verdict_failure: str | None = None,
    emergency_cleanup_deadline_utc: str | None = None,
    cleanup_escalation: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    manifest = _read_control_manifest(path)
    _require_mutable_control_manifest(manifest)
    current = now()
    tag_shape = (
        cleanup_required is True
        and evidence_export_receipt is None
        and final_evidence_export_receipt is None
        and all(value is not None for value in (ecr_baseline_tag, ecr_candidate_tag, ecr_registry, ecr_repository))
        and all(
            value is None
            for value in (
                ecr_baseline_digest,
                ecr_candidate_digest,
                acceptance_state_path,
                oidc_evidence_dir,
                terraform_plan_receipt,
                terraform_applied,
                terraform_noop_receipt,
                cleanup_checkpoint,
                verdict_failure,
                emergency_cleanup_deadline_utc,
                cleanup_escalation,
            )
        )
    )
    digest_shape = (
        ecr_baseline_digest is not None
        and ecr_candidate_digest is not None
        and evidence_export_receipt is None
        and final_evidence_export_receipt is None
        and all(
            value is None
            for value in (
                cleanup_required,
                ecr_baseline_tag,
                ecr_candidate_tag,
                ecr_registry,
                ecr_repository,
                acceptance_state_path,
                oidc_evidence_dir,
                terraform_plan_receipt,
                terraform_applied,
                terraform_noop_receipt,
                cleanup_checkpoint,
                verdict_failure,
                emergency_cleanup_deadline_utc,
                cleanup_escalation,
            )
        )
    )
    evidence_path_shape = (
        (acceptance_state_path is not None) != (oidc_evidence_dir is not None)
        and evidence_export_receipt is None
        and final_evidence_export_receipt is None
        and all(
            value is None
            for value in (
                cleanup_required,
                ecr_baseline_tag,
                ecr_candidate_tag,
                ecr_registry,
                ecr_repository,
                ecr_baseline_digest,
                ecr_candidate_digest,
                terraform_plan_receipt,
                terraform_applied,
                terraform_noop_receipt,
                cleanup_checkpoint,
                verdict_failure,
                emergency_cleanup_deadline_utc,
                cleanup_escalation,
            )
        )
    )
    terraform_shape = (
        sum(value is not None for value in (terraform_plan_receipt, terraform_applied, terraform_noop_receipt)) == 1
        and evidence_export_receipt is None
        and final_evidence_export_receipt is None
        and all(
            value is None
            for value in (
                cleanup_required,
                ecr_baseline_tag,
                ecr_candidate_tag,
                ecr_registry,
                ecr_repository,
                ecr_baseline_digest,
                ecr_candidate_digest,
                acceptance_state_path,
                oidc_evidence_dir,
                cleanup_checkpoint,
                verdict_failure,
                emergency_cleanup_deadline_utc,
                cleanup_escalation,
            )
        )
    )
    checkpoint_shape = (
        cleanup_checkpoint is not None
        and evidence_export_receipt is None
        and final_evidence_export_receipt is None
        and all(
            value is None
            for value in (
                cleanup_required,
                ecr_baseline_tag,
                ecr_candidate_tag,
                ecr_registry,
                ecr_repository,
                ecr_baseline_digest,
                ecr_candidate_digest,
                acceptance_state_path,
                oidc_evidence_dir,
                terraform_plan_receipt,
                terraform_applied,
                terraform_noop_receipt,
                verdict_failure,
                emergency_cleanup_deadline_utc,
                cleanup_escalation,
            )
        )
    )
    verdict_shape = (
        verdict_failure == "teardown_deadline"
        and evidence_export_receipt is None
        and final_evidence_export_receipt is None
        and (emergency_cleanup_deadline_utc is None) == (cleanup_escalation is None)
        and all(
            value is None
            for value in (
                cleanup_required,
                ecr_baseline_tag,
                ecr_candidate_tag,
                ecr_registry,
                ecr_repository,
                ecr_baseline_digest,
                ecr_candidate_digest,
                acceptance_state_path,
                oidc_evidence_dir,
                terraform_plan_receipt,
                terraform_applied,
                terraform_noop_receipt,
                cleanup_checkpoint,
            )
        )
    )
    export_shape = (
        evidence_export_receipt is not None
        and final_evidence_export_receipt is None
        and all(
            value is None
            for value in (
                cleanup_required,
                ecr_baseline_tag,
                ecr_candidate_tag,
                ecr_registry,
                ecr_repository,
                ecr_baseline_digest,
                ecr_candidate_digest,
                acceptance_state_path,
                oidc_evidence_dir,
                terraform_plan_receipt,
                terraform_applied,
                terraform_noop_receipt,
                cleanup_checkpoint,
                verdict_failure,
                emergency_cleanup_deadline_utc,
                cleanup_escalation,
            )
        )
    )
    final_export_shape = (
        final_evidence_export_receipt is not None
        and evidence_export_receipt is None
        and all(
            value is None
            for value in (
                cleanup_required,
                ecr_baseline_tag,
                ecr_candidate_tag,
                ecr_registry,
                ecr_repository,
                ecr_baseline_digest,
                ecr_candidate_digest,
                acceptance_state_path,
                oidc_evidence_dir,
                terraform_plan_receipt,
                terraform_applied,
                terraform_noop_receipt,
                cleanup_checkpoint,
                verdict_failure,
                emergency_cleanup_deadline_utc,
                cleanup_escalation,
            )
        )
    )
    if (
        sum(
            (
                tag_shape,
                digest_shape,
                evidence_path_shape,
                terraform_shape,
                checkpoint_shape,
                verdict_shape,
                export_shape,
                final_export_shape,
            )
        )
        != 1
    ):
        raise AcceptanceCheckError("control_manifest_update")
    cleanup_mutation = any(
        value is not None
        for value in (
            cleanup_checkpoint,
            verdict_failure,
            emergency_cleanup_deadline_utc,
            cleanup_escalation,
            evidence_export_receipt,
            final_evidence_export_receipt,
        )
    )
    expired = current >= _control_timestamp(manifest["teardown_deadline_utc"])
    acceptance_mutation = any(
        value is not None
        for value in (
            cleanup_required,
            ecr_baseline_tag,
            ecr_candidate_tag,
            ecr_registry,
            ecr_repository,
            ecr_baseline_digest,
            ecr_candidate_digest,
            acceptance_state_path,
            oidc_evidence_dir,
            terraform_plan_receipt,
            terraform_applied,
            terraform_noop_receipt,
        )
    )
    if expired and acceptance_mutation:
        raise AcceptanceCheckError("control_manifest_expired")
    if not cleanup_mutation and not acceptance_mutation:
        raise AcceptanceCheckError("control_manifest_update")
    if cleanup_required is not None:
        if cleanup_required is False:
            raise AcceptanceCheckError("control_manifest_cleanup")
        manifest["cleanup_required"] = True
    ecr = manifest["ecr"]
    assert isinstance(ecr, dict)
    for field, value in (
        ("baseline_tag", ecr_baseline_tag),
        ("candidate_tag", ecr_candidate_tag),
        ("registry", ecr_registry),
        ("repository", ecr_repository),
        ("baseline_digest", ecr_baseline_digest),
        ("candidate_digest", ecr_candidate_digest),
    ):
        if value is not None:
            if ecr[field] is not None and ecr[field] != value:
                raise AcceptanceCheckError("control_manifest_conflict")
            ecr[field] = value
    evidence = manifest["evidence"]
    assert isinstance(evidence, dict)
    if acceptance_state_path is not None:
        if evidence["acceptance_state_path"] is not None and evidence["acceptance_state_path"] != acceptance_state_path:
            raise AcceptanceCheckError("control_manifest_conflict")
        evidence["acceptance_state_path"] = acceptance_state_path
    if oidc_evidence_dir is not None:
        if evidence["oidc_evidence_dir"] is not None and evidence["oidc_evidence_dir"] != oidc_evidence_dir:
            raise AcceptanceCheckError("control_manifest_conflict")
        evidence["oidc_evidence_dir"] = oidc_evidence_dir
    if evidence_export_receipt is not None:
        export_path = Path(_control_path(evidence_export_receipt))
        existing_export_path = evidence["export_receipt_path"]
        existing_export_sha256 = evidence["export_receipt_sha256"]
        if existing_export_path is not None:
            if existing_export_path != evidence_export_receipt or type(existing_export_sha256) is not str:
                raise AcceptanceCheckError("control_manifest_conflict")
            _reverify_bound_evidence_export_receipt(
                export_path,
                manifest=manifest,
                expected_sha256=existing_export_sha256,
            )
        else:
            _receipt_count, receipts_sha256 = _verify_stored_receipts(path, manifest)
            ledger = _read_gate_ledger(Path(cast(str, manifest["gate_ledger_path"])))
            ledger_records_sha256 = _gate_ledger_records_hash(ledger)
            _receipt, export_sha256 = _validate_evidence_export_receipt(
                export_path,
                manifest=manifest,
                receipts_sha256=receipts_sha256,
                ledger_records_sha256=ledger_records_sha256,
            )
            evidence["export_receipt_path"] = evidence_export_receipt
            evidence["export_receipt_sha256"] = export_sha256
    if final_evidence_export_receipt is not None:
        initial_export_path = evidence["export_receipt_path"]
        initial_export_sha256 = evidence["export_receipt_sha256"]
        if type(initial_export_path) is not str or type(initial_export_sha256) is not str:
            raise AcceptanceCheckError("control_manifest_update")
        if final_evidence_export_receipt == initial_export_path:
            raise AcceptanceCheckError("control_manifest_conflict")
        _reverify_bound_evidence_export_receipt(
            Path(initial_export_path),
            manifest=manifest,
            expected_sha256=initial_export_sha256,
        )
        export_path = Path(_control_path(final_evidence_export_receipt))
        _receipt_count, receipts_sha256 = _verify_stored_receipts(path, manifest)
        ledger = _read_gate_ledger(Path(cast(str, manifest["gate_ledger_path"])))
        ledger_records_sha256 = _gate_ledger_records_hash(ledger)
        _receipt, export_sha256 = _validate_evidence_export_receipt(
            export_path,
            manifest=manifest,
            receipts_sha256=receipts_sha256,
            ledger_records_sha256=ledger_records_sha256,
        )
        if manifest["final_evidence"] is not None and (
            evidence["final_export_receipt_path"] != final_evidence_export_receipt
            or evidence["final_export_receipt_sha256"] != export_sha256
        ):
            raise AcceptanceCheckError("control_manifest_conflict")
        evidence["final_export_receipt_path"] = final_evidence_export_receipt
        evidence["final_export_receipt_sha256"] = export_sha256
    scenarios = manifest["scenarios"]
    assert isinstance(scenarios, dict)
    if terraform_plan_receipt is not None:
        parts = terraform_plan_receipt.split(":")
        if len(parts) != 4 or parts[0] not in {"A", "B"} or any(_SHA256_PATTERN.fullmatch(value) is None for value in parts[1:]):
            raise AcceptanceCheckError("control_manifest_update")
        scenario = scenarios[parts[0]]
        assert isinstance(scenario, dict)
        receipts = evidence["receipts"]
        approvals = evidence["approvals"]
        assert isinstance(receipts, list) and isinstance(approvals, list)
        subject_sha256 = _sha256(parts[1].encode("utf-8"))
        approval_matches = [
            approval
            for approval in approvals
            if isinstance(approval, dict)
            and approval.get("scenario_id") == parts[0]
            and approval.get("kind") == "terraform-plan"
            and approval.get("plan_receipt_sha256") == parts[2]
            and approval.get("approval_sha256") == parts[3]
        ]
        if (
            not any(
                isinstance(receipt, dict)
                and receipt.get("scenario_id") == parts[0]
                and receipt.get("kind") == "terraform-plan"
                and receipt.get("subject_sha256") == subject_sha256
                and receipt.get("receipt_sha256") == parts[2]
                for receipt in receipts
            )
            or len(approval_matches) != 1
        ):
            raise AcceptanceCheckError("control_manifest_update")
        _require_current_approval(
            cast(list[object], approvals),
            scenario_id=parts[0],
            kind="terraform-plan",
            plan_receipt_sha256=parts[2],
            approval_sha256=parts[3],
            current=current,
        )
        receipt_value = ":".join(parts[1:])
        if scenario["terraform_plan_receipt"] is not None and scenario["terraform_plan_receipt"] != receipt_value:
            raise AcceptanceCheckError("control_manifest_conflict")
        scenario["terraform_plan_receipt"] = receipt_value
    if terraform_applied is not None:
        parts = terraform_applied.split(":")
        if len(parts) != 4 or parts[0] not in {"A", "B"} or any(_SHA256_PATTERN.fullmatch(value) is None for value in parts[1:]):
            raise AcceptanceCheckError("control_manifest_update")
        scenario = scenarios[parts[0]]
        assert isinstance(scenario, dict)
        if scenario["terraform_plan_receipt"] != ":".join(parts[1:]):
            raise AcceptanceCheckError("control_manifest_update")
        approvals = evidence["approvals"]
        assert isinstance(approvals, list)
        _require_current_approval(
            cast(list[object], approvals),
            scenario_id=parts[0],
            kind="terraform-plan",
            plan_receipt_sha256=parts[2],
            approval_sha256=parts[3],
            current=current,
        )
        scenario["terraform_applied"] = True
    if terraform_noop_receipt is not None:
        parts = terraform_noop_receipt.split(":")
        if len(parts) != 3 or parts[0] not in {"A", "B"} or any(_SHA256_PATTERN.fullmatch(value) is None for value in parts[1:]):
            raise AcceptanceCheckError("control_manifest_update")
        scenario = scenarios[parts[0]]
        assert isinstance(scenario, dict)
        if scenario["terraform_applied"] is not True:
            raise AcceptanceCheckError("control_manifest_update")
        receipts = evidence["receipts"]
        assert isinstance(receipts, list)
        if not any(
            isinstance(receipt, dict)
            and receipt.get("scenario_id") == parts[0]
            and receipt.get("kind") == "terraform-noop"
            and receipt.get("subject_sha256") == _sha256(parts[1].encode("utf-8"))
            and receipt.get("receipt_sha256") == parts[2]
            for receipt in receipts
        ):
            raise AcceptanceCheckError("control_manifest_update")
        receipt_value = ":".join(parts[1:])
        if scenario["terraform_noop_receipt"] is not None and scenario["terraform_noop_receipt"] != receipt_value:
            raise AcceptanceCheckError("control_manifest_conflict")
        scenario["terraform_noop_receipt"] = receipt_value
    if cleanup_checkpoint is not None:
        try:
            surface, state_value = cleanup_checkpoint.split(":", 1)
        except ValueError:
            raise AcceptanceCheckError("control_manifest_update") from None
        cleanup_states = manifest["cleanup_states"]
        assert isinstance(cleanup_states, dict)
        if surface not in cleanup_states or state_value not in {"pending", "confirmed", "failed", "interrupted"}:
            raise AcceptanceCheckError("control_manifest_update")
        if cleanup_states[surface] == "confirmed" and state_value != "confirmed":
            raise AcceptanceCheckError("control_manifest_update")
        cleanup_states[surface] = state_value
    if verdict_failure is not None:
        failures = manifest["verdict_failures"]
        assert isinstance(failures, list)
        if verdict_failure not in failures:
            failures.append(verdict_failure)
        if verdict_failure == "teardown_deadline":
            manifest["deadline_failure_recorded"] = True
    if emergency_cleanup_deadline_utc is not None:
        existing_emergency = manifest["emergency_cleanup_deadline_utc"]
        if existing_emergency is not None and existing_emergency != emergency_cleanup_deadline_utc:
            raise AcceptanceCheckError("control_manifest_conflict")
        manifest["emergency_cleanup_deadline_utc"] = emergency_cleanup_deadline_utc
    if cleanup_escalation is not None:
        escalations = manifest["cleanup_escalations"]
        assert isinstance(escalations, list)
        if cleanup_escalation not in escalations:
            escalations.append(cleanup_escalation)
    manifest["updated_at"] = _utc_timestamp(current)
    _validate_control_manifest(manifest)
    _write_protected_document(
        path,
        manifest,
        create=False,
        exists_check="control_manifest_exists",
        write_check="control_manifest_file",
    )
    return manifest


def control_manifest_load_cleanup(
    path: Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    manifest = _read_control_manifest(path)
    current = now()
    expired = current >= _control_timestamp(manifest["teardown_deadline_utc"])
    cleanup_committed = (
        manifest["cleanup_required"] is False
        and isinstance(manifest["final_evidence"], Mapping)
        and manifest["final_evidence"].get("phase") == "committed"
    )
    if (
        expired
        and not cleanup_committed
        and (manifest["deadline_failure_recorded"] is not True or manifest["emergency_cleanup_deadline_utc"] is None)
    ):
        emergency_deadline = manifest["emergency_cleanup_deadline_utc"]
        if emergency_deadline is None:
            emergency_deadline = _utc_timestamp(current + timedelta(seconds=_EMERGENCY_CLEANUP_SECONDS))
        assert isinstance(emergency_deadline, str)
        control_manifest_update(
            path,
            verdict_failure="teardown_deadline",
            emergency_cleanup_deadline_utc=emergency_deadline,
            cleanup_escalation="teardown_deadline",
            now=lambda: current,
        )
        manifest = _read_control_manifest(path)
    aws = manifest["aws"]
    scenarios = manifest["scenarios"]
    ecr = manifest["ecr"]
    evidence = manifest["evidence"]
    assert isinstance(aws, dict) and isinstance(scenarios, dict) and isinstance(ecr, dict) and isinstance(evidence, dict)
    scenario_a = scenarios["A"]
    scenario_b = scenarios["B"]
    assert isinstance(scenario_a, dict) and isinstance(scenario_b, dict)
    inventory_a = _load_bound_scenario_inventory(manifest, "A")
    inventory_b = _load_bound_scenario_inventory(manifest, "B")
    values_a = inventory_a["values"]
    values_b = inventory_b["values"]
    assert isinstance(values_a, dict) and isinstance(values_b, dict)
    assignments: dict[str, object] = {
        "ACCEPTANCE_REENTRY_FORBIDDEN": 1 if expired else 0,
        "ACCEPTANCE_RUN_ID": manifest["acceptance_run_id"],
        "ACCEPTANCE_TEARDOWN_DEADLINE_UTC": manifest["teardown_deadline_utc"],
        "AWS_ACCOUNT_ID": aws["account_id"],
        "AWS_REGION": aws["region"],
        "CANDIDATE_SHA": manifest["candidate_sha"],
        "CANDIDATE_TAG": ecr["candidate_tag"] or "",
        "CLEANUP_REQUIRED": 1 if manifest["cleanup_required"] else 0,
        "DEADLINE_EXPIRED": 1 if expired else 0,
        "ELSPETH_CLEANUP_MODE": 1,
        "ECR_REGISTRY": ecr["registry"] or "",
        "ECR_REPOSITORY": ecr["repository"] or "",
        "EMERGENCY_CLEANUP_DEADLINE_UTC": manifest["emergency_cleanup_deadline_utc"] or "",
        "GATE_LEDGER": manifest["gate_ledger_path"],
        "ROLLBACK_BASELINE_TAG": ecr["baseline_tag"] or "",
        "ROLLBACK_BASELINE_DIGEST": ecr["baseline_digest"] or "",
        "IMAGE_DIGEST": ecr["candidate_digest"] or "",
        "ROLLBACK_BASELINE_IMAGE": (
            f"{ecr['registry']}/{ecr['repository']}@{ecr['baseline_digest']}"
            if ecr["registry"] and ecr["repository"] and ecr["baseline_digest"]
            else ""
        ),
        "CANDIDATE_IMAGE": (
            f"{ecr['registry']}/{ecr['repository']}@{ecr['candidate_digest']}"
            if ecr["registry"] and ecr["repository"] and ecr["candidate_digest"]
            else ""
        ),
        "ACCEPTANCE_STATE": evidence["acceptance_state_path"] or "",
        "OIDC_EVIDENCE_DIR": evidence["oidc_evidence_dir"] or "",
        "EVIDENCE_DESTINATION_SHA256": evidence["destination_sha256"],
        "EVIDENCE_EXPORT_RECEIPT": evidence["export_receipt_path"] or "",
        "FINAL_EVIDENCE_EXPORT_RECEIPT": evidence["final_export_receipt_path"] or "",
        "SCENARIO_A_INVENTORY": scenario_a["inventory_path"],
        "SCENARIO_A_TF_DIR": values_a["SCENARIO_TF_DIR"],
        "SCENARIO_A_TF_VARS": values_a["SCENARIO_TF_VARS"],
        "SCENARIO_A_TF_BINDING_SHA": scenario_a["tf_binding_sha256"],
        "SCENARIO_A_TF_BINDING_FILE": scenario_a["tf_binding_path"],
        "SCENARIO_B_INVENTORY": scenario_b["inventory_path"],
        "SCENARIO_B_TF_DIR": values_b["SCENARIO_TF_DIR"],
        "SCENARIO_B_TF_VARS": values_b["SCENARIO_TF_VARS"],
        "SCENARIO_B_TF_BINDING_SHA": scenario_b["tf_binding_sha256"],
        "SCENARIO_B_TF_BINDING_FILE": scenario_b["tf_binding_path"],
    }
    return "\n".join(f"{name}={shlex.quote(str(value))}" for name, value in assignments.items()) + "\n"


def scenario_load(
    path: Path,
    *,
    scenario_id: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    """Render one bound scenario inventory as a closed shell assignment set."""

    if scenario_id not in {"A", "B"}:
        raise AcceptanceCheckError("scenario_inventory_binding")
    manifest = control_manifest_validate(path, now=now)
    inventory = _load_bound_scenario_inventory(manifest, scenario_id, require_resolved=True)
    values = inventory["values"]
    assert isinstance(values, dict)
    assignments = {
        "ACTIVE_SCENARIO_ID": scenario_id,
        "ACCEPTANCE_RUN_ID": manifest["acceptance_run_id"],
        **{name: values[name] for name in SCENARIO_ASSIGNMENT_NAMES[2:]},
    }
    return "\n".join(f"{name}={shlex.quote(str(assignments[name]))}" for name in SCENARIO_ASSIGNMENT_NAMES) + "\n"


def validate_compatibility_record(
    record_path: Path,
    *,
    manifest_path: Path,
    scenario_id: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Validate the release/schema authority bound to one resolved scenario."""

    if scenario_id not in _APPLICATION_SCENARIO_IDS:
        raise AcceptanceCheckError("compatibility_record_binding")
    manifest = _read_control_manifest(manifest_path)
    inventory = _load_bound_scenario_inventory(manifest, scenario_id, require_resolved=True)
    values = inventory["values"]
    ecr = manifest["ecr"]
    assert isinstance(values, dict) and isinstance(ecr, dict)
    record = _read_protected_document(record_path, check="compatibility_record_file")
    fields = {
        "schema",
        "record_id",
        "acceptance_run_id",
        "scenario_id",
        "candidate_sha",
        "candidate_image_digest",
        "candidate_task_definition",
        "candidate_doctor_task_definition",
        "candidate_package_version",
        "previous_source_sha",
        "previous_image_digest",
        "previous_task_definition",
        "rollback_doctor_task_definition",
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
    if set(record) != fields or record["schema"] != "elspeth.aws-ecs-compatibility-record.v2":
        raise AcceptanceCheckError("compatibility_record_schema")
    previous = values["PREVIOUS_TASK_DEFINITION"] if scenario_id == "B" else ""
    rollback_doctor = values["ROLLBACK_DOCTOR_TASK_DEFINITION"] if scenario_id == "B" else ""
    previous_digest = ecr["baseline_digest"] if scenario_id == "B" else ""
    baseline_tag = ecr["baseline_tag"] if scenario_id == "B" else ""
    baseline_match = re.search(r"baseline-([0-9a-f]{40})$", cast(str, baseline_tag)) if baseline_tag else None
    previous_source_sha = baseline_match.group(1) if baseline_match is not None else ""
    expected_schema_facts = _expected_schema_facts(scenario_id)
    if (
        record["acceptance_run_id"] != manifest["acceptance_run_id"]
        or record["scenario_id"] != scenario_id
        or record["candidate_sha"] != manifest["candidate_sha"]
        or record["candidate_image_digest"] != ecr["candidate_digest"]
        or record["candidate_task_definition"] != values["CANDIDATE_TASK_DEFINITION"]
        or record["candidate_doctor_task_definition"] != values["DOCTOR_TASK_DEFINITION"]
        or record["candidate_package_version"] != _CANDIDATE_PACKAGE_VERSION
        or record["previous_source_sha"] != previous_source_sha
        or record["previous_image_digest"] != previous_digest
        or record["previous_task_definition"] != previous
        or record["rollback_doctor_task_definition"] != rollback_doctor
        or record["previous_package_version"] != (_ROLLBACK_PACKAGE_VERSION if scenario_id == "B" else "")
        or record["schema_facts"] != expected_schema_facts
        or record["decision"] != "approved"
        or record["forward_compatible"] is not True
        or record["backward_compatible"] is not False
        or record["rollback_permitted"] is not False
    ):
        raise AcceptanceCheckError("compatibility_record_binding")
    if any(type(record[field]) is not bool for field in ("forward_compatible", "backward_compatible", "rollback_permitted")):
        raise AcceptanceCheckError("compatibility_record_schema")
    for field in ("record_id", "approver_identity", "countersigner_identity"):
        value = record[field]
        if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}", value) is None:
            raise AcceptanceCheckError("compatibility_record_schema")
    if record["approver_identity"] == record["countersigner_identity"]:
        raise AcceptanceCheckError("compatibility_record_schema")
    approved_at = _control_timestamp(record["approved_at"])
    countersigned_at = _control_timestamp(record["countersigned_at"])
    expires_at = _control_timestamp(record["expires_at"])
    current = now()
    if current.tzinfo is None or current.utcoffset() is None or not approved_at <= countersigned_at <= current < expires_at:
        raise AcceptanceCheckError("compatibility_record_expired")
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema": "elspeth.aws-ecs-compatibility-receipt.v2",
        "record_sha256": _sha256(canonical),
        "acceptance_run_id_sha256": _sha256(cast(str, manifest["acceptance_run_id"]).encode()),
        "scenario_id": scenario_id,
        "candidate_sha": manifest["candidate_sha"],
        "candidate_image_digest": ecr["candidate_digest"],
        "candidate_task_definition_sha256": _sha256(cast(str, values["CANDIDATE_TASK_DEFINITION"]).encode()),
        "candidate_doctor_task_definition_sha256": _sha256(cast(str, values["DOCTOR_TASK_DEFINITION"]).encode()),
        "candidate_package_version": _CANDIDATE_PACKAGE_VERSION,
        "previous_source_sha": previous_source_sha or None,
        "previous_image_digest": previous_digest or None,
        "previous_task_definition_sha256": _sha256(cast(str, previous).encode()) if previous else None,
        "rollback_doctor_task_definition_sha256": _sha256(cast(str, rollback_doctor).encode()) if rollback_doctor else None,
        "previous_package_version": _ROLLBACK_PACKAGE_VERSION if scenario_id == "B" else None,
        "schema_facts": expected_schema_facts,
        "forward_compatible": record["forward_compatible"],
        "backward_compatible": record["backward_compatible"],
        "rollback_permitted": record["rollback_permitted"],
        "decision": "approved",
        "approvals_present": True,
        "expires_at": _utc_timestamp(expires_at),
    }


_LOG_PROJECTION_FIELDS = (
    "event_name",
    "check",
    "class_name",
    "severity",
    "status",
    "outcome",
    "task_revision",
    "deployment_revision",
    "count",
    "ok",
)


def _safe_projection_value(field: str, value: object) -> object | None:
    if field in {"count", "task_revision", "deployment_revision"}:
        return value if type(value) is int and 0 <= value <= 2**63 - 1 else None
    if field == "ok":
        return value if type(value) is bool else None
    if type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value) is not None and "://" not in value:
        return value
    return None


def _project_log_record(record: Mapping[str, object], *, timestamp: object | None = None) -> dict[str, object]:
    projected: dict[str, object] = {}
    candidate_timestamp = timestamp if timestamp is not None else record.get("timestamp")
    if type(candidate_timestamp) is int and candidate_timestamp >= 0:
        projected["timestamp"] = candidate_timestamp
    elif type(candidate_timestamp) is str:
        try:
            _parse_state_timestamp(candidate_timestamp)
        except AcceptanceStateError:
            pass
        else:
            projected["timestamp"] = candidate_timestamp
    for field in _LOG_PROJECTION_FIELDS:
        value = _safe_projection_value(field, record.get(field))
        if value is not None:
            projected[field] = value
    return projected


def sanitize_evidence(kind: str, payload: object) -> dict[str, object]:
    """Project raw diagnostic JSON into one closed, content-free evidence schema."""

    if kind not in _EVIDENCE_KINDS or not isinstance(payload, dict):
        raise AcceptanceCheckError("sanitize_evidence_schema")
    base: dict[str, object] = {
        "schema": "elspeth.aws-ecs-sanitized-evidence.v1",
        "kind": kind,
    }
    if kind in {"web-log", "doctor-log"}:
        events = payload.get("events")
        if not isinstance(events, list) or len(events) > 10_000:
            raise AcceptanceCheckError("sanitize_evidence_schema")
        records: list[dict[str, object]] = []
        for event in events:
            if not isinstance(event, Mapping):
                raise AcceptanceCheckError("sanitize_evidence_schema")
            message = event.get("message")
            if isinstance(message, str) and len(message.encode("utf-8")) <= MAX_JSON_RESPONSE_BYTES:
                try:
                    decoded = json.loads(message)
                except json.JSONDecodeError:
                    decoded = None
            elif isinstance(message, Mapping):
                decoded = message
            else:
                decoded = None
            source = decoded if isinstance(decoded, Mapping) else event
            projected = _project_log_record(source, timestamp=event.get("timestamp"))
            if projected:
                records.append(projected)
        return {
            **base,
            "records": records,
            "counts": {"input": len(events), "projected": len(records)},
        }
    if kind == "deployment-event":
        detail = payload.get("detail", payload)
        if not isinstance(detail, Mapping):
            raise AcceptanceCheckError("sanitize_evidence_schema")
        projected = _project_log_record(detail, timestamp=payload.get("time"))
        return {**base, "records": [projected] if projected else [], "counts": {"input": 1, "projected": bool(projected)}}
    if kind == "task-definition":
        task = payload.get("taskDefinition")
        if not isinstance(task, Mapping):
            raise AcceptanceCheckError("sanitize_evidence_schema")
        revision = task.get("revision")
        network_mode = task.get("networkMode")
        containers = task.get("containerDefinitions")
        volumes = task.get("volumes")
        compatibilities = task.get("requiresCompatibilities")
        if (
            type(revision) is not int
            or revision < 1
            or network_mode not in {"awsvpc", "bridge", "host", "none"}
            or not isinstance(containers, list)
            or not isinstance(volumes, list)
            or not isinstance(compatibilities, list)
        ):
            raise AcceptanceCheckError("sanitize_evidence_schema")
        return {
            **base,
            "projection": {
                "revision": revision,
                "network_mode": network_mode,
                "container_count": len(containers),
                "volume_count": len(volumes),
                "fargate_required": "FARGATE" in compatibilities,
            },
        }
    changes = payload.get("resource_changes")
    if not isinstance(changes, list) or len(changes) > 100_000:
        raise AcceptanceCheckError("sanitize_evidence_schema")
    counts = {"create": 0, "update": 0, "delete": 0, "replace": 0, "no-op": 0}
    for resource_change in changes:
        if not isinstance(resource_change, Mapping):
            raise AcceptanceCheckError("sanitize_evidence_schema")
        change = resource_change.get("change")
        actions = change.get("actions") if isinstance(change, Mapping) else None
        if not isinstance(actions, list) or any(action not in {"create", "update", "delete", "no-op", "read"} for action in actions):
            raise AcceptanceCheckError("sanitize_evidence_schema")
        if set(actions) == {"create", "delete"}:
            counts["replace"] += 1
        elif actions == ["create"]:
            counts["create"] += 1
        elif actions == ["update"]:
            counts["update"] += 1
        elif actions == ["delete"]:
            counts["delete"] += 1
        elif actions == ["no-op"]:
            counts["no-op"] += 1
    return {
        **base,
        "projection": {
            "resource_change_count": len(changes),
            "create_count": counts["create"],
            "update_count": counts["update"],
            "delete_count": counts["delete"],
            "replace_count": counts["replace"],
            "no_op_count": counts["no-op"],
            "has_delete": counts["delete"] > 0,
            "has_replace": counts["replace"] > 0,
        },
    }


def _verify_stored_receipts(manifest_path: Path, manifest: Mapping[str, object]) -> tuple[int, str]:
    evidence = manifest["evidence"]
    assert isinstance(evidence, dict)
    receipts = evidence["receipts"]
    assert isinstance(receipts, list)
    candidate_sha = manifest["candidate_sha"]
    assert isinstance(candidate_sha, str)
    receipt_directory = manifest_path.parent / f"{manifest_path.name}.receipts"
    for record in receipts:
        assert isinstance(record, dict)
        receipt_hash = record["receipt_sha256"]
        scenario_id = record["scenario_id"]
        kind = record["kind"]
        subject_sha256 = record["subject_sha256"]
        assert isinstance(receipt_hash, str)
        assert isinstance(scenario_id, str) and isinstance(kind, str) and isinstance(subject_sha256, str)
        document = _validate_stored_receipt(
            _read_protected_document(receipt_directory / f"{receipt_hash}.json", check="cleanup_finalize_receipt"),
            kind=kind,
            scenario_id=scenario_id,
            subject_sha256=subject_sha256,
            candidate_sha=candidate_sha,
        )
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if _sha256(canonical) != receipt_hash:
            raise AcceptanceCheckError("cleanup_finalize_receipt")
    approvals = evidence["approvals"]
    assert isinstance(approvals, list)
    evidence_records = {"receipts": receipts, "approvals": approvals}
    return len(receipts) + len(approvals), _sha256(json.dumps(evidence_records, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _final_cleanup_receipt_document(
    manifest_path: Path,
    manifest: Mapping[str, object],
    *,
    ledger_sha256: str,
    receipts_sha256: str,
    committed_at: str,
) -> dict[str, object]:
    manifest_sha256 = _sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return {
        "schema": "elspeth.aws-ecs-final-cleanup-receipt.v1",
        "manifest_sha256": manifest_sha256,
        "ledger_sha256": ledger_sha256,
        "receipts_sha256": receipts_sha256,
        "committed_at": committed_at,
    }


def _verify_final_cleanup_receipt(manifest_path: Path, manifest: Mapping[str, object]) -> None:
    final_evidence = manifest["final_evidence"]
    if not isinstance(final_evidence, Mapping) or final_evidence.get("phase") != "committed":
        raise AcceptanceCheckError("cleanup_finalize_receipt")
    ledger = _read_gate_ledger(Path(cast(str, manifest["gate_ledger_path"])))
    ledger_sha256 = _gate_ledger_records_hash(ledger)
    _receipt_count, receipts_sha256 = _verify_stored_receipts(manifest_path, manifest)
    committed_at = final_evidence["committed_at"]
    if type(committed_at) is not str:
        raise AcceptanceCheckError("cleanup_finalize_receipt")
    expected = _final_cleanup_receipt_document(
        manifest_path,
        manifest,
        ledger_sha256=ledger_sha256,
        receipts_sha256=receipts_sha256,
        committed_at=committed_at,
    )
    final_receipt_path = manifest_path.with_name(f"{manifest_path.name}.final-receipt.json")
    if _read_protected_document(final_receipt_path, check="cleanup_finalize_receipt") != expected:
        raise AcceptanceCheckError("cleanup_finalize_receipt")


def _ensure_final_cleanup_receipt(
    manifest_path: Path,
    manifest: Mapping[str, object],
    *,
    ledger_sha256: str,
    receipts_sha256: str,
    committed_at: str,
) -> None:
    final_receipt = _final_cleanup_receipt_document(
        manifest_path,
        manifest,
        ledger_sha256=ledger_sha256,
        receipts_sha256=receipts_sha256,
        committed_at=committed_at,
    )
    final_receipt_path = manifest_path.with_name(f"{manifest_path.name}.final-receipt.json")
    if final_receipt_path.exists():
        if _read_protected_document(final_receipt_path, check="cleanup_finalize_receipt") != final_receipt:
            raise AcceptanceCheckError("cleanup_finalize_conflict")
        return
    _write_protected_document(
        final_receipt_path,
        final_receipt,
        create=True,
        exists_check="cleanup_finalize_conflict",
        write_check="cleanup_finalize_receipt",
    )


@_serialized_control_manifest_write
def cleanup_evidence_finalize(
    manifest_path: Path,
    *,
    ledger_path: Path,
    phase: Literal["prepare", "commit"],
    clear_cleanup_required: bool,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Prepare and commit final cleanup evidence without prematurely clearing teardown."""

    manifest = _read_control_manifest(manifest_path)
    if manifest["gate_ledger_path"] != str(ledger_path):
        raise AcceptanceCheckError("cleanup_finalize_binding")
    ledger = _read_gate_ledger(ledger_path)
    cleanup_records = ledger["cleanup_records"]
    assert isinstance(cleanup_records, list)
    terminal_records = [
        record for record in cleanup_records if isinstance(record, dict) and record.get("check_id") == _TERMINAL_GATE_CHECK_ID
    ]
    if len(terminal_records) > 1:
        raise AcceptanceCheckError("gate_ledger_conflict")
    prefix_records = [
        record for record in cleanup_records if not isinstance(record, dict) or record.get("check_id") != _TERMINAL_GATE_CHECK_ID
    ]
    if [record["check_id"] for record in prefix_records if isinstance(record, dict)] != list(_CLEANUP_GATE_CHECK_ORDER[:-1]):
        raise AcceptanceCheckError("gate_ledger_incomplete")
    receipt_count, receipts_sha256 = _verify_stored_receipts(manifest_path, manifest)
    ledger_records_sha256 = _gate_ledger_records_hash({**ledger, "cleanup_records": prefix_records})
    evidence = manifest["evidence"]
    assert isinstance(evidence, Mapping)
    export_receipt_path = evidence["final_export_receipt_path"]
    export_receipt_sha256 = evidence["final_export_receipt_sha256"]
    if type(export_receipt_path) is not str or type(export_receipt_sha256) is not str:
        raise AcceptanceCheckError("cleanup_finalize_export")
    _export_receipt, observed_export_sha256 = _validate_evidence_export_receipt(
        Path(export_receipt_path),
        manifest=manifest,
        receipts_sha256=receipts_sha256,
        ledger_records_sha256=ledger_records_sha256,
    )
    if observed_export_sha256 != export_receipt_sha256:
        raise AcceptanceCheckError("cleanup_finalize_export")
    timestamp = _utc_timestamp(now())
    candidate_sha = manifest["candidate_sha"]
    assert isinstance(candidate_sha, str)
    terminal_receipt_hash = _sha256(
        json.dumps(
            {
                "check_id": _TERMINAL_GATE_CHECK_ID,
                "prefix_records_sha256": ledger_records_sha256,
                "receipts_sha256": receipts_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if phase == "prepare":
        if clear_cleanup_required or ledger["finalized"] is not None:
            raise AcceptanceCheckError("cleanup_finalize_phase")
        existing = manifest["final_evidence"]
        prepared = {
            "phase": "prepared",
            "prepared_at": timestamp,
            "receipt_count": receipt_count,
            "receipts_sha256": receipts_sha256,
            "ledger_records_sha256": ledger_records_sha256,
            "precommit_manifest_sha256": None,
            "committed_at": None,
            "ledger_sha256": None,
        }
        if isinstance(existing, dict):
            if existing["phase"] == "committed":
                return manifest
            comparable = {**prepared, "prepared_at": existing["prepared_at"]}
            if existing != comparable:
                raise AcceptanceCheckError("cleanup_finalize_conflict")
            if terminal_records:
                terminal = terminal_records[0]
                if (
                    terminal.get("candidate_sha") != candidate_sha
                    or terminal.get("exit_status") != 0
                    or terminal.get("receipt_hash") != terminal_receipt_hash
                ):
                    raise AcceptanceCheckError("cleanup_finalize_conflict")
            return manifest
        if terminal_records:
            raise AcceptanceCheckError("cleanup_finalize_phase")
        manifest["final_evidence"] = prepared
        manifest["updated_at"] = timestamp
        _validate_control_manifest(manifest)
        _write_protected_document(
            manifest_path,
            manifest,
            create=False,
            exists_check="control_manifest_exists",
            write_check="control_manifest_file",
        )
        return manifest
    if phase != "commit" or not clear_cleanup_required:
        raise AcceptanceCheckError("cleanup_finalize_phase")
    final_evidence = manifest["final_evidence"]
    if isinstance(final_evidence, dict) and final_evidence["phase"] == "committed" and manifest["cleanup_required"] is False:
        ledger_sha256 = _gate_ledger_records_hash(ledger)
        if (
            final_evidence["ledger_sha256"] != ledger_sha256
            or final_evidence["receipts_sha256"] != receipts_sha256
            or type(final_evidence["committed_at"]) is not str
        ):
            raise AcceptanceCheckError("cleanup_finalize_conflict")
        _ensure_final_cleanup_receipt(
            manifest_path,
            manifest,
            ledger_sha256=ledger_sha256,
            receipts_sha256=receipts_sha256,
            committed_at=final_evidence["committed_at"],
        )
        return manifest
    if manifest["cleanup_required"] is not True or not isinstance(final_evidence, dict):
        raise AcceptanceCheckError("cleanup_finalize_pending")
    assert isinstance(final_evidence, dict)
    if final_evidence["phase"] != "prepared":
        raise AcceptanceCheckError("cleanup_finalize_pending")
    if (
        final_evidence["receipt_count"] != receipt_count
        or final_evidence["receipts_sha256"] != receipts_sha256
        or final_evidence["ledger_records_sha256"] != ledger_records_sha256
    ):
        raise AcceptanceCheckError("cleanup_finalize_conflict")
    cleanup_states = manifest["cleanup_states"]
    assert isinstance(cleanup_states, dict)
    for surface, state_value in cleanup_states.items():
        if surface == "coordinator":
            continue
        if surface == "teardown_deadline" and manifest["deadline_failure_recorded"] is True:
            if state_value not in {"confirmed", "failed", "pending"}:
                raise AcceptanceCheckError("cleanup_finalize_pending")
            continue
        if state_value != "confirmed":
            raise AcceptanceCheckError("cleanup_finalize_pending")
    cleanup_states["coordinator"] = "confirmed"
    if manifest["deadline_failure_recorded"] is True and cleanup_states["teardown_deadline"] != "confirmed":
        cleanup_states["teardown_deadline"] = "failed"
    if not terminal_records:
        gate_ledger_record_cleanup(
            ledger_path,
            check_id=_TERMINAL_GATE_CHECK_ID,
            exit_status=0,
            receipt_hash=terminal_receipt_hash,
            candidate_sha=candidate_sha,
            started_at=timestamp,
            ended_at=timestamp,
            now=now,
        )
    else:
        terminal = terminal_records[0]
        if (
            terminal.get("candidate_sha") != candidate_sha
            or terminal.get("exit_status") != 0
            or terminal.get("receipt_hash") != terminal_receipt_hash
        ):
            raise AcceptanceCheckError("cleanup_finalize_conflict")
    ledger = _read_gate_ledger(ledger_path)
    ledger_sha256 = _gate_ledger_records_hash(ledger)
    precommit_manifest_sha256 = _sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    final_evidence.update(
        {
            "phase": "committed",
            "precommit_manifest_sha256": precommit_manifest_sha256,
            "committed_at": timestamp,
            "ledger_sha256": ledger_sha256,
        }
    )
    manifest["cleanup_required"] = False
    manifest["updated_at"] = timestamp
    _validate_control_manifest(manifest)
    _write_protected_document(
        manifest_path,
        manifest,
        create=False,
        exists_check="control_manifest_exists",
        write_check="control_manifest_file",
    )
    final_manifest = _read_control_manifest(manifest_path)
    _ensure_final_cleanup_receipt(
        manifest_path,
        final_manifest,
        ledger_sha256=ledger_sha256,
        receipts_sha256=receipts_sha256,
        committed_at=timestamp,
    )
    return final_manifest


def _gate_records_hash(records: object) -> str:
    return _sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _gate_ledger_records_hash(ledger: Mapping[str, object]) -> str:
    return _gate_records_hash(
        {
            "records": ledger["records"],
            "cleanup_records": ledger["cleanup_records"],
        }
    )


def _validate_gate_record_stream(records: object, order: tuple[str, ...]) -> list[dict[str, object]]:
    if not isinstance(records, list) or len(records) > len(order):
        raise AcceptanceCheckError("gate_ledger_schema")
    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != _GATE_RECORD_FIELDS:
            raise AcceptanceCheckError("gate_ledger_schema")
        check_id = record["check_id"]
        if type(check_id) is not str or check_id != order[index] or check_id in seen:
            raise AcceptanceCheckError("gate_ledger_schema")
        seen.add(check_id)
        candidate_sha = record["candidate_sha"]
        if type(candidate_sha) is not str or _GIT_SHA_PATTERN.fullmatch(candidate_sha) is None:
            raise AcceptanceCheckError("gate_ledger_schema")
        started = _control_timestamp(record["started_at"])
        ended = _control_timestamp(record["ended_at"])
        if ended < started:
            raise AcceptanceCheckError("gate_ledger_schema")
        exit_status = record["exit_status"]
        receipt_hash = record["receipt_hash"]
        if type(exit_status) is not int or not 0 <= exit_status <= 255:
            raise AcceptanceCheckError("gate_ledger_schema")
        if type(receipt_hash) is not str or _SHA256_PATTERN.fullmatch(receipt_hash) is None:
            raise AcceptanceCheckError("gate_ledger_schema")
    return cast(list[dict[str, object]], records)


def _validate_gate_ledger(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _GATE_LEDGER_FIELDS:
        raise AcceptanceCheckError("gate_ledger_schema")
    if payload["schema"] != "elspeth.aws-ecs-gate-ledger.v4":
        raise AcceptanceCheckError("gate_ledger_schema")
    branch = payload["branch"]
    starting_sha = payload["starting_sha"]
    if type(branch) is not str or re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch) is None or branch.startswith("/"):
        raise AcceptanceCheckError("gate_ledger_schema")
    if type(starting_sha) is not str or _GIT_SHA_PATTERN.fullmatch(starting_sha) is None:
        raise AcceptanceCheckError("gate_ledger_schema")
    for field, pattern in (
        ("plan_sha256", _SHA256_PATTERN),
        ("program_base_sha", _GIT_SHA_PATTERN),
        ("reconciled_release_sha", _GIT_SHA_PATTERN),
    ):
        value = payload[field]
        if type(value) is not str or pattern.fullmatch(value) is None:
            raise AcceptanceCheckError("gate_ledger_schema")
    records = _validate_gate_record_stream(payload["records"], _SUCCESS_GATE_CHECK_ORDER)
    cleanup_records = _validate_gate_record_stream(payload["cleanup_records"], _CLEANUP_GATE_CHECK_ORDER)
    cleanup_start_count = payload["success_record_count_at_cleanup_start"]
    if cleanup_records:
        if type(cleanup_start_count) is not int or cleanup_start_count != len(records):
            raise AcceptanceCheckError("gate_ledger_schema")
    elif cleanup_start_count is not None:
        raise AcceptanceCheckError("gate_ledger_schema")
    bound_candidate = payload["candidate_sha"]
    bound_record_count = payload["candidate_bound_record_count"]
    if (bound_candidate is None) != (bound_record_count is None):
        raise AcceptanceCheckError("gate_ledger_schema")
    if bound_candidate is None and (len(records) > len(_TASK1_GATE_CHECK_ORDER) or cleanup_records):
        raise AcceptanceCheckError("gate_ledger_schema")
    if bound_candidate is not None and (
        type(bound_candidate) is not str
        or _GIT_SHA_PATTERN.fullmatch(bound_candidate) is None
        or type(bound_record_count) is not int
        or bound_record_count != len(_TASK1_GATE_CHECK_ORDER)
        or bound_record_count > len(records)
        or any(record["candidate_sha"] != bound_candidate for record in records[:bound_record_count])
        or any(record["candidate_sha"] != bound_candidate for record in records[bound_record_count:])
        or any(record["candidate_sha"] != bound_candidate for record in cleanup_records)
    ):
        raise AcceptanceCheckError("gate_ledger_schema")
    finalized = payload["finalized"]
    if finalized is not None:
        if not isinstance(finalized, dict) or set(finalized) != {
            "candidate_sha",
            "record_count",
            "records_sha256",
            "finalized_at",
        }:
            raise AcceptanceCheckError("gate_ledger_schema")
        if (
            type(finalized["candidate_sha"]) is not str
            or _GIT_SHA_PATTERN.fullmatch(finalized["candidate_sha"]) is None
            or finalized["candidate_sha"] != bound_candidate
            or finalized["record_count"] != len(records) + len(cleanup_records)
            or finalized["records_sha256"] != _gate_ledger_records_hash(payload)
        ):
            raise AcceptanceCheckError("gate_ledger_schema")
        _control_timestamp(finalized["finalized_at"])
    created = _control_timestamp(payload["created_at"])
    updated = _control_timestamp(payload["updated_at"])
    if updated < created:
        raise AcceptanceCheckError("gate_ledger_schema")
    return payload


def _read_gate_ledger(path: Path) -> dict[str, object]:
    return _validate_gate_ledger(_read_protected_document(path, check="gate_ledger_file"))


def gate_ledger_init(
    path: Path,
    *,
    branch: str,
    starting_sha: str,
    plan_sha256: str,
    program_base_sha: str,
    reconciled_release_sha: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Create a ledger, or accept an exact-owner unfinalized resume."""

    try:
        existing = _read_gate_ledger(path)
    except AcceptanceCheckError as exc:
        if exc.check != "gate_ledger_file" or path.exists():
            raise
    else:
        if (
            existing["branch"] == branch
            and existing["starting_sha"] == starting_sha
            and existing["plan_sha256"] == plan_sha256
            and existing["program_base_sha"] == program_base_sha
            and existing["reconciled_release_sha"] == reconciled_release_sha
            and existing["finalized"] is None
            and not existing["cleanup_records"]
        ):
            return existing
        raise AcceptanceCheckError("gate_ledger_conflict")
    timestamp = _utc_timestamp(now())
    ledger: dict[str, object] = {
        "schema": "elspeth.aws-ecs-gate-ledger.v4",
        "branch": branch,
        "starting_sha": starting_sha,
        "plan_sha256": plan_sha256,
        "program_base_sha": program_base_sha,
        "reconciled_release_sha": reconciled_release_sha,
        "candidate_sha": None,
        "candidate_bound_record_count": None,
        "records": [],
        "cleanup_records": [],
        "success_record_count_at_cleanup_start": None,
        "finalized": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _validate_gate_ledger(ledger)
    _write_protected_document(
        path,
        ledger,
        create=True,
        exists_check="gate_ledger_exists",
        write_check="gate_ledger_file",
        parent_check="gate_ledger_parent",
    )
    return ledger


def gate_ledger_get(path: Path, field: str) -> str:
    """Return one closed, non-secret ledger anchor for resume-safe shell use."""

    if field not in _GATE_LEDGER_GET_FIELDS:
        raise AcceptanceCheckError("gate_ledger_get")
    value = _read_gate_ledger(path)[field]
    if type(value) is not str:
        raise AcceptanceCheckError("gate_ledger_get")
    return value


def gate_ledger_bind_candidate(
    path: Path,
    *,
    candidate_sha: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Freeze the candidate SHA at the exact ledger boundary where it exists."""

    ledger = _read_gate_ledger(path)
    if ledger["finalized"] is not None:
        raise AcceptanceCheckError("gate_ledger_finalized")
    existing = ledger["candidate_sha"]
    if existing is not None:
        if existing == candidate_sha:
            return ledger
        raise AcceptanceCheckError("gate_ledger_conflict")
    if _GIT_SHA_PATTERN.fullmatch(candidate_sha) is None:
        raise AcceptanceCheckError("gate_ledger_schema")
    records = ledger["records"]
    assert isinstance(records, list)
    if [record["check_id"] for record in records] != list(_TASK1_GATE_CHECK_ORDER):
        raise AcceptanceCheckError("gate_ledger_incomplete")
    if any(record["exit_status"] != 0 for record in records):
        raise AcceptanceCheckError("gate_ledger_failed")
    if any(record["candidate_sha"] != candidate_sha for record in records):
        raise AcceptanceCheckError("gate_ledger_candidate")
    timestamp = _utc_timestamp(now())
    ledger["candidate_sha"] = candidate_sha
    ledger["candidate_bound_record_count"] = len(records)
    ledger["updated_at"] = timestamp
    _validate_gate_ledger(ledger)
    _write_protected_document(
        path,
        ledger,
        create=False,
        exists_check="gate_ledger_exists",
        write_check="gate_ledger_file",
        parent_check="gate_ledger_parent",
    )
    return ledger


def _gate_ledger_record_stream(
    path: Path,
    *,
    stream: Literal["records", "cleanup_records"],
    order: tuple[str, ...],
    check_id: str,
    exit_status: int,
    receipt_hash: str,
    candidate_sha: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    ledger = _read_gate_ledger(path)
    if ledger["finalized"] is not None:
        raise AcceptanceCheckError("gate_ledger_finalized")
    records = ledger[stream]
    assert isinstance(records, list)
    bound_candidate = ledger["candidate_sha"]
    if bound_candidate is not None and candidate_sha != bound_candidate:
        raise AcceptanceCheckError("gate_ledger_candidate")
    existing = next(
        (item for item in records if isinstance(item, dict) and item.get("check_id") == check_id),
        None,
    )
    if existing is not None:
        same_evidence = (
            existing.get("candidate_sha") == candidate_sha
            and existing.get("exit_status") == exit_status
            and existing.get("receipt_hash") == receipt_hash
            and (started_at is None or existing.get("started_at") == started_at)
            and (ended_at is None or existing.get("ended_at") == ended_at)
        )
        if same_evidence:
            return ledger
        raise AcceptanceCheckError("gate_ledger_conflict")
    if stream == "records":
        cleanup_records = ledger["cleanup_records"]
        assert isinstance(cleanup_records, list)
        if cleanup_records or (bound_candidate is None and check_id not in _TASK1_GATE_CHECK_ORDER):
            raise AcceptanceCheckError("gate_ledger_phase")
    expected_index = len(records)
    if expected_index >= len(order) or check_id != order[expected_index]:
        raise AcceptanceCheckError("gate_ledger_schema")
    timestamp = _utc_timestamp(now())
    record = {
        "check_id": check_id,
        "candidate_sha": candidate_sha,
        "started_at": started_at or timestamp,
        "ended_at": ended_at or timestamp,
        "exit_status": exit_status,
        "receipt_hash": receipt_hash,
    }
    candidate = {**ledger, stream: [*records, record], "updated_at": timestamp}
    if stream == "cleanup_records" and not records:
        success_records = ledger["records"]
        assert isinstance(success_records, list)
        candidate["success_record_count_at_cleanup_start"] = len(success_records)
    _validate_gate_ledger(candidate)
    _write_protected_document(
        path,
        candidate,
        create=False,
        exists_check="gate_ledger_exists",
        write_check="gate_ledger_file",
        parent_check="gate_ledger_parent",
    )
    return candidate


def gate_ledger_record(
    path: Path,
    *,
    check_id: str,
    exit_status: int,
    receipt_hash: str,
    candidate_sha: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    return _gate_ledger_record_stream(
        path,
        stream="records",
        order=_SUCCESS_GATE_CHECK_ORDER,
        check_id=check_id,
        exit_status=exit_status,
        receipt_hash=receipt_hash,
        candidate_sha=candidate_sha,
        started_at=started_at,
        ended_at=ended_at,
        now=now,
    )


def gate_ledger_record_cleanup(
    path: Path,
    *,
    check_id: str,
    exit_status: int,
    receipt_hash: str,
    candidate_sha: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Record ordered cleanup evidence independently of incomplete success checks."""

    ledger = _read_gate_ledger(path)
    if ledger["candidate_sha"] is None:
        raise AcceptanceCheckError("gate_ledger_candidate")
    return _gate_ledger_record_stream(
        path,
        stream="cleanup_records",
        order=_CLEANUP_GATE_CHECK_ORDER,
        check_id=check_id,
        exit_status=exit_status,
        receipt_hash=receipt_hash,
        candidate_sha=candidate_sha,
        started_at=started_at,
        ended_at=ended_at,
        now=now,
    )


def gate_ledger_finalize(
    path: Path,
    *,
    candidate_sha: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    ledger = _read_gate_ledger(path)
    if ledger["finalized"] is not None:
        finalized = ledger["finalized"]
        if isinstance(finalized, dict) and finalized.get("candidate_sha") == candidate_sha:
            return ledger
        raise AcceptanceCheckError("gate_ledger_conflict")
    records = ledger["records"]
    cleanup_records = ledger["cleanup_records"]
    assert isinstance(records, list)
    if not records:
        raise AcceptanceCheckError("gate_ledger_empty")
    assert isinstance(cleanup_records, list)
    if [record["check_id"] for record in records if isinstance(record, dict)] != list(_SUCCESS_GATE_CHECK_ORDER):
        raise AcceptanceCheckError("gate_ledger_incomplete")
    if [record["check_id"] for record in cleanup_records if isinstance(record, dict)] != list(_CLEANUP_GATE_CHECK_ORDER):
        raise AcceptanceCheckError("gate_ledger_incomplete")
    if ledger["candidate_sha"] != candidate_sha:
        raise AcceptanceCheckError("gate_ledger_candidate")
    bound_record_count = ledger["candidate_bound_record_count"]
    if (
        type(bound_record_count) is not int
        or any(record["candidate_sha"] != candidate_sha for record in records[bound_record_count:])
        or any(record["candidate_sha"] != candidate_sha for record in cleanup_records)
    ):
        raise AcceptanceCheckError("gate_ledger_candidate")
    if any(record["exit_status"] != 0 for record in [*records, *cleanup_records]):
        raise AcceptanceCheckError("gate_ledger_failed")
    timestamp = _utc_timestamp(now())
    ledger["finalized"] = {
        "candidate_sha": candidate_sha,
        "record_count": len(records) + len(cleanup_records),
        "records_sha256": _gate_ledger_records_hash(ledger),
        "finalized_at": timestamp,
    }
    ledger["updated_at"] = timestamp
    _validate_gate_ledger(ledger)
    _write_protected_document(
        path,
        ledger,
        create=False,
        exists_check="gate_ledger_exists",
        write_check="gate_ledger_file",
        parent_check="gate_ledger_parent",
    )
    return ledger


def build_parser() -> argparse.ArgumentParser:
    """Build the closed command surface used by the Plan 12 controller."""

    parser = argparse.ArgumentParser(prog="python -m elspeth.web.aws_ecs_acceptance")
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser("capture")
    capture.add_argument("--state-file", required=True)

    verify_api = commands.add_parser("verify-api")
    verify_api.add_argument("--state-file", required=True)

    verify_payloads = commands.add_parser("verify-payloads")
    verify_payloads.add_argument("--landscape-run-id", required=True)

    scenario_namespace = commands.add_parser("scenario-namespace")
    scenario_namespace.add_argument("--acceptance-run-id", required=True)
    scenario_namespace.add_argument("--scenario-id", required=True, choices=("A", "B"))

    verify_operator = commands.add_parser("verify-operator-telemetry")
    verify_operator.add_argument("--phase", choices=("positive", "outage"), default="positive")
    verify_operator.add_argument("--landscape-run-id")

    verify_connection = commands.add_parser("verify-connection-budget")
    verify_connection.add_argument("--cluster-id", required=True)
    verify_connection.add_argument("--start-time", required=True)
    verify_connection.add_argument("--approved-budget", required=True, type=int)
    verify_connection.add_argument("--safety-margin", required=True, type=int)

    extract_receipt = commands.add_parser("extract-exec-receipt")
    extract_receipt.add_argument(
        "--check",
        required=True,
        choices=(
            "verify-s3",
            "verify-bedrock",
            "verify-bedrock-guardrails",
            "verify-connection-budget",
            "verify-operator-telemetry",
        ),
    )
    extract_receipt.add_argument("--candidate-sha", required=True)
    extract_receipt.add_argument("--task-arn", required=True)
    extract_receipt.add_argument("--scenario-id", required=True)
    extract_receipt.add_argument("--plugin-policy-binding-sha256")

    control = commands.add_parser("control-manifest")
    control_actions = control.add_subparsers(dest="control_action", required=True)
    control_init = control_actions.add_parser("init")
    for option in (
        "file",
        "acceptance-run-id",
        "candidate-sha",
        "aws-account-id",
        "aws-region",
        "scenario-a-inventory",
        "scenario-b-inventory",
        "scenario-a-tf-binding",
        "scenario-b-tf-binding",
        "evidence-destination-sha256",
        "gate-ledger",
        "teardown-deadline-utc",
    ):
        control_init.add_argument(f"--{option}", required=True)
    control_validate = control_actions.add_parser("validate")
    control_validate.add_argument("--file", required=True)
    control_validate.add_argument("--acceptance-run-id")
    control_validate.add_argument("--candidate-sha")
    control_validate.add_argument("--cleanup-only", action="store_true")
    control_validate.add_argument("--require-cleanup-cleared", action="store_true")
    control_get = control_actions.add_parser("get")
    control_get.add_argument("--file", required=True)
    control_get.add_argument("--field", required=True)
    control_load = control_actions.add_parser("load-cleanup")
    control_load.add_argument("--file", required=True)
    control_load.add_argument("--shell-assignments", action="store_true", required=True)
    control_bind = control_actions.add_parser("bind-scenario")
    control_bind.add_argument("--file", required=True)
    control_bind.add_argument("--scenario-id", required=True, choices=("A", "B"))
    control_bind.add_argument("--inventory", required=True)
    control_bind_retained = control_actions.add_parser("bind-retained-evidence")
    control_bind_retained.add_argument("--file", required=True)
    control_bind_retained.add_argument("--receipt", required=True)
    control_bind_retained.add_argument("--require-complete", action="store_true")
    control_checkpoint_operator = control_actions.add_parser("checkpoint-operator-evidence")
    control_checkpoint_operator.add_argument("--file", required=True)
    control_checkpoint_operator.add_argument("--exec-receipt", required=True)
    control_checkpoint_operator.add_argument("--checkpoint", required=True)
    control_update = control_actions.add_parser("update")
    control_update.add_argument("--file", required=True)
    control_update.add_argument("--cleanup-required", choices=("true",))
    control_update.add_argument("--ecr-baseline-tag")
    control_update.add_argument("--ecr-candidate-tag")
    control_update.add_argument("--ecr-registry")
    control_update.add_argument("--ecr-repository")
    control_update.add_argument("--ecr-baseline-digest")
    control_update.add_argument("--ecr-candidate-digest")
    control_update.add_argument("--acceptance-state-path")
    control_update.add_argument("--oidc-evidence-dir")
    control_update.add_argument("--evidence-export-receipt")
    control_update.add_argument("--final-evidence-export-receipt")
    control_update.add_argument("--terraform-plan-receipt")
    control_update.add_argument("--terraform-applied")
    control_update.add_argument("--terraform-noop-receipt")
    control_update.add_argument("--cleanup-checkpoint")
    control_update.add_argument("--verdict-failure")
    control_update.add_argument("--emergency-cleanup-deadline-utc")
    control_update.add_argument("--cleanup-escalation")

    ledger = commands.add_parser("gate-ledger")
    ledger_actions = ledger.add_subparsers(dest="ledger_action", required=True)
    ledger_init = ledger_actions.add_parser("init")
    ledger_init.add_argument("--file", required=True)
    ledger_init.add_argument("--branch", required=True)
    ledger_init.add_argument("--starting-sha", required=True)
    ledger_init.add_argument("--plan-sha256", required=True)
    ledger_init.add_argument("--program-base-sha", required=True)
    ledger_init.add_argument("--reconciled-release-sha", required=True)
    ledger_get = ledger_actions.add_parser("get")
    ledger_get.add_argument("--file", required=True)
    ledger_get.add_argument("--field", required=True, choices=tuple(sorted(_GATE_LEDGER_GET_FIELDS)))
    ledger_record = ledger_actions.add_parser("record")
    ledger_record.add_argument("--file", required=True)
    ledger_record.add_argument("--check-id", required=True)
    ledger_record.add_argument("--exit-status", required=True, type=int)
    ledger_record.add_argument("--receipt-hash", required=True)
    ledger_record.add_argument("--candidate-sha", required=True)
    ledger_record.add_argument("--started-at")
    ledger_record.add_argument("--ended-at")
    ledger_cleanup = ledger_actions.add_parser("record-cleanup")
    ledger_cleanup.add_argument("--file", required=True)
    ledger_cleanup.add_argument("--check-id", required=True)
    ledger_cleanup.add_argument("--exit-status", required=True, type=int)
    ledger_cleanup.add_argument("--receipt-hash", required=True)
    ledger_cleanup.add_argument("--candidate-sha", required=True)
    ledger_cleanup.add_argument("--started-at")
    ledger_cleanup.add_argument("--ended-at")
    ledger_bind = ledger_actions.add_parser("bind-candidate")
    ledger_bind.add_argument("--file", required=True)
    ledger_bind.add_argument("--candidate-sha", required=True)
    ledger_finalize = ledger_actions.add_parser("finalize")
    ledger_finalize.add_argument("--file", required=True)
    ledger_finalize.add_argument("--candidate-sha", required=True)

    receipt_command = commands.add_parser("receipt-store")
    receipt_command.add_argument("--file", required=True)
    receipt_command.add_argument("--scenario-id", required=True, choices=("A", "B", "bootstrap"))
    receipt_command.add_argument("--kind", required=True)
    receipt_command.add_argument("--subject-id", required=True)
    receipt_input = receipt_command.add_mutually_exclusive_group(required=True)
    receipt_input.add_argument("--receipt-file")
    receipt_input.add_argument("--receipt-stdin", action="store_true")

    approval_command = commands.add_parser("approval-verify")
    approval_command.add_argument("--file", required=True)
    approval_command.add_argument("--scenario-id", required=True, choices=("A", "B", "bootstrap"))
    approval_command.add_argument("--kind", required=True, choices=("terraform-plan", "terraform-destroy-plan"))
    approval_command.add_argument("--plan-receipt-hash", required=True)
    approval_command.add_argument("--approval-file", required=True)
    approval_current = commands.add_parser("approval-require-current")
    approval_current.add_argument("--file", required=True)
    approval_current.add_argument("--scenario-id", required=True, choices=("A", "B", "bootstrap"))
    approval_current.add_argument("--kind", required=True, choices=("terraform-plan", "terraform-destroy-plan"))
    approval_current.add_argument("--plan-receipt-hash", required=True)
    approval_current.add_argument("--approval-hash", required=True)

    scenario_command = commands.add_parser("scenario-load")
    scenario_command.add_argument("--file", required=True)
    scenario_command.add_argument("--scenario-id", required=True, choices=("A", "B"))
    scenario_command.add_argument("--shell-assignments", action="store_true", required=True)

    task_definition_policy = commands.add_parser("validate-task-definition-policy")
    task_definition_policy.add_argument("--file", required=True)
    task_definition_policy.add_argument("--scenario-id", required=True, choices=("A", "B"))
    task_definition_policy.add_argument("--container-name", required=True)
    task_definition_policy.add_argument("--expected-user", choices=("1000:1000",))
    task_definition_policy.add_argument("--expected-image-role", choices=("candidate", "rollback-baseline"), default="candidate")

    compatibility_record = commands.add_parser("compatibility-record-validate")
    compatibility_record.add_argument("--file", required=True)
    compatibility_record.add_argument("--scenario-id", required=True, choices=("A", "B"))
    compatibility_record.add_argument("--record", required=True)

    orphan_command = commands.add_parser("orphan-sweep")
    orphan_command.add_argument("--file", required=True)
    orphan_command.add_argument("--acceptance-run-id", required=True)

    cleanup_command = commands.add_parser("cleanup-evidence-finalize")
    cleanup_command.add_argument("--file", required=True)
    cleanup_command.add_argument("--ledger", required=True)
    cleanup_command.add_argument("--phase", required=True, choices=("prepare", "commit"))
    cleanup_command.add_argument("--clear-cleanup-required", action="store_true")

    evidence_export = commands.add_parser("evidence-export-receipt")
    evidence_export.add_argument("--file", required=True)
    evidence_export.add_argument("--ledger", required=True)
    evidence_export.add_argument("--output", required=True)
    evidence_export.add_argument("--artifact-count", required=True, type=int)

    for command in (
        "provision-storage",
        "verify-local-auth",
        "verify-s3",
        "verify-bedrock",
        "verify-bedrock-guardrails",
    ):
        commands.add_parser(command)

    sanitize_evidence = commands.add_parser("sanitize-evidence")
    sanitize_evidence.add_argument("--kind", required=True, choices=_EVIDENCE_KINDS)
    return parser


def _print_json(value: object) -> None:
    sys.stdout.write(f"{json.dumps(value, sort_keys=True, separators=(',', ':'))}\n")


def _print_error(value: object) -> None:
    sys.stderr.write(f"{json.dumps(value, sort_keys=True, separators=(',', ':'))}\n")


def _write_stdout_line(value: str) -> None:
    sys.stdout.write(f"{value}\n")


def main(argv: list[str] | None = None) -> int:
    """Dispatch the closed acceptance command surface with static failures."""

    args = build_parser().parse_args(argv)
    try:
        if args.command == "capture":
            capture(os.environ, state_file=Path(args.state_file))
        elif args.command == "provision-storage":
            _print_json(provision_storage())
        elif args.command == "scenario-namespace":
            _write_stdout_line(scenario_resource_namespace(args.acceptance_run_id, args.scenario_id))
        elif args.command == "verify-api":
            _print_json(verify_api(os.environ, state_file=Path(args.state_file)))
        elif args.command == "verify-payloads":
            _print_json(verify_payloads(args.landscape_run_id))
        elif args.command == "verify-local-auth":
            _print_json(verify_local_auth())
        elif args.command in {
            "verify-s3",
            "verify-bedrock",
            "verify-bedrock-guardrails",
            "verify-connection-budget",
            "verify-operator-telemetry",
        }:
            with _suppress_process_output():
                if args.command == "verify-s3":
                    details = verify_s3(os.environ)
                elif args.command == "verify-bedrock":
                    details = asyncio.run(verify_bedrock(os.environ))
                elif args.command == "verify-bedrock-guardrails":
                    details = run_bedrock_guardrails_live(os.environ)
                elif args.command == "verify-connection-budget":
                    details = verify_connection_budget_live(
                        os.environ,
                        cluster_id=args.cluster_id,
                        start_time=args.start_time,
                        approved_budget=args.approved_budget,
                        safety_margin=args.safety_margin,
                    )
                else:
                    details = verify_operator_telemetry_live(
                        os.environ,
                        phase=args.phase,
                        landscape_run_id=args.landscape_run_id,
                    )
            _write_stdout_line(encode_exec_receipt(args.command, details, resolve_exec_receipt_env(os.environ)))
        elif args.command == "extract-exec-receipt":
            stream = sys.stdin.read(MAX_EXEC_STREAM_BYTES + 1)
            if args.check == "verify-bedrock-guardrails" and args.plugin_policy_binding_sha256 is None:
                raise AcceptanceCheckError("plugin_policy_binding")
            _print_json(
                extract_exec_receipt(
                    stream,
                    expected_candidate_sha=args.candidate_sha,
                    expected_task_arn=args.task_arn,
                    expected_scenario_id=args.scenario_id,
                    expected_check=args.check,
                    expected_plugin_policy_binding_sha256=args.plugin_policy_binding_sha256,
                )
            )
        elif args.command == "control-manifest":
            path = Path(args.file)
            if args.control_action == "init":
                control_manifest_init(
                    path,
                    acceptance_run_id=args.acceptance_run_id,
                    candidate_sha=args.candidate_sha,
                    aws_account_id=args.aws_account_id,
                    aws_region=args.aws_region,
                    scenario_a_inventory=args.scenario_a_inventory,
                    scenario_b_inventory=args.scenario_b_inventory,
                    scenario_a_tf_binding=args.scenario_a_tf_binding,
                    scenario_b_tf_binding=args.scenario_b_tf_binding,
                    evidence_destination_sha256=args.evidence_destination_sha256,
                    gate_ledger=args.gate_ledger,
                    teardown_deadline_utc=args.teardown_deadline_utc,
                )
            elif args.control_action == "validate":
                if args.cleanup_only != args.require_cleanup_cleared:
                    raise AcceptanceCheckError("control_manifest_cleanup")
                control_manifest_validate(
                    path,
                    acceptance_run_id=args.acceptance_run_id,
                    candidate_sha=args.candidate_sha,
                    cleanup_only=args.cleanup_only,
                    require_cleanup_cleared=args.require_cleanup_cleared,
                )
            elif args.control_action == "get":
                _write_stdout_line(control_manifest_get(path, args.field))
            elif args.control_action == "load-cleanup":
                sys.stdout.write(control_manifest_load_cleanup(path))
            elif args.control_action == "bind-scenario":
                control_manifest_bind_scenario(path, scenario_id=args.scenario_id, inventory_path=args.inventory)
            elif args.control_action == "bind-retained-evidence":
                control_manifest_bind_retained_evidence(
                    path,
                    receipt_path=args.receipt,
                    require_complete=args.require_complete,
                )
            elif args.control_action == "checkpoint-operator-evidence":
                control_manifest_checkpoint_operator_evidence(
                    path,
                    exec_receipt_path=args.exec_receipt,
                    checkpoint_path=args.checkpoint,
                )
            else:
                control_manifest_update(
                    path,
                    cleanup_required=True if args.cleanup_required == "true" else None,
                    ecr_baseline_tag=args.ecr_baseline_tag,
                    ecr_candidate_tag=args.ecr_candidate_tag,
                    ecr_registry=args.ecr_registry,
                    ecr_repository=args.ecr_repository,
                    ecr_baseline_digest=args.ecr_baseline_digest,
                    ecr_candidate_digest=args.ecr_candidate_digest,
                    acceptance_state_path=args.acceptance_state_path,
                    oidc_evidence_dir=args.oidc_evidence_dir,
                    evidence_export_receipt=args.evidence_export_receipt,
                    final_evidence_export_receipt=args.final_evidence_export_receipt,
                    terraform_plan_receipt=args.terraform_plan_receipt,
                    terraform_applied=args.terraform_applied,
                    terraform_noop_receipt=args.terraform_noop_receipt,
                    cleanup_checkpoint=args.cleanup_checkpoint,
                    verdict_failure=args.verdict_failure,
                    emergency_cleanup_deadline_utc=args.emergency_cleanup_deadline_utc,
                    cleanup_escalation=args.cleanup_escalation,
                )
        elif args.command == "gate-ledger":
            path = Path(args.file)
            if args.ledger_action == "init":
                gate_ledger_init(
                    path,
                    branch=args.branch,
                    starting_sha=args.starting_sha,
                    plan_sha256=args.plan_sha256,
                    program_base_sha=args.program_base_sha,
                    reconciled_release_sha=args.reconciled_release_sha,
                )
            elif args.ledger_action == "get":
                _write_stdout_line(gate_ledger_get(path, args.field))
            elif args.ledger_action == "record":
                gate_ledger_record(
                    path,
                    check_id=args.check_id,
                    exit_status=args.exit_status,
                    receipt_hash=args.receipt_hash,
                    candidate_sha=args.candidate_sha,
                    started_at=args.started_at,
                    ended_at=args.ended_at,
                )
            elif args.ledger_action == "record-cleanup":
                gate_ledger_record_cleanup(
                    path,
                    check_id=args.check_id,
                    exit_status=args.exit_status,
                    receipt_hash=args.receipt_hash,
                    candidate_sha=args.candidate_sha,
                    started_at=args.started_at,
                    ended_at=args.ended_at,
                )
            elif args.ledger_action == "bind-candidate":
                gate_ledger_bind_candidate(path, candidate_sha=args.candidate_sha)
            else:
                gate_ledger_finalize(path, candidate_sha=args.candidate_sha)
        elif args.command == "receipt-store":
            receipt_bytes = sys.stdin.buffer.read(MAX_CONTROL_DOCUMENT_BYTES + 1) if args.receipt_stdin else None
            _write_stdout_line(
                receipt_store(
                    Path(args.file),
                    scenario_id=args.scenario_id,
                    kind=args.kind,
                    subject_id=args.subject_id,
                    receipt_file=Path(args.receipt_file) if args.receipt_file else None,
                    receipt_bytes=receipt_bytes,
                )
            )
        elif args.command == "approval-verify":
            _write_stdout_line(
                approval_verify(
                    Path(args.file),
                    scenario_id=args.scenario_id,
                    kind=args.kind,
                    plan_receipt_hash=args.plan_receipt_hash,
                    approval_file=Path(args.approval_file),
                )
            )
        elif args.command == "approval-require-current":
            approval_require_current(
                Path(args.file),
                scenario_id=args.scenario_id,
                kind=args.kind,
                plan_receipt_hash=args.plan_receipt_hash,
                approval_hash=args.approval_hash,
            )
        elif args.command == "scenario-load":
            sys.stdout.write(scenario_load(Path(args.file), scenario_id=args.scenario_id))
        elif args.command == "validate-task-definition-policy":
            raw_payload = sys.stdin.read(MAX_JSON_RESPONSE_BYTES + 1)
            if len(raw_payload.encode("utf-8")) > MAX_JSON_RESPONSE_BYTES:
                raise AcceptanceCheckError("task_definition_policy_binding")
            try:
                payload = json.loads(raw_payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise AcceptanceCheckError("task_definition_policy_binding") from None
            task_definition_arn = validate_task_definition_policy_binding(
                payload,
                manifest_path=Path(args.file),
                scenario_id=args.scenario_id,
                container_name=args.container_name,
                expected_user=args.expected_user,
                expected_image_role=args.expected_image_role,
            )
            _print_json({"task_definition_arn": task_definition_arn})
        elif args.command == "compatibility-record-validate":
            _print_json(
                validate_compatibility_record(
                    Path(args.record),
                    manifest_path=Path(args.file),
                    scenario_id=args.scenario_id,
                )
            )
        elif args.command == "orphan-sweep":
            _print_json(orphan_sweep(Path(args.file), acceptance_run_id=args.acceptance_run_id))
        elif args.command == "cleanup-evidence-finalize":
            cleanup_evidence_finalize(
                Path(args.file),
                ledger_path=Path(args.ledger),
                phase=args.phase,
                clear_cleanup_required=args.clear_cleanup_required,
            )
        elif args.command == "evidence-export-receipt":
            _print_json(
                create_evidence_export_receipt(
                    Path(args.file),
                    ledger_path=Path(args.ledger),
                    output_path=Path(args.output),
                    artifact_count=args.artifact_count,
                )
            )
        elif args.command == "sanitize-evidence":
            content = sys.stdin.buffer.read(MAX_CONTROL_DOCUMENT_BYTES + 1)
            if len(content) > MAX_CONTROL_DOCUMENT_BYTES:
                raise AcceptanceCheckError("sanitize_evidence_schema")
            try:
                raw_evidence = json.loads(content)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise AcceptanceCheckError("sanitize_evidence_schema") from None
            _print_json(sanitize_evidence(args.kind, raw_evidence))
        else:
            raise AcceptanceCheckError("command_not_implemented")
    except AcceptanceCheckError as exc:
        _print_error({"error_class": type(exc).__name__, "check": exc.check})
        return 1
    except (AcceptanceHttpError, AcceptanceInputError, AcceptanceStateError, OperatorTelemetryAcceptanceError) as exc:
        _print_error({"error_class": type(exc).__name__})
        return 1
    except Exception:
        _print_error({"error_class": "AcceptanceInternalError"})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
