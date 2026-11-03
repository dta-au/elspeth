"""END SDK-capable H, preserving the exact historical checkpoint v2 recipe."""

from dataclasses import replace
from typing import Literal

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
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
) -> CheckpointTerminalHoldPlan:
    legacy = prepare_checkpoint_terminal_hold_plan(
        identity=identity, admitted=admitted, phase=phase, creator_sequence=creator_sequence, requested_model=requested_model
    )
    if historical_contract_sha256 == legacy.contract_sha256:
        return legacy
    if phase == "early":
        if historical_contract_sha256 is not None:
            raise AuditIntegrityError("EARLY checkpoint refuses an uninstalled SDK hold recipe")
        return legacy
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
