"""Validation for the AWS ECS acceptance control manifest."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from .contracts import (
    _GIT_SHA_PATTERN,
    _SHA256_PATTERN,
    AcceptanceCheckError,
    AcceptanceInputError,
    _canonical_uuid,
    _control_timestamp,
    _sha256,
)
from .receipt_contracts import _TERRAFORM_RECEIPT_KINDS
from .scenario_inventory import (
    _control_path,
    _load_bound_scenario_inventory,
    _validate_orphan_inventory,
)
from .secure_documents import _read_protected_document

_CLEANUP_SURFACES = (
    "coordinator",
    "evidence_export",
    "identity_cleanup",
    "shared_resource_cleanup",
    "terraform_scenario_a",
    "terraform_scenario_b",
    "orphan_sweep",
    "ecr_baseline",
    "ecr_candidate",
    "local_images",
    "final_evidence_prepare",
    "local_evidence",
    "teardown_deadline",
)
CLEANUP_SURFACES = _CLEANUP_SURFACES
_CONTROL_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "acceptance_run_id",
        "candidate_sha",
        "aws",
        "scenarios",
        "gate_ledger_path",
        "teardown_deadline_utc",
        "emergency_cleanup_deadline_utc",
        "cleanup_required",
        "cleanup_states",
        "ecr",
        "evidence",
        "verdict_failures",
        "cleanup_escalations",
        "deadline_failure_recorded",
        "final_evidence",
        "created_at",
        "updated_at",
    }
)
_INFRASTRUCTURE_APPROVAL_SCOPES = frozenset({"A", "B", "bootstrap"})
_RETAINED_EVIDENCE_FIELDS = frozenset(
    {
        "cloudwatch_retained_metrics",
        "xray_retained_trace_ids",
        "expected_retained_metric_series",
        "expected_retained_trace_ids",
    }
)


def _validate_control_manifest(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _CONTROL_MANIFEST_FIELDS:
        raise AcceptanceCheckError("control_manifest_schema")
    if payload["schema"] != "elspeth.aws-ecs-control-manifest.v5":
        raise AcceptanceCheckError("control_manifest_schema")
    try:
        _canonical_uuid(payload["acceptance_run_id"], label="control manifest run identity")
    except AcceptanceInputError:
        raise AcceptanceCheckError("control_manifest_schema") from None
    candidate_sha = payload["candidate_sha"]
    if type(candidate_sha) is not str or _GIT_SHA_PATTERN.fullmatch(candidate_sha) is None:
        raise AcceptanceCheckError("control_manifest_schema")
    aws = payload["aws"]
    if not isinstance(aws, dict) or set(aws) != {"account_id", "region"}:
        raise AcceptanceCheckError("control_manifest_schema")
    if type(aws["account_id"]) is not str or re.fullmatch(r"[0-9]{12}", aws["account_id"]) is None:
        raise AcceptanceCheckError("control_manifest_schema")
    if type(aws["region"]) is not str or re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]", aws["region"]) is None:
        raise AcceptanceCheckError("control_manifest_schema")
    scenarios = payload["scenarios"]
    if not isinstance(scenarios, dict) or set(scenarios) != {"A", "B"}:
        raise AcceptanceCheckError("control_manifest_schema")
    scenario_fields = {
        "preapply_inventory_path",
        "preapply_inventory_sha256",
        "inventory_path",
        "inventory_sha256",
        "inventory_phase",
        "tf_binding_sha256",
        "tf_binding_path",
        "tf_state_identity_sha256",
        "terraform_plan_receipt",
        "terraform_applied",
        "terraform_noop_receipt",
    }
    for scenario in scenarios.values():
        if not isinstance(scenario, dict) or set(scenario) != scenario_fields:
            raise AcceptanceCheckError("control_manifest_schema")
        _control_path(scenario["preapply_inventory_path"])
        preapply_inventory_sha = scenario["preapply_inventory_sha256"]
        if type(preapply_inventory_sha) is not str or _SHA256_PATTERN.fullmatch(preapply_inventory_sha) is None:
            raise AcceptanceCheckError("control_manifest_schema")
        _control_path(scenario["inventory_path"])
        inventory_sha = scenario["inventory_sha256"]
        if type(inventory_sha) is not str or _SHA256_PATTERN.fullmatch(inventory_sha) is None:
            raise AcceptanceCheckError("control_manifest_schema")
        if scenario["inventory_phase"] not in {"preapply", "resolved"}:
            raise AcceptanceCheckError("control_manifest_schema")
        if scenario["inventory_phase"] == "preapply" and (
            scenario["inventory_path"] != scenario["preapply_inventory_path"]
            or scenario["inventory_sha256"] != scenario["preapply_inventory_sha256"]
        ):
            raise AcceptanceCheckError("control_manifest_schema")
        binding = scenario["tf_binding_sha256"]
        if type(binding) is not str or _SHA256_PATTERN.fullmatch(binding) is None:
            raise AcceptanceCheckError("control_manifest_schema")
        _control_path(scenario["tf_binding_path"])
        state_identity = scenario["tf_state_identity_sha256"]
        if type(state_identity) is not str or _SHA256_PATTERN.fullmatch(state_identity) is None:
            raise AcceptanceCheckError("control_manifest_schema")
        if type(scenario["terraform_applied"]) is not bool:
            raise AcceptanceCheckError("control_manifest_schema")
        for field in ("terraform_plan_receipt", "terraform_noop_receipt"):
            value = scenario[field]
            if value is not None and (type(value) is not str or not value or len(value) > 1024):
                raise AcceptanceCheckError("control_manifest_schema")
    if scenarios["A"]["inventory_path"] == scenarios["B"]["inventory_path"]:
        raise AcceptanceCheckError("control_manifest_schema")
    if scenarios["A"]["inventory_sha256"] == scenarios["B"]["inventory_sha256"]:
        raise AcceptanceCheckError("control_manifest_schema")
    if scenarios["A"]["preapply_inventory_path"] == scenarios["B"]["preapply_inventory_path"]:
        raise AcceptanceCheckError("control_manifest_schema")
    if scenarios["A"]["preapply_inventory_sha256"] == scenarios["B"]["preapply_inventory_sha256"]:
        raise AcceptanceCheckError("control_manifest_schema")
    if scenarios["A"]["tf_binding_sha256"] == scenarios["B"]["tf_binding_sha256"]:
        raise AcceptanceCheckError("control_manifest_schema")
    if scenarios["A"]["tf_binding_path"] == scenarios["B"]["tf_binding_path"]:
        raise AcceptanceCheckError("control_manifest_schema")
    if scenarios["A"]["tf_state_identity_sha256"] == scenarios["B"]["tf_state_identity_sha256"]:
        raise AcceptanceCheckError("control_manifest_schema")
    _control_path(payload["gate_ledger_path"])
    deadline = _control_timestamp(payload["teardown_deadline_utc"])
    emergency = payload["emergency_cleanup_deadline_utc"]
    if emergency is not None and _control_timestamp(emergency) <= deadline:
        raise AcceptanceCheckError("control_manifest_schema")
    if type(payload["cleanup_required"]) is not bool or type(payload["deadline_failure_recorded"]) is not bool:
        raise AcceptanceCheckError("control_manifest_schema")
    cleanup_states = payload["cleanup_states"]
    if not isinstance(cleanup_states, dict) or set(cleanup_states) != set(_CLEANUP_SURFACES):
        raise AcceptanceCheckError("control_manifest_schema")
    if any(value not in {"pending", "confirmed", "failed", "interrupted"} for value in cleanup_states.values()):
        raise AcceptanceCheckError("control_manifest_schema")
    ecr = payload["ecr"]
    if not isinstance(ecr, dict) or set(ecr) != {
        "registry",
        "repository",
        "baseline_tag",
        "candidate_tag",
        "baseline_digest",
        "candidate_digest",
    }:
        raise AcceptanceCheckError("control_manifest_schema")
    registry = ecr["registry"]
    repository = ecr["repository"]
    if registry is not None and (
        type(registry) is not str or len(registry) > 253 or re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", registry) is None
    ):
        raise AcceptanceCheckError("control_manifest_schema")
    if repository is not None and (
        type(repository) is not str or len(repository) > 256 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", repository) is None
    ):
        raise AcceptanceCheckError("control_manifest_schema")
    if (registry is None) != (repository is None):
        raise AcceptanceCheckError("control_manifest_schema")
    run_id = payload["acceptance_run_id"]
    for field in ("baseline_tag", "candidate_tag"):
        value = ecr[field]
        if value is not None and (
            type(value) is not str or len(value) > 300 or re.fullmatch(r"[A-Za-z0-9._-]+", value) is None or run_id not in value
        ):
            raise AcceptanceCheckError("control_manifest_schema")
    for field in ("baseline_digest", "candidate_digest"):
        value = ecr[field]
        if value is not None and (type(value) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None):
            raise AcceptanceCheckError("control_manifest_schema")
    evidence = payload["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {
        "acceptance_state_path",
        "oidc_evidence_dir",
        "destination_sha256",
        "export_receipt_path",
        "export_receipt_sha256",
        "final_export_receipt_path",
        "final_export_receipt_sha256",
        "retained_evidence_path",
        "retained_evidence_sha256",
        "receipts",
        "approvals",
    }:
        raise AcceptanceCheckError("control_manifest_schema")
    for field in ("acceptance_state_path", "oidc_evidence_dir"):
        value = evidence[field]
        if value is not None:
            _control_path(value)
    destination_sha256 = evidence["destination_sha256"]
    if type(destination_sha256) is not str or _SHA256_PATTERN.fullmatch(destination_sha256) is None:
        raise AcceptanceCheckError("control_manifest_schema")
    for path_field, hash_field in (
        ("export_receipt_path", "export_receipt_sha256"),
        ("final_export_receipt_path", "final_export_receipt_sha256"),
        ("retained_evidence_path", "retained_evidence_sha256"),
    ):
        export_path = evidence[path_field]
        export_sha256 = evidence[hash_field]
        if (export_path is None) != (export_sha256 is None):
            raise AcceptanceCheckError("control_manifest_schema")
        if export_path is not None:
            _control_path(export_path)
            if type(export_sha256) is not str or _SHA256_PATTERN.fullmatch(export_sha256) is None:
                raise AcceptanceCheckError("control_manifest_schema")
    if evidence["export_receipt_path"] is not None and evidence["export_receipt_path"] == evidence["final_export_receipt_path"]:
        raise AcceptanceCheckError("control_manifest_schema")
    receipts = evidence["receipts"]
    if not isinstance(receipts, list) or len(receipts) > 4096:
        raise AcceptanceCheckError("control_manifest_schema")
    for receipt in receipts:
        if not isinstance(receipt, dict) or set(receipt) != {
            "scenario_id",
            "kind",
            "subject_sha256",
            "receipt_sha256",
            "stored_at",
        }:
            raise AcceptanceCheckError("control_manifest_schema")
        if type(receipt["kind"]) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", receipt["kind"]) is None:
            raise AcceptanceCheckError("control_manifest_schema")
        if receipt["scenario_id"] not in _INFRASTRUCTURE_APPROVAL_SCOPES or (
            receipt["scenario_id"] == "bootstrap" and receipt["kind"] not in _TERRAFORM_RECEIPT_KINDS
        ):
            raise AcceptanceCheckError("control_manifest_schema")
        for field in ("subject_sha256", "receipt_sha256"):
            value = receipt[field]
            if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
                raise AcceptanceCheckError("control_manifest_schema")
        _control_timestamp(receipt["stored_at"])
    approvals = evidence["approvals"]
    if not isinstance(approvals, list) or len(approvals) > 4096:
        raise AcceptanceCheckError("control_manifest_schema")
    for approval in approvals:
        if not isinstance(approval, dict) or set(approval) != {
            "scenario_id",
            "kind",
            "plan_receipt_sha256",
            "approval_sha256",
            "approval_path",
            "expires_at",
            "verified_at",
        }:
            raise AcceptanceCheckError("control_manifest_schema")
        if approval["scenario_id"] not in _INFRASTRUCTURE_APPROVAL_SCOPES or approval["kind"] not in {
            "terraform-plan",
            "terraform-destroy-plan",
        }:
            raise AcceptanceCheckError("control_manifest_schema")
        for field in ("plan_receipt_sha256", "approval_sha256"):
            value = approval[field]
            if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
                raise AcceptanceCheckError("control_manifest_schema")
        _control_path(approval["approval_path"])
        expires_at = _control_timestamp(approval["expires_at"])
        if expires_at <= _control_timestamp(approval["verified_at"]):
            raise AcceptanceCheckError("control_manifest_schema")
        _control_timestamp(approval["verified_at"])
    for field in ("verdict_failures", "cleanup_escalations"):
        values = payload[field]
        if not isinstance(values, list) or len(values) > 128:
            raise AcceptanceCheckError("control_manifest_schema")
        if any(type(value) is not str or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value) is None for value in values):
            raise AcceptanceCheckError("control_manifest_schema")
    final_evidence = payload["final_evidence"]
    if final_evidence is not None:
        if not isinstance(final_evidence, dict) or set(final_evidence) != {
            "phase",
            "prepared_at",
            "receipt_count",
            "receipts_sha256",
            "ledger_records_sha256",
            "precommit_manifest_sha256",
            "committed_at",
            "ledger_sha256",
        }:
            raise AcceptanceCheckError("control_manifest_schema")
        if final_evidence["phase"] not in {"prepared", "committed"}:
            raise AcceptanceCheckError("control_manifest_schema")
        _control_timestamp(final_evidence["prepared_at"])
        if type(final_evidence["receipt_count"]) is not int or final_evidence["receipt_count"] < 0:
            raise AcceptanceCheckError("control_manifest_schema")
        for field in ("receipts_sha256", "ledger_records_sha256"):
            value = final_evidence[field]
            if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
                raise AcceptanceCheckError("control_manifest_schema")
        if final_evidence["phase"] == "prepared":
            if any(final_evidence[field] is not None for field in ("precommit_manifest_sha256", "committed_at", "ledger_sha256")):
                raise AcceptanceCheckError("control_manifest_schema")
        else:
            for field in ("precommit_manifest_sha256", "ledger_sha256"):
                value = final_evidence[field]
                if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
                    raise AcceptanceCheckError("control_manifest_schema")
            _control_timestamp(final_evidence["committed_at"])
    created = _control_timestamp(payload["created_at"])
    updated = _control_timestamp(payload["updated_at"])
    if updated < created:
        raise AcceptanceCheckError("control_manifest_schema")
    return payload


def _read_control_manifest(path: Path) -> dict[str, object]:
    return _validate_control_manifest(_read_protected_document(path, check="control_manifest_file"))


def _require_mutable_control_manifest(manifest: Mapping[str, object]) -> None:
    """Reject every rewrite after final receipt authority is committed."""

    final_evidence = manifest["final_evidence"]
    if final_evidence is not None:
        phase = cast(Mapping[str, object], final_evidence)["phase"]
        if phase == "committed":
            raise AcceptanceCheckError("control_manifest_finalized")


def _validate_retained_evidence_receipt(
    payload: object,
    *,
    manifest: Mapping[str, object],
) -> tuple[dict[str, object], str]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "acceptance_run_id",
        "candidate_sha",
        "scenarios",
        "captured_at",
    }:
        raise AcceptanceCheckError("retained_evidence_schema")
    if (
        payload["schema"] != "elspeth.aws-ecs-retained-evidence.v1"
        or payload["acceptance_run_id"] != manifest["acceptance_run_id"]
        or payload["candidate_sha"] != manifest["candidate_sha"]
    ):
        raise AcceptanceCheckError("retained_evidence_binding")
    _control_timestamp(payload["captured_at"])
    scenario_evidence = payload["scenarios"]
    if not isinstance(scenario_evidence, dict) or set(scenario_evidence) != {"A", "B"}:
        raise AcceptanceCheckError("retained_evidence_schema")
    canonical_metrics: dict[str, set[str]] = {}
    trace_ids: dict[str, set[str]] = {}
    acceptance_run_id = cast(str, manifest["acceptance_run_id"])
    for scenario_id in ("A", "B"):
        inventory = _load_bound_scenario_inventory(manifest, scenario_id, require_resolved=True)
        orphan = cast(dict[str, object], inventory["orphan_sweep"])
        evidence = scenario_evidence[scenario_id]
        if not isinstance(evidence, dict) or set(evidence) != _RETAINED_EVIDENCE_FIELDS:
            raise AcceptanceCheckError("retained_evidence_schema")
        candidate_orphan = {**orphan, **evidence}
        _validate_orphan_inventory(candidate_orphan)
        metrics = cast(list[dict[str, object]], evidence["cloudwatch_retained_metrics"])
        traces = cast(list[str], evidence["xray_retained_trace_ids"])
        if len(metrics) != len(traces):
            raise AcceptanceCheckError("retained_evidence_schema")
        namespace = f"{acceptance_run_id}-{scenario_id.lower()}"
        if any(
            not any(
                cast(Mapping[str, object], dimension)["name"] == "elspeth.acceptance.namespace"
                and cast(Mapping[str, object], dimension)["value"] == namespace
                for dimension in cast(list[object], metric["dimensions"])
            )
            for metric in metrics
        ):
            raise AcceptanceCheckError("retained_evidence_binding")
        canonical_metrics[scenario_id] = {json.dumps(metric, sort_keys=True, separators=(",", ":")) for metric in metrics}
        trace_ids[scenario_id] = set(traces)
    if canonical_metrics["A"] & canonical_metrics["B"] or trace_ids["A"] & trace_ids["B"]:
        raise AcceptanceCheckError("retained_evidence_binding")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return payload, _sha256(canonical)


def _load_retained_evidence(manifest: Mapping[str, object]) -> dict[str, object]:
    evidence = cast(Mapping[str, object], manifest["evidence"])
    path = evidence["retained_evidence_path"]
    expected_sha256 = evidence["retained_evidence_sha256"]
    if type(path) is not str or type(expected_sha256) is not str:
        raise AcceptanceCheckError("retained_evidence_missing")
    receipt, observed_sha256 = _validate_retained_evidence_receipt(
        _read_protected_document(Path(path), check="retained_evidence_file"),
        manifest=manifest,
    )
    if observed_sha256 != expected_sha256:
        raise AcceptanceCheckError("retained_evidence_binding")
    return receipt
