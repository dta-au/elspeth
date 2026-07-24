"""Closed evidence projection and receipt verification."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .contracts import (
    _EVIDENCE_KINDS,
    _SHA256_PATTERN,
    MAX_JSON_RESPONSE_BYTES,
    AcceptanceCheckError,
    AcceptanceStateError,
    _control_timestamp,
    _sha256,
    _utc_timestamp,
)
from .gate_ledger import _gate_ledger_records_hash, _read_gate_ledger
from .manifest_schema import _read_control_manifest
from .receipt_contracts import _validate_stored_receipt
from .secure_documents import _read_protected_document, _write_protected_document
from .state import _parse_state_timestamp

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
