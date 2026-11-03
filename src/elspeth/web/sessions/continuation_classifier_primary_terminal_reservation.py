"""One classifier V4 predispatch H recipe shared by every terminal reader."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, is_lower_sha256_hex

if TYPE_CHECKING:
    from elspeth.web.sessions.continuation_classifier_primary_incomplete_tail import ClassifierPaidIncompleteTailPlan
    from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkTailPlan
    from elspeth.web.sessions.continuation_classifier_primary_paid_tail import ClassifierPaidOverflowTailPlan


@dataclass(frozen=True, slots=True)
class ClassifierTerminalReservation:
    reserved_bytes: int
    contract_sha256: str


def classifier_terminal_reservation(
    *,
    paid_contract: str,
    paid_bytes: int,
    incomplete_contract: str,
    incomplete_bytes: int,
    no_sdk_contract: str,
    no_sdk_bytes: int,
) -> ClassifierTerminalReservation:
    """Bind three closed tails to the maximum, without granting dispatch."""
    if any(not is_lower_sha256_hex(value) for value in (paid_contract, incomplete_contract, no_sdk_contract)):
        raise ValueError("classifier terminal reserve needs three exact tail contracts")
    if any(type(value) is not int or value <= 0 for value in (paid_bytes, incomplete_bytes, no_sdk_bytes)):
        raise ValueError("classifier terminal reserve needs three positive byte bounds")
    reserved = max(paid_bytes, incomplete_bytes, no_sdk_bytes)
    contract = canonical_json(
        {
            "version": "composer.classifier-primary-v4-combined-tail.v2",
            "paid": paid_contract,
            "incomplete": incomplete_contract,
            "no_sdk": no_sdk_contract,
            "reserved_bytes": reserved,
        }
    )
    return ClassifierTerminalReservation(reserved, sha256(contract.encode("utf-8")).hexdigest())


def classifier_sdk_terminal_reservation(
    paid: ClassifierPaidOverflowTailPlan,
    incomplete: ClassifierPaidIncompleteTailPlan,
    no_sdk: ClassifierNoSdkTailPlan,
    *,
    historical_contract_sha256: str | None = None,
) -> tuple[int, str]:
    from elspeth.web.sessions.continuation_classifier_primary_incomplete_tail import ClassifierPaidIncompleteTailPlan
    from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkTailPlan
    from elspeth.web.sessions.continuation_classifier_primary_paid_tail import ClassifierPaidOverflowTailPlan
    from elspeth.web.sessions.continuation_sdk_reservation import extend_sdk_terminal_reservation
    from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily, prepare_sdk_tail_plan

    if (
        type(paid) is not ClassifierPaidOverflowTailPlan
        or type(incomplete) is not ClassifierPaidIncompleteTailPlan
        or type(no_sdk) is not ClassifierNoSdkTailPlan
        or paid.identity != incomplete.identity
        or paid.identity.attempt_id != no_sdk.attempt_id
        or paid.admitted != incomplete.admitted
        or paid.admitted != no_sdk.admitted
        or paid.creator_sequence != incomplete.creator_sequence
    ):
        raise AuditIntegrityError("classifier SDK H changed its original nominal tails")
    legacy = classifier_terminal_reservation(
        paid_contract=paid.fallback_contract_sha256,
        paid_bytes=paid.reserved_bytes,
        incomplete_contract=incomplete.fallback_contract_sha256,
        incomplete_bytes=incomplete.reserved_bytes,
        no_sdk_contract=no_sdk.contract_sha256,
        no_sdk_bytes=no_sdk.reserved_bytes,
    )
    plan = prepare_sdk_tail_plan(
        identity=paid.identity, admitted=paid.admitted, creator_sequence=paid.creator_sequence, family=SdkTerminalFamily.CLASSIFIER
    )
    return extend_sdk_terminal_reservation(
        legacy_reserved_bytes=legacy.reserved_bytes,
        legacy_contract_sha256=legacy.contract_sha256,
        plan=plan,
        version="composer.classifier-primary-v4-combined-tail.v3",
        historical_contract_sha256=historical_contract_sha256,
    )
