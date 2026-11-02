"""Bounded PRIMARY SDK terminal DATA, without dispatch or settlement authority.

A receipt records an independently proved SDK observation. Constructing or
reading it does not prove entry, a provider failure, a charge, or SQL closure.
In particular, a retained terminal hold alone cannot supply those facts.
"""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from elspeth.contracts.bounded_serialization import utf8_size
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.sessions.continuation_paid_overflow import (
    PaidOverflowCost,
    PaidOverflowIdentity,
    PaidOverflowUsage,
    encode_paid_overflow_cost_amount,
)

# A hard wire allowance, not a measured result size or the entire terminal H.
# A future tail owner must reserve this receipt plus its snapshot/event bytes.
SDK_TERMINAL_RECEIPT_MAX_UTF8_BYTES = 8192

type SdkTerminalObservation = Literal["provider_failure", "provider_timeout", "no_observed_result"]


class SdkTerminalReceipt(BaseModel):
    """Closed nonretryable observation; no provider output is represented."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: Literal["composer.sdk-terminal.v1"]
    identity: PaidOverflowIdentity
    producer: Literal["primary"]
    outcome: Literal["terminal_sdk_failure"]
    observation: SdkTerminalObservation
    sdk_entered: Literal[True]
    sdk_result_observed: Literal[False]
    retryable: Literal[False]
    cost: PaidOverflowCost
    usage: PaidOverflowUsage

    @field_validator("sdk_entered", "sdk_result_observed", "retryable", mode="before")
    @classmethod
    def _strict_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("SDK terminal boolean facts require exact booleans")
        return value

    @field_validator("cost")
    @classmethod
    def _canonical_cost(cls, value: PaidOverflowCost) -> PaidOverflowCost:
        if value.amount is not None and encode_paid_overflow_cost_amount(Decimal(value.amount)) != value.amount:
            raise ValueError("SDK terminal cost requires canonical amount text")
        return value


def _require_limit(max_bytes: int) -> None:
    if type(max_bytes) is not int or not 1 <= max_bytes <= SDK_TERMINAL_RECEIPT_MAX_UTF8_BYTES:
        raise ValueError("SDK terminal receipt budget must fit its fixed wire allowance")


def _bind(receipt: SdkTerminalReceipt, expected: PaidOverflowIdentity) -> None:
    if type(expected) is not PaidOverflowIdentity:
        raise TypeError("SDK terminal receipt requires independently owned identity")
    try:
        checked = PaidOverflowIdentity.model_validate(expected)
    except ValidationError as error:
        raise AuditIntegrityError("SDK terminal expected identity violates its owned contract") from error
    if receipt.identity != checked:
        raise AuditIntegrityError("SDK terminal receipt differs from its original attempt identity")


def encode_sdk_terminal_receipt(
    receipt: SdkTerminalReceipt,
    *,
    expected: PaidOverflowIdentity,
    max_bytes: int = SDK_TERMINAL_RECEIPT_MAX_UTF8_BYTES,
) -> str:
    """Revalidate owned data and bind canonical bytes to independent identity."""
    _require_limit(max_bytes)
    if type(receipt) is not SdkTerminalReceipt:
        raise TypeError("SDK terminal encoding requires its owned receipt")
    try:
        checked = SdkTerminalReceipt.model_validate(receipt)
    except ValidationError as error:
        raise AuditIntegrityError("SDK terminal receipt violates its owned contract") from error
    _bind(checked, expected)
    encoded = canonical_json(checked.model_dump(mode="json"))
    if utf8_size(encoded, limit=max_bytes) > max_bytes:
        raise AuditIntegrityError("SDK terminal receipt exceeds its byte budget")
    return encoded


def decode_sdk_terminal_receipt(
    encoded: str,
    *,
    expected: PaidOverflowIdentity,
    max_bytes: int = SDK_TERMINAL_RECEIPT_MAX_UTF8_BYTES,
) -> SdkTerminalReceipt:
    """Bound before parsing; refuse duplicate keys and noncanonical wire data."""
    _require_limit(max_bytes)
    if type(encoded) is not str:
        raise AuditIntegrityError("SDK terminal receipt must be canonical text")
    if utf8_size(encoded, limit=max_bytes) > max_bytes:
        raise AuditIntegrityError("SDK terminal receipt exceeds its byte budget")
    value, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("SDK terminal receipt is not strict JSON")
    try:
        receipt = SdkTerminalReceipt.model_validate(value)
    except ValidationError as cause:
        raise AuditIntegrityError("SDK terminal receipt violates its owned contract") from cause
    _bind(receipt, expected)
    if canonical_json(receipt.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("SDK terminal receipt is not canonical")
    return receipt


def sdk_terminal_receipt_keys(encoded: str, *, expected: PaidOverflowIdentity) -> tuple[str, str]:
    """Bind immutable keys to a validated document, without authorizing writes."""
    decode_sdk_terminal_receipt(encoded, expected=expected)
    digest = sha256(encoded.encode("utf-8")).hexdigest()
    return f"sdk-terminal:{digest}", f"value:composer.sdk-terminal.v1:{digest}"
