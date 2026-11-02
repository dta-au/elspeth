"""One pre-SDK H binding both checkpoint terminal alternatives."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_checkpoint_no_sdk_tail import CheckpointNoSdkTailPlan, prepare_checkpoint_no_sdk_tail_plan
from elspeth.web.sessions.continuation_checkpoint_paid_tail import (
    CheckpointPaidOverflowTailPlan,
    prepare_checkpoint_paid_overflow_tail_plan,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity


@dataclass(frozen=True, slots=True)
class CheckpointTerminalHoldPlan:
    paid_overflow: CheckpointPaidOverflowTailPlan
    no_sdk: CheckpointNoSdkTailPlan
    reserved_bytes: int
    contract_sha256: str


def prepare_checkpoint_terminal_hold_plan(
    *,
    identity: PaidOverflowIdentity,
    admitted: StoredContinuationSnapshot,
    phase: Literal["early", "end"],
    creator_sequence: int,
    requested_model: str,
) -> CheckpointTerminalHoldPlan:
    """Reserve the larger complete batch; bind both exact shape contracts."""
    if phase not in ("early", "end"):
        raise AuditIntegrityError("checkpoint H requires an original EARLY/END phase")
    paid = prepare_checkpoint_paid_overflow_tail_plan(identity=identity, admitted=admitted, phase=phase, creator_sequence=creator_sequence)
    no_sdk = prepare_checkpoint_no_sdk_tail_plan(
        identity=identity, admitted=admitted, phase=phase, creator_sequence=creator_sequence, requested_model=requested_model
    )
    reserved = max(paid.reserved_bytes, no_sdk.reserved_bytes)
    contract = sha256(
        canonical_json(
            {
                "owner": "composer_checkpoint_terminal_hold",
                "version": 1,
                "paid_overflow_contract_sha256": paid.fallback_contract_sha256,
                "paid_overflow_reserved_bytes": paid.reserved_bytes,
                "no_sdk_contract_sha256": no_sdk.contract_sha256,
                "no_sdk_reserved_bytes": no_sdk.reserved_bytes,
                "reserved_bytes": reserved,
            }
        ).encode("utf-8")
    ).hexdigest()
    return CheckpointTerminalHoldPlan(paid, no_sdk, reserved, contract)
