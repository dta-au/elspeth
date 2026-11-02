"""Bounded receipt for a returned paid result whose mandatory evidence is incomplete.

This codec proves only the document's closed shape and expected identity. The
caller must independently prove the actual SDK return, original dispatched
attempt, capture failure, and atomic terminal SQL settlement. It grants no
success, retry, or provider-dispatch authority.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from elspeth.contracts.bounded_serialization import utf8_size
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.sessions.continuation_paid_overflow import (
    PaidOverflowCost,
    PaidOverflowIdentity,
    PaidOverflowUsage,
    encode_paid_overflow_cost_amount,
)

type PaidIncompleteReason = Literal["missing_required_field", "field_unavailable", "unsupported_field", "cyclic_field"]
type PaidIncompleteCaptureSurface = Literal["provider_response", "assistant_message", "provider_metadata", "tool_batch", "physical_audit"]


class PaidIncompleteReceipt(BaseModel):
    """Value-free terminal fact; no byte excess or output representation exists."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: Literal["composer.paid-incomplete.v1"]
    identity: PaidOverflowIdentity
    producer: Literal["primary", "title", "semantic_advisor", "manual_advisor", "checkpoint_advisor"]
    outcome: Literal["terminal_paid_incomplete"]
    evidence_status: Literal["incomplete"]
    retryable: Literal[False]
    provider_returned: Literal[True]
    reason: PaidIncompleteReason
    capture_surface: PaidIncompleteCaptureSurface
    cost: PaidOverflowCost
    usage: PaidOverflowUsage

    @field_validator("retryable", "provider_returned", mode="before")
    @classmethod
    def _strict_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("paid incomplete boolean facts require exact booleans")
        return value


def _require_limit(max_bytes: int) -> None:
    if type(max_bytes) is not int or not 1 <= max_bytes <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise ValueError("paid incomplete receipt budget must fit the private document limit")


def _bind(receipt: PaidIncompleteReceipt, expected: PaidOverflowIdentity) -> None:
    if type(expected) is not PaidOverflowIdentity:
        raise TypeError("paid incomplete receipt requires independently owned identity")
    if receipt.identity != expected:
        raise AuditIntegrityError("paid incomplete receipt differs from its original attempt identity")


def encode_paid_incomplete_receipt(
    receipt: PaidIncompleteReceipt,
    *,
    expected: PaidOverflowIdentity,
    max_bytes: int = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
) -> str:
    """Validate fresh owned data, including unchecked model copies, then encode."""
    _require_limit(max_bytes)
    if type(receipt) is not PaidIncompleteReceipt:
        raise TypeError("paid incomplete encoding requires its owned receipt")
    try:
        checked = PaidIncompleteReceipt.model_validate(receipt)
    except ValidationError as error:
        raise AuditIntegrityError("paid incomplete receipt violates its owned contract") from error
    _bind(checked, expected)
    if checked.cost.amount is not None:
        try:
            amount = encode_paid_overflow_cost_amount(Decimal(checked.cost.amount))
        except ValueError as error:
            raise AuditIntegrityError("paid incomplete writer requires canonical amount text") from error
        if amount != checked.cost.amount:
            raise AuditIntegrityError("paid incomplete writer requires canonical amount text")
    encoded = canonical_json(checked.model_dump(mode="json"))
    if utf8_size(encoded, limit=max_bytes) > max_bytes:
        raise AuditIntegrityError("paid incomplete receipt exceeds its byte budget")
    return encoded


def decode_paid_incomplete_receipt(
    encoded: str,
    *,
    expected: PaidOverflowIdentity,
    max_bytes: int = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
) -> PaidIncompleteReceipt:
    """Bound input before strict parsing and bind its canonical bytes."""
    _require_limit(max_bytes)
    if type(encoded) is not str:
        raise AuditIntegrityError("paid incomplete receipt must be canonical text")
    if utf8_size(encoded, limit=max_bytes) > max_bytes:
        raise AuditIntegrityError("paid incomplete receipt exceeds its byte budget")
    value, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("paid incomplete receipt is not strict JSON")
    try:
        receipt = PaidIncompleteReceipt.model_validate(value)
    except ValidationError as cause:
        raise AuditIntegrityError("paid incomplete receipt violates its owned contract") from cause
    _bind(receipt, expected)
    if canonical_json(receipt.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("paid incomplete receipt is not canonical")
    return receipt
