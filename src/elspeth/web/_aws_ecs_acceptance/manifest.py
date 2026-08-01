"""Serialized control-manifest initialization, mutation, and reads."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .contracts import _SCENARIO_VALUE_FIELDS, AcceptanceCheckError, _control_timestamp, _utc_timestamp
from .manifest_schema import (
    _CLEANUP_SURFACES,
    _load_retained_evidence,
    _read_control_manifest,
    _require_mutable_control_manifest,
    _validate_control_manifest,
    _validate_retained_evidence_receipt,
)
from .receipt_contracts import _validate_exec_receipt_schema
from .scenario_inventory import (
    _ORPHAN_INVENTORY_FIELDS,
    _PROVIDER_GENERATED_ORPHAN_FIELDS,
    _RESOLVED_SCENARIO_FIELDS,
    _control_path,
    _load_bound_scenario_inventory,
    _load_preapply_scenario_inventory,
    _scenario_inventory_hash,
    _validate_scenario_inventory,
    _validate_scenario_inventory_isolation,
    _validate_tf_binding_receipt,
)
from .secure_documents import (
    _read_protected_document,
    _serialized_control_manifest_write,
    _write_protected_document,
)


def _control_manifest_bind_retained_evidence_locked(
    path: Path,
    *,
    receipt_path: str,
    require_complete: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Bind an immutable monotonic checkpoint of retained metric/trace identities."""

    manifest = _read_control_manifest(path)
    _require_mutable_control_manifest(manifest)
    current = now()
    if current >= _control_timestamp(manifest["teardown_deadline_utc"]):
        raise AcceptanceCheckError("control_manifest_expired")
    protected_path = _control_path(receipt_path)
    receipt, receipt_sha256 = _validate_retained_evidence_receipt(
        _read_protected_document(Path(protected_path), check="retained_evidence_file"),
        manifest=manifest,
    )
    if require_complete:
        scenarios = cast(dict[str, object], receipt["scenarios"])
        if any(
            not cast(dict[str, object], scenarios[scenario_id])["cloudwatch_retained_metrics"]
            or not cast(dict[str, object], scenarios[scenario_id])["xray_retained_trace_ids"]
            for scenario_id in ("A", "B")
        ):
            raise AcceptanceCheckError("retained_evidence_incomplete")
    evidence = cast(dict[str, object], manifest["evidence"])
    if evidence["retained_evidence_path"] is not None:
        if evidence["retained_evidence_path"] == protected_path and evidence["retained_evidence_sha256"] == receipt_sha256:
            return manifest
        previous = _load_retained_evidence(manifest)
        if _control_timestamp(cast(str, receipt["captured_at"])) < _control_timestamp(cast(str, previous["captured_at"])):
            raise AcceptanceCheckError("retained_evidence_conflict")
        previous_scenarios = cast(dict[str, object], previous["scenarios"])
        next_scenarios = cast(dict[str, object], receipt["scenarios"])
        grew = False
        for scenario_id in ("A", "B"):
            previous_scenario = cast(dict[str, object], previous_scenarios[scenario_id])
            next_scenario = cast(dict[str, object], next_scenarios[scenario_id])
            previous_metrics = {
                json.dumps(item, sort_keys=True, separators=(",", ":"))
                for item in cast(list[dict[str, object]], previous_scenario["cloudwatch_retained_metrics"])
            }
            next_metrics = {
                json.dumps(item, sort_keys=True, separators=(",", ":"))
                for item in cast(list[dict[str, object]], next_scenario["cloudwatch_retained_metrics"])
            }
            previous_traces = set(cast(list[str], previous_scenario["xray_retained_trace_ids"]))
            next_traces = set(cast(list[str], next_scenario["xray_retained_trace_ids"]))
            if not previous_metrics <= next_metrics or not previous_traces <= next_traces:
                raise AcceptanceCheckError("retained_evidence_conflict")
            grew = grew or previous_metrics != next_metrics or previous_traces != next_traces
        if not grew:
            raise AcceptanceCheckError("retained_evidence_conflict")
    evidence["retained_evidence_path"] = protected_path
    evidence["retained_evidence_sha256"] = receipt_sha256
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


@_serialized_control_manifest_write
def control_manifest_bind_retained_evidence(
    path: Path,
    *,
    receipt_path: str,
    require_complete: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Bind an immutable monotonic checkpoint of retained metric/trace identities."""

    return _control_manifest_bind_retained_evidence_locked(
        path,
        receipt_path=receipt_path,
        require_complete=require_complete,
        now=now,
    )


@_serialized_control_manifest_write
def control_manifest_checkpoint_operator_evidence(
    path: Path,
    *,
    exec_receipt_path: str,
    checkpoint_path: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Create and bind the next immutable checkpoint from one positive operator receipt."""

    manifest = _read_control_manifest(path)
    _require_mutable_control_manifest(manifest)
    exec_receipt = _validate_exec_receipt_schema(
        _read_protected_document(Path(_control_path(exec_receipt_path)), check="operator_exec_receipt_file")
    )
    scenario_id = exec_receipt["scenario_id"]
    details = cast(dict[str, object], exec_receipt["details"])
    if (
        exec_receipt["check"] != "verify-operator-telemetry"
        or exec_receipt["candidate_sha"] != manifest["candidate_sha"]
        or scenario_id not in {"A", "B"}
        or details["phase"] != "positive"
    ):
        raise AcceptanceCheckError("retained_evidence_binding")
    metric_query = cast(dict[str, object], details["retained_metric_query"])
    trace_id = cast(str, details["retained_trace_id"])
    protected_checkpoint = Path(_control_path(checkpoint_path))
    current = now()

    if protected_checkpoint.exists():
        existing, _existing_sha256 = _validate_retained_evidence_receipt(
            _read_protected_document(protected_checkpoint, check="retained_evidence_file"),
            manifest=manifest,
        )
        existing_scenarios = cast(dict[str, object], existing["scenarios"])
        existing_scenario = cast(dict[str, object], existing_scenarios[scenario_id])
        existing_metrics = cast(list[dict[str, object]], existing_scenario["cloudwatch_retained_metrics"])
        existing_traces = cast(list[str], existing_scenario["xray_retained_trace_ids"])
        if metric_query not in existing_metrics or trace_id not in existing_traces:
            raise AcceptanceCheckError("retained_evidence_conflict")
    else:
        evidence = cast(dict[str, object], manifest["evidence"])
        if evidence["retained_evidence_path"] is None:
            scenarios: dict[str, object] = {
                scenario: {
                    "cloudwatch_retained_metrics": [],
                    "xray_retained_trace_ids": [],
                    "expected_retained_metric_series": 0,
                    "expected_retained_trace_ids": 0,
                }
                for scenario in ("A", "B")
            }
        else:
            previous = _load_retained_evidence(manifest)
            scenarios = json.loads(json.dumps(previous["scenarios"]))
        scenario = cast(dict[str, object], scenarios[scenario_id])
        metrics = cast(list[dict[str, object]], scenario["cloudwatch_retained_metrics"])
        traces = cast(list[str], scenario["xray_retained_trace_ids"])
        if metric_query in metrics or trace_id in traces:
            raise AcceptanceCheckError("retained_evidence_conflict")
        metrics.append(metric_query)
        traces.append(trace_id)
        scenario["expected_retained_metric_series"] = len(metrics)
        scenario["expected_retained_trace_ids"] = len(traces)
        checkpoint = {
            "schema": "elspeth.aws-ecs-retained-evidence.v1",
            "acceptance_run_id": manifest["acceptance_run_id"],
            "candidate_sha": manifest["candidate_sha"],
            "scenarios": scenarios,
            "captured_at": _utc_timestamp(current),
        }
        _validate_retained_evidence_receipt(checkpoint, manifest=manifest)
        _write_protected_document(
            protected_checkpoint,
            checkpoint,
            create=True,
            exists_check="retained_evidence_exists",
            write_check="retained_evidence_file",
            parent_check="retained_evidence_parent",
        )
    return _control_manifest_bind_retained_evidence_locked(
        path,
        receipt_path=str(protected_checkpoint),
        now=lambda: current,
    )


@_serialized_control_manifest_write
def control_manifest_init(
    path: Path,
    *,
    acceptance_run_id: str,
    candidate_sha: str,
    aws_account_id: str,
    aws_region: str,
    scenario_a_inventory: str,
    scenario_b_inventory: str,
    scenario_a_tf_binding: str,
    scenario_b_tf_binding: str,
    evidence_destination_sha256: str,
    gate_ledger: str,
    teardown_deadline_utc: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Create one fresh, closed, interruption-safe Plan 12 control manifest."""

    scenario_a_document = _validate_scenario_inventory(
        _read_protected_document(Path(scenario_a_inventory), check="scenario_inventory_file"),
        scenario_id="A",
        acceptance_run_id=acceptance_run_id,
        candidate_sha=candidate_sha,
        aws_account_id=aws_account_id,
        aws_region=aws_region,
        tf_binding_sha256=scenario_a_tf_binding,
        expected_phase="preapply",
    )
    scenario_b_document = _validate_scenario_inventory(
        _read_protected_document(Path(scenario_b_inventory), check="scenario_inventory_file"),
        scenario_id="B",
        acceptance_run_id=acceptance_run_id,
        candidate_sha=candidate_sha,
        aws_account_id=aws_account_id,
        aws_region=aws_region,
        tf_binding_sha256=scenario_b_tf_binding,
        expected_phase="preapply",
    )
    _validate_scenario_inventory_isolation(scenario_a_document, scenario_b_document)
    scenario_a_values = cast(dict[str, object], scenario_a_document["values"])
    scenario_b_values = cast(dict[str, object], scenario_b_document["values"])
    scenario_a_binding_path = cast(str, scenario_a_values["SCENARIO_TF_BINDING_FILE"])
    scenario_b_binding_path = cast(str, scenario_b_values["SCENARIO_TF_BINDING_FILE"])
    _, scenario_a_state_identity = _validate_tf_binding_receipt(
        Path(scenario_a_binding_path),
        scenario_id="A",
        acceptance_run_id=acceptance_run_id,
        aws_account_id=aws_account_id,
        aws_region=aws_region,
        expected_sha256=scenario_a_tf_binding,
    )
    _, scenario_b_state_identity = _validate_tf_binding_receipt(
        Path(scenario_b_binding_path),
        scenario_id="B",
        acceptance_run_id=acceptance_run_id,
        aws_account_id=aws_account_id,
        aws_region=aws_region,
        expected_sha256=scenario_b_tf_binding,
    )
    if scenario_a_state_identity == scenario_b_state_identity:
        raise AcceptanceCheckError("tf_binding_binding")
    timestamp = _utc_timestamp(now())
    manifest: dict[str, object] = {
        "schema": "elspeth.aws-ecs-control-manifest.v5",
        "acceptance_run_id": acceptance_run_id,
        "candidate_sha": candidate_sha,
        "aws": {"account_id": aws_account_id, "region": aws_region},
        "scenarios": {
            "A": {
                "preapply_inventory_path": scenario_a_inventory,
                "preapply_inventory_sha256": _scenario_inventory_hash(scenario_a_document),
                "inventory_path": scenario_a_inventory,
                "inventory_sha256": _scenario_inventory_hash(scenario_a_document),
                "inventory_phase": "preapply",
                "tf_binding_sha256": scenario_a_tf_binding,
                "tf_binding_path": scenario_a_binding_path,
                "tf_state_identity_sha256": scenario_a_state_identity,
                "terraform_plan_receipt": None,
                "terraform_applied": False,
                "terraform_noop_receipt": None,
            },
            "B": {
                "preapply_inventory_path": scenario_b_inventory,
                "preapply_inventory_sha256": _scenario_inventory_hash(scenario_b_document),
                "inventory_path": scenario_b_inventory,
                "inventory_sha256": _scenario_inventory_hash(scenario_b_document),
                "inventory_phase": "preapply",
                "tf_binding_sha256": scenario_b_tf_binding,
                "tf_binding_path": scenario_b_binding_path,
                "tf_state_identity_sha256": scenario_b_state_identity,
                "terraform_plan_receipt": None,
                "terraform_applied": False,
                "terraform_noop_receipt": None,
            },
        },
        "gate_ledger_path": gate_ledger,
        "teardown_deadline_utc": teardown_deadline_utc,
        "emergency_cleanup_deadline_utc": None,
        "cleanup_required": False,
        "cleanup_states": dict.fromkeys(_CLEANUP_SURFACES, "pending"),
        "ecr": {
            "registry": None,
            "repository": None,
            "baseline_tag": None,
            "candidate_tag": None,
            "baseline_digest": None,
            "candidate_digest": None,
        },
        "evidence": {
            "acceptance_state_path": None,
            "oidc_evidence_dir": None,
            "destination_sha256": evidence_destination_sha256,
            "export_receipt_path": None,
            "export_receipt_sha256": None,
            "final_export_receipt_path": None,
            "final_export_receipt_sha256": None,
            "retained_evidence_path": None,
            "retained_evidence_sha256": None,
            "receipts": [],
            "approvals": [],
        },
        "verdict_failures": [],
        "cleanup_escalations": [],
        "deadline_failure_recorded": False,
        "final_evidence": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _validate_control_manifest(manifest)
    _require_mutable_control_manifest(manifest)
    if _control_timestamp(teardown_deadline_utc) <= now():
        raise AcceptanceCheckError("control_manifest_expired")
    _write_protected_document(
        path,
        manifest,
        create=True,
        exists_check="control_manifest_exists",
        write_check="control_manifest_write",
    )
    return manifest


@_serialized_control_manifest_write
def control_manifest_bind_scenario(
    path: Path,
    *,
    scenario_id: str,
    inventory_path: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Atomically replace a pre-apply inventory with its immutable resolved inventory."""

    if scenario_id not in {"A", "B"}:
        raise AcceptanceCheckError("scenario_inventory_binding")
    manifest = _read_control_manifest(path)
    _require_mutable_control_manifest(manifest)
    scenarios = cast(dict[str, object], manifest["scenarios"])
    aws = cast(dict[str, object], manifest["aws"])
    scenario = cast(dict[str, object], scenarios[scenario_id])
    resolved_path = _control_path(inventory_path)
    inventory = _validate_scenario_inventory(
        _read_protected_document(Path(resolved_path), check="scenario_inventory_file"),
        scenario_id=scenario_id,
        acceptance_run_id=cast(str, manifest["acceptance_run_id"]),
        candidate_sha=cast(str, manifest["candidate_sha"]),
        aws_account_id=cast(str, aws["account_id"]),
        aws_region=cast(str, aws["region"]),
        tf_binding_sha256=cast(str, scenario["tf_binding_sha256"]),
        expected_phase="resolved",
    )
    inventory_sha256 = _scenario_inventory_hash(inventory)
    if scenario["inventory_phase"] == "resolved":
        if scenario["inventory_path"] == resolved_path and scenario["inventory_sha256"] == inventory_sha256:
            return manifest
        raise AcceptanceCheckError("scenario_inventory_conflict")
    if scenario["inventory_phase"] != "preapply" or scenario["inventory_path"] == resolved_path:
        raise AcceptanceCheckError("scenario_inventory_conflict")
    if now() >= _control_timestamp(manifest["teardown_deadline_utc"]):
        raise AcceptanceCheckError("control_manifest_expired")
    if (
        scenario["terraform_plan_receipt"] is None
        or scenario["terraform_applied"] is not True
        or scenario["terraform_noop_receipt"] is None
    ):
        raise AcceptanceCheckError("scenario_inventory_unresolved")
    preapply = _load_preapply_scenario_inventory(manifest, scenario_id)
    preapply_values = cast(dict[str, object], preapply["values"])
    preapply_orphan = cast(dict[str, object], preapply["orphan_sweep"])
    values = cast(dict[str, object], inventory["values"])
    orphan = cast(dict[str, object], inventory["orphan_sweep"])
    if any(values[field] != preapply_values[field] for field in _SCENARIO_VALUE_FIELDS - _RESOLVED_SCENARIO_FIELDS):
        raise AcceptanceCheckError("scenario_inventory_conflict")
    if any(orphan[field] != preapply_orphan[field] for field in _ORPHAN_INVENTORY_FIELDS - _PROVIDER_GENERATED_ORPHAN_FIELDS):
        raise AcceptanceCheckError("scenario_inventory_conflict")
    if values["SCENARIO_TF_BINDING_FILE"] != scenario["tf_binding_path"]:
        raise AcceptanceCheckError("tf_binding_binding")
    _, state_identity = _validate_tf_binding_receipt(
        Path(cast(str, scenario["tf_binding_path"])),
        scenario_id=scenario_id,
        acceptance_run_id=cast(str, manifest["acceptance_run_id"]),
        aws_account_id=cast(str, aws["account_id"]),
        aws_region=cast(str, aws["region"]),
        expected_sha256=cast(str, scenario["tf_binding_sha256"]),
    )
    if state_identity != scenario["tf_state_identity_sha256"]:
        raise AcceptanceCheckError("tf_binding_binding")
    other_id = "B" if scenario_id == "A" else "A"
    other_inventory = _load_bound_scenario_inventory(manifest, other_id)
    _validate_scenario_inventory_isolation(
        inventory if scenario_id == "A" else other_inventory, other_inventory if scenario_id == "A" else inventory
    )
    scenario["inventory_path"] = resolved_path
    scenario["inventory_sha256"] = inventory_sha256
    scenario["inventory_phase"] = "resolved"
    manifest["updated_at"] = _utc_timestamp(now())
    _validate_control_manifest(manifest)
    _write_protected_document(
        path,
        manifest,
        create=False,
        exists_check="control_manifest_exists",
        write_check="control_manifest_file",
    )
    return manifest


def control_manifest_get(path: Path, field: str) -> str:
    value: object = _read_control_manifest(path)
    if not field or len(field) > 256:
        raise AcceptanceCheckError("control_manifest_field")
    for segment in field.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            raise AcceptanceCheckError("control_manifest_field")
        value = value[segment]
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, (str, int)):
        return str(value)
    raise AcceptanceCheckError("control_manifest_field")
