"""Two-phase AWS ECS acceptance cleanup orchestration."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from .contracts import AcceptanceCheckError, _sha256, _utc_timestamp
from .evidence import (
    _final_cleanup_receipt_document,
    _validate_evidence_export_receipt,
    _verify_final_cleanup_receipt,
    _verify_stored_receipts,
)
from .gate_ledger import (
    _CLEANUP_GATE_CHECK_ORDER,
    _TERMINAL_GATE_CHECK_ID,
    _gate_ledger_record_cleanup_bound,
    _gate_ledger_records_hash,
    _read_gate_ledger,
)
from .manifest_schema import _read_control_manifest, _validate_control_manifest
from .secure_documents import (
    _read_protected_document,
    _serialized_control_manifest_write,
    _write_protected_document,
)


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


def _validate_committed_cleanup_replay(
    manifest_path: Path,
    manifest: Mapping[str, object],
    ledger: Mapping[str, object],
    *,
    receipts_sha256: str,
) -> bool:
    final_evidence = manifest["final_evidence"]
    if final_evidence is None:
        return False
    final_evidence_record = cast(dict[str, object], final_evidence)
    if final_evidence_record["phase"] != "committed" or manifest["cleanup_required"] is not False:
        return False
    ledger_sha256 = _gate_ledger_records_hash(ledger)
    if (
        final_evidence_record["ledger_sha256"] != ledger_sha256
        or final_evidence_record["receipts_sha256"] != receipts_sha256
        or type(final_evidence_record["committed_at"]) is not str
    ):
        raise AcceptanceCheckError("cleanup_finalize_conflict")
    _verify_final_cleanup_receipt(manifest_path, manifest)
    return True


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
    cleanup_records = cast(list[object], ledger["cleanup_records"])
    cleanup_record_items = [cast(dict[str, object], record) for record in cleanup_records]
    terminal_records = [record for record in cleanup_record_items if record["check_id"] == _TERMINAL_GATE_CHECK_ID]
    if len(terminal_records) > 1:
        raise AcceptanceCheckError("gate_ledger_conflict")
    prefix_records = [record for record in cleanup_record_items if record["check_id"] != _TERMINAL_GATE_CHECK_ID]
    if [record["check_id"] for record in prefix_records] != list(_CLEANUP_GATE_CHECK_ORDER[:-1]):
        raise AcceptanceCheckError("gate_ledger_incomplete")
    receipt_count, receipts_sha256 = _verify_stored_receipts(manifest_path, manifest)
    ledger_records_sha256 = _gate_ledger_records_hash({**ledger, "cleanup_records": prefix_records})
    evidence = cast(Mapping[str, object], manifest["evidence"])
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
    candidate_sha = cast(str, manifest["candidate_sha"])
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
        if clear_cleanup_required:
            raise AcceptanceCheckError("cleanup_finalize_phase")
        if _validate_committed_cleanup_replay(
            manifest_path,
            manifest,
            ledger,
            receipts_sha256=receipts_sha256,
        ):
            return manifest
        if ledger["finalized"] is not None:
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
        if existing is not None:
            existing_record = cast(dict[str, object], existing)
            comparable = {**prepared, "prepared_at": existing_record["prepared_at"]}
            if existing_record != comparable:
                raise AcceptanceCheckError("cleanup_finalize_conflict")
            if terminal_records:
                terminal = terminal_records[0]
                if (
                    terminal["candidate_sha"] != candidate_sha
                    or terminal["exit_status"] != 0
                    or terminal["receipt_hash"] != terminal_receipt_hash
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
    if _validate_committed_cleanup_replay(
        manifest_path,
        manifest,
        ledger,
        receipts_sha256=receipts_sha256,
    ):
        return manifest
    if manifest["cleanup_required"] is not True or final_evidence is None:
        raise AcceptanceCheckError("cleanup_finalize_pending")
    final_evidence = cast(dict[str, object], final_evidence)
    if final_evidence["phase"] != "prepared":
        raise AcceptanceCheckError("cleanup_finalize_pending")
    if (
        final_evidence["receipt_count"] != receipt_count
        or final_evidence["receipts_sha256"] != receipts_sha256
        or final_evidence["ledger_records_sha256"] != ledger_records_sha256
    ):
        raise AcceptanceCheckError("cleanup_finalize_conflict")
    cleanup_states = cast(dict[str, object], manifest["cleanup_states"])
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
    try:
        ledger = _gate_ledger_record_cleanup_bound(
            ledger_path,
            check_id=_TERMINAL_GATE_CHECK_ID,
            exit_status=0,
            receipt_hash=terminal_receipt_hash,
            candidate_sha=candidate_sha,
            expected_prefix_records_sha256=ledger_records_sha256,
            started_at=timestamp,
            ended_at=timestamp,
            now=now,
        )
    except AcceptanceCheckError as exc:
        if exc.check == "gate_ledger_conflict":
            raise AcceptanceCheckError("cleanup_finalize_conflict") from None
        raise
    cleanup_records = cast(list[object], ledger["cleanup_records"])
    terminal = cast(dict[str, object], cleanup_records[-1])
    if terminal["check_id"] != _TERMINAL_GATE_CHECK_ID:
        raise AcceptanceCheckError("cleanup_finalize_conflict")
    committed_at = cast(str, terminal["ended_at"])
    ledger_sha256 = _gate_ledger_records_hash(ledger)
    precommit_manifest_sha256 = _sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    final_evidence.update(
        {
            "phase": "committed",
            "precommit_manifest_sha256": precommit_manifest_sha256,
            "committed_at": committed_at,
            "ledger_sha256": ledger_sha256,
        }
    )
    manifest["cleanup_required"] = False
    manifest["updated_at"] = committed_at
    _validate_control_manifest(manifest)
    _ensure_final_cleanup_receipt(
        manifest_path,
        manifest,
        ledger_sha256=ledger_sha256,
        receipts_sha256=receipts_sha256,
        committed_at=committed_at,
    )
    _write_protected_document(
        manifest_path,
        manifest,
        create=False,
        exists_check="control_manifest_exists",
        write_check="control_manifest_file",
    )
    final_manifest = _read_control_manifest(manifest_path)
    _verify_final_cleanup_receipt(manifest_path, final_manifest)
    return final_manifest
