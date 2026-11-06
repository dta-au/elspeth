"""Bounded receipt for a returned paid result whose mandatory evidence is incomplete.

This codec proves only the document's closed shape and expected identity. The
caller must independently prove the actual SDK return, original dispatched
attempt, capture failure, and atomic terminal SQL settlement. It grants no
success, retry, or provider-dispatch authority.
"""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from elspeth.contracts.bounded_serialization import utf8_size
from elspeth.contracts.composer_llm_audit import ComposerLLMProviderCostSource
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.provider_result_admission import V4ProviderResultLoss
from elspeth.web.sessions.continuation_paid_overflow import (
    CANONICAL_INTEGER_MAX,
    COST_AMOUNT_MAX_LENGTH,
    FINISHED_AT_MAX_LENGTH,
    LEDGER_TOKENS_MAX,
    MODEL_MAX_LENGTH,
    PaidOverflowCost,
    PaidOverflowCostStatus,
    PaidOverflowIdentity,
    PaidOverflowUsage,
    PaidOverflowUsageStatus,
    encode_paid_overflow_cost_amount,
)

type PaidIncompleteProducer = Literal["primary", "title", "semantic_advisor", "manual_advisor", "checkpoint_advisor"]
type PaidIncompleteReason = Literal[
    "missing_required_field",
    "field_unavailable",
    "unsupported_field",
    "cyclic_field",
    "capture_budget_exceeded",
    "invalid_field",
]
type PaidIncompleteCaptureSurface = Literal["provider_response", "assistant_message", "provider_metadata", "tool_batch", "physical_audit"]


def classify_paid_incomplete_loss(loss: V4ProviderResultLoss) -> tuple[PaidIncompleteReason, PaidIncompleteCaptureSurface]:
    """Map one nominal raw-capture loss without inventing a byte measurement."""
    if type(loss) is not V4ProviderResultLoss:
        raise AuditIntegrityError("paid incomplete classification requires one original raw-capture loss")
    if loss.reason == "missing":
        reason: PaidIncompleteReason = "missing_required_field"
    elif loss.reason == "recursive":
        reason = "cyclic_field"
    elif loss.reason == "unsupported":
        reason = "unsupported_field"
    elif loss.reason in ("depth_limit", "item_limit", "text_budget", "scalar_utf8_limit"):
        reason = "capture_budget_exceeded"
    elif loss.reason in ("invalid_type", "invalid_utf8", "nonfinite", "invalid_native", "invalid_tool_calls"):
        reason = "invalid_field"
    else:
        raise AuditIntegrityError("paid incomplete capture has an unknown closed loss reason")
    if loss.field == "content":
        surface: PaidIncompleteCaptureSurface = "assistant_message"
    elif loss.field == "tool_calls":
        surface = "tool_batch"
    elif loss.field in (
        "model_returned",
        "provider_request_id",
        "finish_reason",
        "reasoning_content",
        "reasoning_details",
        "thinking_blocks",
    ):
        surface = "provider_metadata"
    else:
        raise AuditIntegrityError("paid incomplete capture has an unknown closed field")
    return reason, surface


class PaidIncompleteReceipt(BaseModel):
    """Value-free terminal fact; no byte excess or output representation exists."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: Literal["composer.paid-incomplete.v1"]
    identity: PaidOverflowIdentity
    producer: PaidIncompleteProducer
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


def paid_incomplete_receipt_keys(encoded: str, *, expected: PaidOverflowIdentity) -> tuple[str, str]:
    """Share exact receipt and immutable chunk keys across family tail plans."""
    decode_paid_incomplete_receipt(encoded, expected=expected)
    digest = sha256(encoded.encode("utf-8")).hexdigest()
    return f"paid-incomplete:{digest}", f"value:composer.paid-incomplete.v1:{digest}"


_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "identity",
        "producer",
        "outcome",
        "evidence_status",
        "retryable",
        "provider_returned",
        "reason",
        "capture_surface",
        "cost",
        "usage",
    }
)
_IDENTITY_FIELDS = frozenset(
    {
        "session_id",
        "operation_id",
        "effect_key",
        "attempt_id",
        "session_operation_id",
        "session_operation_epoch",
        "ownership_generation",
        "lease_sha256",
        "policy_sha256",
        "source_checkpoint_id",
        "source_cursor",
        "source_snapshot_sha256",
    }
)


def maximum_paid_incomplete_receipt_bytes(identity: PaidOverflowIdentity, producer: PaidIncompleteProducer) -> int:
    """Conservative schema-locked envelope for one exact original identity.

    The model/timestamp/cost witnesses use escaping and combinations beyond
    an attainable receipt, so this is a safe bound rather than an exact max.
    Each family still bounds its own terminal root/events and reserves their
    complete prospective batch before dispatch.
    """
    if type(identity) is not PaidOverflowIdentity or producer not in get_args(PaidIncompleteProducer.__value__):
        raise AuditIntegrityError("paid incomplete terminal bound requires exact original identity and producer")
    try:
        identity = PaidOverflowIdentity.model_validate(identity)
    except ValidationError as error:
        raise AuditIntegrityError("paid incomplete terminal bound lost its original bounded identity") from error
    schema = PaidIncompleteReceipt.model_json_schema()
    definitions = schema.get("$defs", {})
    properties = schema.get("properties", {})
    identity_schema = PaidOverflowIdentity.model_json_schema()
    cost_properties = PaidOverflowCost.model_json_schema().get("properties", {})
    usage_properties = PaidOverflowUsage.model_json_schema().get("properties", {})
    amount = cost_properties.get("amount", {}).get("anyOf", ())
    sources = get_args(ComposerLLMProviderCostSource)
    if (
        frozenset(PaidIncompleteReceipt.model_fields) != _RECEIPT_FIELDS
        or frozenset(PaidOverflowIdentity.model_fields) != _IDENTITY_FIELDS
        or frozenset(PaidOverflowCost.model_fields) != frozenset({"status", "amount", "source"})
        or frozenset(PaidOverflowUsage.model_fields)
        != frozenset({"status", "model", "prompt_tokens", "completion_tokens", "cached_prompt_tokens", "reasoning_tokens", "finished_at"})
        or definitions.get("_Ordinal") != {"maximum": CANONICAL_INTEGER_MAX, "minimum": 0, "type": "integer"}
        or definitions.get("_Identity")
        != {
            "maxLength": 36,
            "minLength": 36,
            "pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            "type": "string",
        }
        or definitions.get("_Digest") != {"maxLength": 64, "minLength": 64, "pattern": r"^[0-9a-f]{64}$", "type": "string"}
        or definitions.get("_LedgerTokens") != {"maximum": LEDGER_TOKENS_MAX, "minimum": 0, "type": "integer"}
        or identity_schema.get("properties", {}).get("effect_key", {}).get("maxLength") != 128
        or usage_properties.get("model", {}).get("anyOf")
        != [{"maxLength": MODEL_MAX_LENGTH, "minLength": 1, "type": "string"}, {"type": "null"}]
        or usage_properties.get("finished_at", {}).get("anyOf")
        != [{"maxLength": FINISHED_AT_MAX_LENGTH, "minLength": 1, "type": "string"}, {"type": "null"}]
        or not amount
        or amount[0].get("maxLength") != COST_AMOUNT_MAX_LENGTH
        or amount[0].get("pattern") != r"^(0|[1-9][0-9]*)(\.[0-9]+)?(e-?[0-9]+)?$"
        or tuple(definitions.get("PaidIncompleteReason", {}).get("enum", ())) != get_args(PaidIncompleteReason.__value__)
        or tuple(definitions.get("PaidIncompleteCaptureSurface", {}).get("enum", ())) != get_args(PaidIncompleteCaptureSurface.__value__)
        or tuple(definitions.get("PaidOverflowCostStatus", {}).get("enum", ())) != get_args(PaidOverflowCostStatus.__value__)
        or tuple(definitions.get("PaidOverflowUsageStatus", {}).get("enum", ())) != get_args(PaidOverflowUsageStatus.__value__)
        or tuple(cost_properties.get("source", {}).get("enum", ())) != sources
        or properties.get("producer", {}).get("$ref") != "#/$defs/PaidIncompleteProducer"
        or definitions.get("PaidIncompleteProducer", {}).get("enum") != list(get_args(PaidIncompleteProducer.__value__))
        or properties.get("schema_version", {}).get("const") != "composer.paid-incomplete.v1"
        or properties.get("outcome", {}).get("const") != "terminal_paid_incomplete"
        or properties.get("evidence_status", {}).get("const") != "incomplete"
        or properties.get("retryable", {}).get("const") is not False
        or properties.get("provider_returned", {}).get("const") is not True
        or not sources
        or any(type(source) is not str for source in sources)
    ):
        raise AuditIntegrityError("paid incomplete terminal bound no longer covers its closed receipt schema")

    def longest(values: tuple[str, ...]) -> str:
        return max(values, key=lambda value: len(canonical_json(value).encode("utf-8")))

    envelope = {
        "schema_version": "composer.paid-incomplete.v1",
        "identity": identity.model_dump(mode="json"),
        "producer": producer,
        "outcome": "terminal_paid_incomplete",
        "evidence_status": "incomplete",
        "retryable": False,
        "provider_returned": True,
        "reason": longest(get_args(PaidIncompleteReason.__value__)),
        "capture_surface": longest(get_args(PaidIncompleteCaptureSurface.__value__)),
        "cost": {
            "status": longest(get_args(PaidOverflowCostStatus.__value__)),
            "amount": "9" * COST_AMOUNT_MAX_LENGTH,
            "source": longest(sources),
        },
        "usage": {
            "status": longest(get_args(PaidOverflowUsageStatus.__value__)),
            "model": "\x00" * MODEL_MAX_LENGTH,
            "prompt_tokens": LEDGER_TOKENS_MAX,
            "completion_tokens": LEDGER_TOKENS_MAX,
            "cached_prompt_tokens": LEDGER_TOKENS_MAX,
            "reasoning_tokens": LEDGER_TOKENS_MAX,
            "finished_at": "\x00" * FINISHED_AT_MAX_LENGTH,
        },
    }
    return len(canonical_json(envelope).encode("utf-8"))
