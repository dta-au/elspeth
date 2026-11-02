"""Bounded paid-overflow receipts; no SQL custody or dispatch authority.

The caller supplies identity independently recovered from its original effect
and physical attempt. Equality here binds the document to that expectation;
it cannot establish that either identity was authorized or ever dispatched.
No complete result, provider text, or successful-response audit is represented.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from elspeth.contracts.bounded_serialization import utf8_size
from elspeth.contracts.composer_llm_audit import ComposerLLMProviderCostSource
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.credential_guard import require_no_credential_material

_CANONICAL_INTEGER_MAX = 2**53 - 1
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_SHA256 = r"^[0-9a-f]{64}$"
type _Identity = Annotated[str, Field(pattern=_UUID, min_length=36, max_length=36)]
type _Digest = Annotated[str, Field(pattern=_SHA256, min_length=64, max_length=64)]
type _Ordinal = Annotated[int, Field(ge=0, le=_CANONICAL_INTEGER_MAX)]
type _LedgerTokens = Annotated[int, Field(ge=0, le=2**31 - 1)]


class PaidOverflowIdentity(BaseModel):
    """Original source identity, to be proved by the integrating SQL owner."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, revalidate_instances="always")

    session_id: _Identity
    operation_id: _Identity
    effect_key: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_:.-]+$")]
    attempt_id: _Identity
    session_operation_id: _Identity
    session_operation_epoch: _Ordinal
    ownership_generation: _Ordinal
    # Bind the original lease without retaining a reusable lease token.
    lease_sha256: _Digest
    policy_sha256: _Digest
    source_checkpoint_id: _Identity
    source_cursor: _Ordinal
    source_snapshot_sha256: _Digest


class PaidOverflowCost(BaseModel):
    """Bounded decimal text preserves a known charge without float rounding."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, revalidate_instances="always")

    status: Literal["known", "unknown"]
    amount: Annotated[str, Field(max_length=128, pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")] | None
    source: ComposerLLMProviderCostSource

    @model_validator(mode="after")
    def _consistent_cost(self) -> Self:
        if self.status == "known":
            if self.amount is None or self.source == "not_available":
                raise ValueError("known overflow cost requires an amount and provenance")
        elif self.amount is not None or self.source != "not_available":
            raise ValueError("unknown overflow cost cannot assert an amount or provenance")
        return self


class PaidOverflowUsage(BaseModel):
    """Actual ledger projection, or explicit unavailability without a model.

    The effective model is the actual returned model when present, otherwise
    the original requested model, as in the ordinary ledger adapter. Callers
    must select unavailable when that exact model cannot fit; never truncate
    it or substitute a different model. None token measures remain unknown.
    The call identity comes from the receipt, not this observation.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, revalidate_instances="always")

    status: Literal["observed", "unavailable"]
    model: Annotated[str, Field(min_length=1, max_length=256)] | None
    prompt_tokens: _LedgerTokens | None
    completion_tokens: _LedgerTokens | None
    cached_prompt_tokens: _LedgerTokens | None
    reasoning_tokens: _LedgerTokens | None
    # The quota owner's database-clock-bound completion time, not provider time.
    finished_at: Annotated[str, Field(min_length=1, max_length=64)] | None

    @field_validator("model")
    @classmethod
    def _credential_safe_model(cls, value: str | None) -> str | None:
        if value is not None:
            require_no_credential_material(value, surface="composer_paid_overflow_usage_model")
        return value

    @model_validator(mode="after")
    def _consistent_observation(self) -> Self:
        if self.status == "unavailable":
            if any(
                value is not None
                for value in (
                    self.model,
                    self.prompt_tokens,
                    self.completion_tokens,
                    self.cached_prompt_tokens,
                    self.reasoning_tokens,
                    self.finished_at,
                )
            ):
                raise ValueError("unavailable overflow usage cannot assert ledger observations")
            return self
        if self.model is None or not self.model.strip() or self.finished_at is None:
            raise ValueError("observed overflow usage requires its exact model and completion time")
        completed = datetime.fromisoformat(self.finished_at)
        if completed.tzinfo is None or completed.utcoffset() is None or completed.isoformat() != self.finished_at:
            raise ValueError("overflow completion time must be canonical and timezone aware")
        return self


class PaidOverflowReceipt(BaseModel):
    """A failed paid attempt with incomplete content evidence, never a result."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: Literal["composer.paid-overflow.v1"]
    identity: PaidOverflowIdentity
    producer: Literal["primary", "title", "semantic_advisor", "manual_advisor"]
    outcome: Literal["terminal_paid_overflow"]
    evidence_status: Literal["incomplete"]
    retryable: Literal[False]
    reason: Literal["private_document_bytes", "evidence_pair_bytes", "retained_payload_bytes"]
    representation: Literal["response", "physical_audit", "recorder", "evidence_pair", "retained_tail"]
    measurement: Literal["exact", "at_least"]
    observed_bytes: _Ordinal
    limit_bytes: Annotated[int, Field(ge=1, le=_CANONICAL_INTEGER_MAX)]
    cost: PaidOverflowCost
    usage: PaidOverflowUsage | None = None

    @field_validator("retryable", mode="before")
    @classmethod
    def _strict_retryable(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("overflow retryable must be a boolean")
        return value

    @model_validator(mode="after")
    def _observed_overflow(self) -> Self:
        if self.observed_bytes <= self.limit_bytes:
            raise ValueError("overflow observation must exceed its original limit")
        return self


def _require_limit(max_bytes: int) -> None:
    if type(max_bytes) is not int or not 1 <= max_bytes <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise ValueError("overflow receipt budget must fit the private document limit")


def _bind(receipt: PaidOverflowReceipt, expected: PaidOverflowIdentity) -> None:
    if type(expected) is not PaidOverflowIdentity:
        raise TypeError("overflow receipt requires its independently owned identity")
    if receipt.identity != expected:
        raise AuditIntegrityError("paid overflow receipt differs from its original source identity")


def encode_paid_overflow_receipt(
    receipt: PaidOverflowReceipt,
    *,
    expected: PaidOverflowIdentity,
    max_bytes: int = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
) -> str:
    """Validate fresh owned data including unchecked copies before encoding."""
    _require_limit(max_bytes)
    if type(receipt) is not PaidOverflowReceipt:
        raise TypeError("paid overflow encoding requires its owned receipt")
    try:
        checked = PaidOverflowReceipt.model_validate(receipt)
    except ValidationError as error:
        raise AuditIntegrityError("paid overflow receipt violates its owned contract") from error
    _bind(checked, expected)
    encoded = canonical_json(checked.model_dump(mode="json"))
    if utf8_size(encoded, limit=max_bytes) > max_bytes:
        raise AuditIntegrityError("paid overflow receipt exceeds its byte budget")
    return encoded


def decode_paid_overflow_receipt(
    encoded: str,
    *,
    expected: PaidOverflowIdentity,
    max_bytes: int = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
) -> PaidOverflowReceipt:
    """Reject oversized input before JSON parsing; require exact canonical bytes."""
    _require_limit(max_bytes)
    if type(encoded) is not str:
        raise AuditIntegrityError("paid overflow receipt must be canonical text")
    if utf8_size(encoded, limit=max_bytes) > max_bytes:
        raise AuditIntegrityError("paid overflow receipt exceeds its byte budget")
    value, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("paid overflow receipt is not strict JSON")
    try:
        receipt = PaidOverflowReceipt.model_validate(value)
    except ValidationError as cause:
        raise AuditIntegrityError("paid overflow receipt violates its owned contract") from cause
    if encode_paid_overflow_receipt(receipt, expected=expected, max_bytes=max_bytes) != encoded:
        raise AuditIntegrityError("paid overflow receipt is not canonical")
    return receipt
