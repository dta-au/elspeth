"""Prepare V4 paid-overflow token accounting without granting settlement authority.

The terminal SQL owner must prove the original effect, persisted receipt and
failed parent in one transaction before passing the returned entry to the
existing quota settlement writer. A receipt in memory is no such proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal, final

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.coordination.quota_authority import TokenUsageEntry
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions.continuation_paid_incomplete import PaidIncompleteReceipt
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowReceipt

_REQUESTED_MODEL_MAX_UTF8_BYTES = 256


@final
@dataclass(frozen=True, slots=True)
class AdmittedPaidOverflowRequestedModel:
    """An exact requested model checked before the V4 SDK invocation."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or not self.value or self.value.strip() != self.value:
            raise ValueError("V4 requested model must be a nonblank exact string without surrounding whitespace")
        if not self.value.isprintable() or len(self.value.encode("utf-8")) > _REQUESTED_MODEL_MAX_UTF8_BYTES:
            raise ValueError("V4 requested model exceeds its safe retention bound")
        require_no_credential_material(self.value, surface="composer_paid_overflow_requested_model")


def preflight_v4_requested_model(value: str) -> AdmittedPaidOverflowRequestedModel:
    """Call before SDK entry; retain this exact value with original effect custody."""
    return AdmittedPaidOverflowRequestedModel(value)


@final
@dataclass(frozen=True, slots=True)
class OriginalPaidOverflowPhysicalAttempt:
    """Exact original quota row fields supplied by the terminal SQL owner."""

    attempt_id: str
    session_id: str
    source: Literal["composer", "auto_title"]
    run_id: None
    operation_id: str
    operation_epoch: int
    lease_token: str
    started_at: datetime


def _prepare_paid_terminal_quota_entry(
    *,
    physical: OriginalPaidOverflowPhysicalAttempt,
    context: SessionOperationContext,
    receipt: PaidOverflowReceipt | PaidIncompleteReceipt,
    requested_model: AdmittedPaidOverflowRequestedModel,
    observed_at: datetime,
) -> TokenUsageEntry:
    """Project one original physical attempt; unknown measures remain NULL.

    ``observed_at`` is the caller's database-clock-bound response observation,
    not a recovery timestamp. The SQL owner must compare ``requested_model``
    with the original persisted pre-SDK model and verify durable terminal
    evidence before invoking ``settle_provider_attempt_on_connection``.
    """
    if type(physical) is not OriginalPaidOverflowPhysicalAttempt:
        raise TypeError("paid overflow quota requires its original physical attempt")
    if type(context) is not SessionOperationContext or context.operation_kind is not SessionOperationKind.COMPOSE:
        raise AuditIntegrityError("paid overflow quota requires the original COMPOSE context")
    if type(receipt) not in (PaidOverflowReceipt, PaidIncompleteReceipt) or type(requested_model) is not AdmittedPaidOverflowRequestedModel:
        raise TypeError("paid terminal quota requires owned receipt and preflight model")
    # Revalidate unchecked dataclass/Pydantic copies before using their values.
    requested_model = AdmittedPaidOverflowRequestedModel(requested_model.value)
    if type(receipt) is PaidOverflowReceipt:
        receipt = PaidOverflowReceipt.model_validate(receipt)
    else:
        receipt = PaidIncompleteReceipt.model_validate(receipt)
    if type(observed_at) is not datetime or observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("paid overflow observation time must be an aware datetime")
    if type(physical.started_at) is not datetime or physical.started_at.tzinfo is None or physical.started_at.utcoffset() is None:
        raise AuditIntegrityError("original physical attempt lost its aware admission time")
    fence = context.fence
    identity = receipt.identity
    expected_source = "auto_title" if receipt.producer == "title" else "composer"
    if (
        type(physical.attempt_id) is not str
        or type(physical.session_id) is not str
        or physical.source != expected_source
        or physical.run_id is not None
        or type(physical.operation_id) is not str
        or type(physical.operation_epoch) is not int
        or type(physical.lease_token) is not str
        or not physical.lease_token
        or physical.attempt_id != identity.attempt_id
        or physical.session_id != identity.session_id
        or physical.operation_id != identity.session_operation_id
        or physical.operation_epoch != identity.session_operation_epoch
        or sha256(physical.lease_token.encode("utf-8")).hexdigest() != identity.lease_sha256
        or physical.session_id != fence.session_id
        or physical.operation_id != fence.operation_id
        or physical.operation_epoch != fence.operation_epoch
        or physical.lease_token != fence.lease_token
    ):
        raise AuditIntegrityError("paid overflow quota differs from its original physical fence")
    if observed_at.astimezone(UTC) < physical.started_at.astimezone(UTC):
        raise AuditIntegrityError("paid overflow observation predates its physical admission")
    usage = receipt.usage
    if usage is None:
        raise AuditIntegrityError("V4 paid overflow requires an explicit usage disposition")
    if usage.status == "observed":
        if usage.finished_at != observed_at.isoformat():
            raise AuditIntegrityError("paid overflow usage differs from its observed completion time")
        counts = (usage.prompt_tokens, usage.completion_tokens, usage.cached_prompt_tokens, usage.reasoning_tokens)
    else:
        counts = (None, None, None, None)
    return TokenUsageEntry(
        model=requested_model.value,
        prompt_tokens=counts[0],
        completion_tokens=counts[1],
        cached_prompt_tokens=counts[2],
        reasoning_tokens=counts[3],
        recorded_at=observed_at.astimezone(UTC),
        call_id=physical.attempt_id,
    )


def prepare_paid_overflow_quota_entry(
    *,
    physical: OriginalPaidOverflowPhysicalAttempt,
    context: SessionOperationContext,
    receipt: PaidOverflowReceipt,
    requested_model: AdmittedPaidOverflowRequestedModel,
    observed_at: datetime,
) -> TokenUsageEntry:
    """Preserve the published measured-overflow API and exact validation."""
    if type(receipt) is not PaidOverflowReceipt:
        raise TypeError("paid overflow quota requires its exact measured receipt")
    return _prepare_paid_terminal_quota_entry(
        physical=physical, context=context, receipt=receipt, requested_model=requested_model, observed_at=observed_at
    )
