"""Runtime approval verification for AWS ECS acceptance operations."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .contracts import _SHA256_PATTERN, AcceptanceCheckError, _control_timestamp, _sha256, _utc_timestamp
from .manifest_schema import (
    _INFRASTRUCTURE_APPROVAL_SCOPES,
    _read_control_manifest,
    _require_mutable_control_manifest,
    _validate_control_manifest,
)
from .scenario_inventory import _control_path
from .secure_documents import _read_protected_document, _serialized_control_manifest_write, _write_protected_document


def _require_current_approval(
    approvals: list[object],
    *,
    scenario_id: str,
    kind: str,
    plan_receipt_sha256: str,
    approval_sha256: str,
    current: datetime,
) -> Mapping[str, object]:
    approval_records = cast(list[Mapping[str, object]], approvals)
    matches = [
        approval
        for approval in approval_records
        if approval["scenario_id"] == scenario_id
        and approval["kind"] == kind
        and approval["plan_receipt_sha256"] == plan_receipt_sha256
        and approval["approval_sha256"] == approval_sha256
    ]
    if len(matches) != 1:
        raise AcceptanceCheckError("control_manifest_update")
    approval = matches[0]
    if current >= _control_timestamp(approval["expires_at"]):
        raise AcceptanceCheckError("approval_expired")
    approval_path = cast(str, approval["approval_path"])
    expected_sha256 = cast(str, approval["approval_sha256"])
    document = _read_protected_document(Path(approval_path), check="approval_file")
    observed_sha256 = _sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if observed_sha256 != expected_sha256:
        raise AcceptanceCheckError("approval_binding")
    return approval


def _decode_approval_base64url(value: object, *, expected_bytes: int) -> bytes:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise AcceptanceCheckError("approval_verifier")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error):
        raise AcceptanceCheckError("approval_verifier") from None
    if len(decoded) != expected_bytes or base64.urlsafe_b64encode(decoded).decode().rstrip("=") != value:
        raise AcceptanceCheckError("approval_verifier")
    return decoded


def _configured_approval_signature_verifier(
    environ: Mapping[str, str],
) -> Callable[[bytes, str, str], bool]:
    keyring_value = environ.get("ELSPETH_ACCEPTANCE_APPROVAL_KEYRING")
    if not keyring_value:
        raise AcceptanceCheckError("approval_verifier")
    keyring = _read_protected_document(Path(keyring_value), check="approval_verifier")
    if set(keyring) != {"schema", "keys"} or keyring["schema"] != "elspeth.aws-ecs-approval-keyring.v1":
        raise AcceptanceCheckError("approval_verifier")
    keys = keyring["keys"]
    if not isinstance(keys, dict) or not 1 <= len(keys) <= 64:
        raise AcceptanceCheckError("approval_verifier")
    decoded_keys: dict[str, bytes] = {}
    for key_id, encoded_key in keys.items():
        if type(key_id) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", key_id) is None:
            raise AcceptanceCheckError("approval_verifier")
        decoded_keys[key_id] = _decode_approval_base64url(encoded_key, expected_bytes=32)

    def verify(payload: bytes, signature: str, key_id: str) -> bool:
        public_key_bytes = decoded_keys.get(key_id)
        if public_key_bytes is None:
            return False
        signature_bytes = _decode_approval_base64url(signature, expected_bytes=64)
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(signature_bytes, payload)
        except Exception:
            return False
        return True

    return verify


@_serialized_control_manifest_write
def approval_verify(
    manifest_path: Path,
    *,
    scenario_id: str,
    kind: str,
    plan_receipt_hash: str,
    approval_file: Path,
    signature_verifier: Callable[[bytes, str, str], bool] | None = None,
    environ: Mapping[str, str] = os.environ,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    """Verify a bound, unexpired explicit approval through an injected trust root."""

    if signature_verifier is None:
        signature_verifier = _configured_approval_signature_verifier(environ)
    if scenario_id not in _INFRASTRUCTURE_APPROVAL_SCOPES or kind not in {"terraform-plan", "terraform-destroy-plan"}:
        raise AcceptanceCheckError("approval_binding")
    if _SHA256_PATTERN.fullmatch(plan_receipt_hash) is None:
        raise AcceptanceCheckError("approval_binding")
    manifest = _read_control_manifest(manifest_path)
    _require_mutable_control_manifest(manifest)
    evidence = cast(dict[str, object], manifest["evidence"])
    receipts = cast(list[dict[str, object]], evidence["receipts"])
    if not any(
        receipt["scenario_id"] == scenario_id and receipt["kind"] == kind and receipt["receipt_sha256"] == plan_receipt_hash
        for receipt in receipts
    ):
        raise AcceptanceCheckError("approval_binding")
    approval = _read_protected_document(approval_file, check="approval_file")
    fields = {
        "schema",
        "acceptance_run_id",
        "scenario_id",
        "kind",
        "plan_receipt_hash",
        "approver_identity",
        "authority",
        "decision",
        "approved_at",
        "expires_at",
        "key_id",
        "signature",
    }
    if set(approval) != fields or approval["schema"] != "elspeth.aws-ecs-approval.v1":
        raise AcceptanceCheckError("approval_schema")
    expected_authority = "terraform-apply" if kind == "terraform-plan" else "terraform-destroy"
    if (
        approval["acceptance_run_id"] != manifest["acceptance_run_id"]
        or approval["scenario_id"] != scenario_id
        or approval["kind"] != kind
        or approval["plan_receipt_hash"] != plan_receipt_hash
        or approval["authority"] != expected_authority
        or approval["decision"] != "approved"
    ):
        raise AcceptanceCheckError("approval_binding")
    for field in ("approver_identity", "key_id", "signature"):
        value = approval[field]
        if (
            type(value) is not str
            or not value
            or len(value) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise AcceptanceCheckError("approval_schema")
    approved_at = _control_timestamp(approval["approved_at"])
    expires_at = _control_timestamp(approval["expires_at"])
    current = now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise AcceptanceCheckError("approval_schema")
    if not approved_at <= current < expires_at:
        raise AcceptanceCheckError("approval_expired")
    signed = {key: value for key, value in approval.items() if key != "signature"}
    canonical = json.dumps(signed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = cast(str, approval["signature"])
    key_id = cast(str, approval["key_id"])
    try:
        verified = signature_verifier(canonical, signature, key_id)
    except Exception:
        raise AcceptanceCheckError("approval_signature") from None
    if verified is not True:
        raise AcceptanceCheckError("approval_signature")
    approval_sha256 = _sha256(json.dumps(approval, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    approvals = cast(list[dict[str, object]], evidence["approvals"])
    record: dict[str, object] = {
        "scenario_id": scenario_id,
        "kind": kind,
        "plan_receipt_sha256": plan_receipt_hash,
        "approval_sha256": approval_sha256,
        "approval_path": _control_path(str(approval_file)),
        "expires_at": _utc_timestamp(expires_at),
        "verified_at": _utc_timestamp(current),
    }
    matches = [
        item
        for item in approvals
        if item["scenario_id"] == scenario_id and item["kind"] == kind and item["plan_receipt_sha256"] == plan_receipt_hash
    ]
    if matches:
        comparable = {**record, "verified_at": matches[0]["verified_at"]}
        if matches != [comparable]:
            raise AcceptanceCheckError("approval_conflict")
        return approval_sha256
    approvals.append(record)
    manifest["updated_at"] = record["verified_at"]
    _validate_control_manifest(manifest)
    _write_protected_document(
        manifest_path,
        manifest,
        create=False,
        exists_check="control_manifest_exists",
        write_check="control_manifest_file",
    )
    return approval_sha256


def approval_require_current(
    manifest_path: Path,
    *,
    scenario_id: str,
    kind: str,
    plan_receipt_hash: str,
    approval_hash: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Reopen a stored approval and require it to be current at point of use."""

    if (
        scenario_id not in _INFRASTRUCTURE_APPROVAL_SCOPES
        or kind not in {"terraform-plan", "terraform-destroy-plan"}
        or _SHA256_PATTERN.fullmatch(plan_receipt_hash) is None
        or _SHA256_PATTERN.fullmatch(approval_hash) is None
    ):
        raise AcceptanceCheckError("approval_binding")
    manifest = _read_control_manifest(manifest_path)
    evidence = cast(Mapping[str, object], manifest["evidence"])
    approvals = cast(list[object], evidence["approvals"])
    _require_current_approval(
        approvals,
        scenario_id=scenario_id,
        kind=kind,
        plan_receipt_sha256=plan_receipt_hash,
        approval_sha256=approval_hash,
        current=now(),
    )
