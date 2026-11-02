"""One pre-dispatch bound for every ordinary PRIMARY V4 terminal alternative."""

from hashlib import sha256

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.sessions.continuation_primary_paid_incomplete_tail import (
    PrimaryPaidIncompleteTailPlan,
    prepare_primary_paid_incomplete_tail_plan,
)
from elspeth.web.sessions.continuation_primary_paid_overflow_no_sdk import PrimaryNoSdkTailPlan
from elspeth.web.sessions.continuation_primary_paid_overflow_tail import PrimaryPaidOverflowTailPlan


def primary_terminal_reservation(
    paid: PrimaryPaidOverflowTailPlan, no_sdk: PrimaryNoSdkTailPlan
) -> tuple[PrimaryPaidIncompleteTailPlan, int, str]:
    """Bind max(overflow, incomplete, no-SDK) to the same original attempt/head.

    Complete results retain their own exact batch admission; when they cannot
    fit, this already-reserved terminal remains available without a new hold.
    """
    if type(paid) is not PrimaryPaidOverflowTailPlan or type(no_sdk) is not PrimaryNoSdkTailPlan:
        raise TypeError("PRIMARY terminal reserve requires its nominal paid and no-SDK plans")
    if paid.admitted != no_sdk.admitted or paid.identity.attempt_id != no_sdk.attempt_id:
        raise AuditIntegrityError("PRIMARY terminal reserve crossed its original attempt or admitted head")
    incomplete = prepare_primary_paid_incomplete_tail_plan(
        identity=paid.identity, admitted=paid.admitted, creator_sequence=paid.creator_sequence
    )
    reserved = max(paid.reserved_bytes, incomplete.reserved_bytes, no_sdk.reserved_bytes)
    contract = sha256(
        canonical_json(
            {
                "version": "composer.primary-v4-combined-tail.v2",
                "paid": paid.fallback_contract_sha256,
                "incomplete": incomplete.fallback_contract_sha256,
                "no_sdk": no_sdk.contract_sha256,
                "reserved_bytes": reserved,
            }
        ).encode("utf-8")
    ).hexdigest()
    return incomplete, reserved, contract
