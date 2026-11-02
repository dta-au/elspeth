"""One classifier V4 predispatch H recipe shared by every terminal reader."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from elspeth.contracts.hashing import canonical_json, is_lower_sha256_hex


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
