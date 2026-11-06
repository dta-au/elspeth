"""Versioned checkpoint SDK holds with exact historical recipe preservation."""

from dataclasses import replace
from hashlib import sha256
from typing import Literal

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_checkpoint_early_source import CheckpointEarlyInput, encode_checkpoint_early_input
from elspeth.web.sessions.continuation_checkpoint_terminal_hold import CheckpointTerminalHoldPlan, prepare_checkpoint_terminal_hold_plan
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_sdk_reservation import extend_sdk_terminal_reservation
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily, prepare_sdk_tail_plan


def prepare_checkpoint_sdk_terminal_hold_plan(
    *,
    identity: PaidOverflowIdentity,
    admitted: StoredContinuationSnapshot,
    phase: Literal["early", "end"],
    creator_sequence: int,
    requested_model: str,
    historical_contract_sha256: str | None = None,
    early_input: CheckpointEarlyInput | None = None,
) -> CheckpointTerminalHoldPlan:
    if phase == "end" and early_input is not None:
        raise AuditIntegrityError("END checkpoint cannot borrow an EARLY request commitment")
    legacy = prepare_checkpoint_terminal_hold_plan(
        identity=identity, admitted=admitted, phase=phase, creator_sequence=creator_sequence, requested_model=requested_model
    )
    if historical_contract_sha256 == legacy.contract_sha256:
        return legacy
    if phase == "early":
        if early_input is None:
            if historical_contract_sha256 is not None:
                raise AuditIntegrityError("historical EARLY checkpoint lacks its original SDK request commitment")
            return legacy
        if (
            type(early_input) is not CheckpointEarlyInput
            or early_input.session_id != identity.session_id
            or early_input.operation_id != identity.operation_id
            or early_input.requested_model != requested_model
        ):
            raise AuditIntegrityError("EARLY SDK hold changed its nominal original request input")
        early_plan = prepare_sdk_tail_plan(
            identity=identity, admitted=admitted, creator_sequence=creator_sequence, family=SdkTerminalFamily.CHECKPOINT_EARLY
        )
        reserved = max(legacy.reserved_bytes, early_plan.reserved_bytes)
        contract = sha256(
            canonical_json(
                {
                    "version": "composer.checkpoint-terminal-hold.v4",
                    "previous_contract": legacy.contract_sha256,
                    "sdk_terminal": early_plan.contract_sha256,
                    "early_input": encode_checkpoint_early_input(early_input).reference.model_dump(mode="json"),
                    "reserved_bytes": reserved,
                }
            ).encode("utf-8")
        ).hexdigest()
        if historical_contract_sha256 is not None and historical_contract_sha256 != contract:
            raise AuditIntegrityError("EARLY SDK reservation refuses an unknown historical hold recipe")
        return replace(legacy, reserved_bytes=reserved, contract_sha256=contract)
    plan = prepare_sdk_tail_plan(
        identity=identity, admitted=admitted, creator_sequence=creator_sequence, family=SdkTerminalFamily.CHECKPOINT_END
    )
    reserved, contract = extend_sdk_terminal_reservation(
        legacy_reserved_bytes=legacy.reserved_bytes,
        legacy_contract_sha256=legacy.contract_sha256,
        plan=plan,
        version="composer.checkpoint-terminal-hold.v3",
        historical_contract_sha256=historical_contract_sha256,
    )
    return replace(legacy, reserved_bytes=reserved, contract_sha256=contract)
