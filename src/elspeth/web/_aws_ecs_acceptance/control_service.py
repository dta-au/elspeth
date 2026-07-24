"""High-level AWS ECS acceptance control-manifest services."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from .approvals import _require_current_approval
from .contracts import (
    _EMERGENCY_CLEANUP_SECONDS,
    _SHA256_PATTERN,
    AcceptanceCheckError,
    _control_timestamp,
    _sha256,
    _utc_timestamp,
)
from .evidence import (
    _reverify_bound_evidence_export_receipt,
    _validate_evidence_export_receipt,
    _verify_final_cleanup_receipt,
    _verify_stored_receipts,
)
from .gate_ledger import _gate_ledger_records_hash, _read_gate_ledger
from .manifest_schema import (
    _read_control_manifest,
    _require_mutable_control_manifest,
    _validate_control_manifest,
)
from .receipt_contracts import (
    _CANDIDATE_PACKAGE_VERSION,
    _ROLLBACK_PACKAGE_VERSION,
    _expected_schema_facts,
)
from .scenario_inventory import (
    SCENARIO_ASSIGNMENT_NAMES,
    _control_path,
    _load_bound_scenario_inventory,
)
from .secure_documents import (
    _read_protected_document,
    _serialized_control_manifest_write,
    _write_protected_document,
)

_APPLICATION_SCENARIO_IDS = frozenset({"A", "B"})


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
