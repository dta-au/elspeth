"""Atomic persistence for sanitized AWS ECS acceptance receipts."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .contracts import MAX_CONTROL_DOCUMENT_BYTES, AcceptanceCheckError, _control_timestamp, _sha256, _utc_timestamp
from .manifest_schema import (
    _INFRASTRUCTURE_APPROVAL_SCOPES,
    _read_control_manifest,
    _require_mutable_control_manifest,
    _validate_control_manifest,
)
from .receipt_contracts import _RECEIPT_KINDS, _TERRAFORM_RECEIPT_KINDS, _validate_stored_receipt
from .scenario_inventory import _load_bound_scenario_inventory
from .secure_documents import _read_protected_document, _receipt_manifest_write_lock, _write_protected_document


def receipt_store(
    manifest_path: Path,
    *,
    scenario_id: str,
    kind: str,
    subject_id: str,
    receipt_file: Path | None = None,
    receipt_bytes: bytes | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    """Persist one receipt while serializing its manifest state transition."""

    with _receipt_manifest_write_lock(manifest_path, check="receipt_store_write"):
        return _receipt_store_locked(
            manifest_path,
            scenario_id=scenario_id,
            kind=kind,
            subject_id=subject_id,
            receipt_file=receipt_file,
            receipt_bytes=receipt_bytes,
            now=now,
        )


def _receipt_store_locked(
    manifest_path: Path,
    *,
    scenario_id: str,
    kind: str,
    subject_id: str,
    receipt_file: Path | None = None,
    receipt_bytes: bytes | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    """Persist one bounded sanitized receipt while the manifest lock is held."""

    if (
        scenario_id not in _INFRASTRUCTURE_APPROVAL_SCOPES
        or kind not in _RECEIPT_KINDS
        or (scenario_id == "bootstrap" and kind not in _TERRAFORM_RECEIPT_KINDS)
    ):
        raise AcceptanceCheckError("receipt_store_binding")
    if (
        type(subject_id) is not str
        or not subject_id
        or len(subject_id) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in subject_id)
    ):
        raise AcceptanceCheckError("receipt_store_binding")
    if (receipt_file is None) == (receipt_bytes is None):
        raise AcceptanceCheckError("receipt_store_input")
    if receipt_file is not None:
        document = _read_protected_document(receipt_file, check="receipt_store_file")
    else:
        assert receipt_bytes is not None
        if len(receipt_bytes) > MAX_CONTROL_DOCUMENT_BYTES:
            raise AcceptanceCheckError("receipt_store_file")
        try:
            decoded = json.loads(receipt_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AcceptanceCheckError("receipt_store_schema") from None
        if not isinstance(decoded, dict):
            raise AcceptanceCheckError("receipt_store_schema")
        document = decoded
    subject_sha256 = _sha256(subject_id.encode("utf-8"))
    manifest = _read_control_manifest(manifest_path)
    _require_mutable_control_manifest(manifest)
    candidate_sha = manifest["candidate_sha"]
    assert isinstance(candidate_sha, str)
    expected_plugin_policy_binding_sha256: str | None = None
    expected_acceptance_run_id_sha256: str | None = None
    expected_cluster_id_sha256: str | None = None
    if kind == "connection-budget":
        inventory = _load_bound_scenario_inventory(manifest, scenario_id, require_resolved=True)
        values = inventory["values"]
        acceptance_run_id = manifest["acceptance_run_id"]
        assert isinstance(values, dict) and isinstance(acceptance_run_id, str)
        cluster_id = values["DB_CLUSTER_IDENTIFIER"]
        assert isinstance(cluster_id, str)
        expected_acceptance_run_id_sha256 = _sha256(acceptance_run_id.encode("utf-8"))
        expected_cluster_id_sha256 = _sha256(cluster_id.encode("utf-8"))
    if kind == "verify-bedrock-guardrails":
        inventory = _load_bound_scenario_inventory(manifest, scenario_id, require_resolved=True)
        values = inventory["values"]
        assert isinstance(values, dict)
        binding = values["ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256"]
        assert isinstance(binding, str)
        expected_plugin_policy_binding_sha256 = binding
    if kind == "compatibility-record":
        inventory = _load_bound_scenario_inventory(manifest, scenario_id, require_resolved=True)
        values = inventory["values"]
        ecr = manifest["ecr"]
        if not isinstance(values, dict) or not isinstance(ecr, dict) or not isinstance(document, Mapping):
            raise AcceptanceCheckError("receipt_store_binding")
        previous = values["PREVIOUS_TASK_DEFINITION"] if scenario_id == "B" else ""
        rollback_doctor = values["ROLLBACK_DOCTOR_TASK_DEFINITION"] if scenario_id == "B" else ""
        baseline_tag = ecr["baseline_tag"] if scenario_id == "B" else ""
        baseline_match = re.search(r"baseline-([0-9a-f]{40})$", cast(str, baseline_tag)) if baseline_tag else None
        previous_source_sha = baseline_match.group(1) if baseline_match is not None else None
        if (
            document.get("acceptance_run_id_sha256") != _sha256(cast(str, manifest["acceptance_run_id"]).encode())
            or document.get("candidate_image_digest") != ecr["candidate_digest"]
            or document.get("candidate_task_definition_sha256") != _sha256(cast(str, values["CANDIDATE_TASK_DEFINITION"]).encode())
            or document.get("candidate_doctor_task_definition_sha256") != _sha256(cast(str, values["DOCTOR_TASK_DEFINITION"]).encode())
            or document.get("previous_source_sha") != previous_source_sha
            or document.get("previous_image_digest") != (ecr["baseline_digest"] if scenario_id == "B" else None)
            or document.get("previous_task_definition_sha256") != (_sha256(cast(str, previous).encode()) if previous else None)
            or document.get("rollback_doctor_task_definition_sha256")
            != (_sha256(cast(str, rollback_doctor).encode()) if rollback_doctor else None)
            or _control_timestamp(document.get("expires_at")) <= now()
        ):
            raise AcceptanceCheckError("receipt_store_binding")
    document = _validate_stored_receipt(
        document,
        kind=kind,
        scenario_id=scenario_id,
        subject_sha256=subject_sha256,
        candidate_sha=candidate_sha,
        subject_id=subject_id,
        expected_plugin_policy_binding_sha256=expected_plugin_policy_binding_sha256,
        expected_acceptance_run_id_sha256=expected_acceptance_run_id_sha256,
        expected_cluster_id_sha256=expected_cluster_id_sha256,
    )
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    receipt_sha256 = _sha256(canonical)
    receipt_directory = manifest_path.parent / f"{manifest_path.name}.receipts"
    try:
        receipt_directory.mkdir(mode=0o700, exist_ok=True)
        directory_stat = receipt_directory.lstat()
    except OSError:
        raise AcceptanceCheckError("receipt_store_write") from None
    if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != os.getuid() or directory_stat.st_mode & 0o077:
        raise AcceptanceCheckError("receipt_store_write")
    stored_path = receipt_directory / f"{receipt_sha256}.json"
    if stored_path.exists():
        existing = _validate_stored_receipt(
            _read_protected_document(stored_path, check="receipt_store_file"),
            kind=kind,
            scenario_id=scenario_id,
            subject_sha256=subject_sha256,
            candidate_sha=candidate_sha,
            subject_id=subject_id,
            expected_plugin_policy_binding_sha256=expected_plugin_policy_binding_sha256,
            expected_acceptance_run_id_sha256=expected_acceptance_run_id_sha256,
            expected_cluster_id_sha256=expected_cluster_id_sha256,
        )
        if existing != document:
            raise AcceptanceCheckError("receipt_store_conflict")
    else:
        _write_protected_document(
            stored_path,
            document,
            create=True,
            exists_check="receipt_store_conflict",
            write_check="receipt_store_write",
            parent_check="receipt_store_write",
        )
    evidence = manifest["evidence"]
    assert isinstance(evidence, dict)
    receipts = evidence["receipts"]
    assert isinstance(receipts, list)
    record = {
        "scenario_id": scenario_id,
        "kind": kind,
        "subject_sha256": subject_sha256,
        "receipt_sha256": receipt_sha256,
        "stored_at": _utc_timestamp(now()),
    }
    matches = [
        item
        for item in receipts
        if isinstance(item, dict)
        and item.get("scenario_id") == scenario_id
        and item.get("kind") == kind
        and item.get("subject_sha256") == subject_sha256
    ]
    if matches:
        comparable = {**record, "stored_at": matches[0].get("stored_at")}
        if matches != [comparable]:
            raise AcceptanceCheckError("receipt_store_conflict")
        return receipt_sha256
    receipts.append(record)
    manifest["updated_at"] = record["stored_at"]
    _validate_control_manifest(manifest)
    _write_protected_document(
        manifest_path,
        manifest,
        create=False,
        exists_check="control_manifest_exists",
        write_check="control_manifest_file",
    )
    return receipt_sha256
