"""Pure returned-incomplete cost and quota preparation, with no SQL authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMProviderMetadata
from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete, V4ProviderSdkReturn
from elspeth.web.coordination.quota_authority import TokenUsageEntry
from elspeth.web.sessions.continuation_paid_incomplete import PaidIncompleteReceipt, classify_paid_incomplete_loss
from elspeth.web.sessions.continuation_paid_overflow import (
    LEDGER_TOKENS_MAX,
    MODEL_MAX_LENGTH,
    PaidOverflowCost,
    PaidOverflowUsage,
    encode_paid_overflow_cost_amount,
)
from elspeth.web.sessions.continuation_paid_overflow_quota import (
    AdmittedPaidOverflowRequestedModel,
    OriginalPaidOverflowPhysicalAttempt,
    _prepare_paid_terminal_quota_entry,
)


@dataclass(frozen=True, slots=True)
class PaidIncompleteChargeObservation:
    """Bounded actual charge facts; never a response or original SDK grant."""

    cost: PaidOverflowCost
    usage: PaidOverflowUsage


def prepare_paid_incomplete_receipt_facts(
    incomplete: V4ProviderResultIncomplete, audit_call: ComposerLLMCall, *, observed_at: datetime
) -> PaidIncompleteChargeObservation:
    """Retain known charge and explicit unknowns from one original raw return."""
    if (
        type(incomplete) is not V4ProviderResultIncomplete
        or type(incomplete.sdk_return) is not V4ProviderSdkReturn
        or type(audit_call) is not ComposerLLMCall
        or audit_call.status is not ComposerLLMCallStatus.MALFORMED_RESPONSE
        or audit_call.call_id != incomplete.sdk_return.attempt_id
        or type(observed_at) is not datetime
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise AuditIntegrityError("paid incomplete charge lost its original returned physical audit")
    for loss in incomplete.losses:
        classify_paid_incomplete_loss(loss)
    metadata = incomplete.metadata
    if type(metadata) is not _AdmittedLLMProviderMetadata:
        raise AuditIntegrityError("paid incomplete charge lost its owned bounded metadata")
    usage = metadata.usage
    if (
        metadata.v4_completeness is not None
        or audit_call.model_returned != metadata.model_returned
        or audit_call.provider_request_id != metadata.provider_request_id
        or audit_call.provider_cost != metadata.provider_cost
        or audit_call.provider_cost_source != metadata.provider_cost_source
        or audit_call.prompt_tokens != usage.prompt_tokens
        or audit_call.completion_tokens != usage.completion_tokens
        or audit_call.cached_prompt_tokens != usage.cached_prompt_tokens
        or audit_call.reasoning_tokens != usage.reasoning_tokens
    ):
        raise AuditIntegrityError("paid incomplete charge differs from its original bounded return facts")
    if audit_call.provider_cost is None:
        cost = PaidOverflowCost(status="unknown", amount=None, source="not_available")
    else:
        cost = PaidOverflowCost(
            status="known",
            amount=encode_paid_overflow_cost_amount(audit_call.provider_cost),
            source=audit_call.provider_cost_source,
        )
    model_lost = any(loss.field == "model_returned" for loss in incomplete.losses)
    model = audit_call.model_returned if audit_call.model_returned is not None else audit_call.model_requested
    counts = (
        audit_call.prompt_tokens,
        audit_call.completion_tokens,
        audit_call.cached_prompt_tokens,
        audit_call.reasoning_tokens,
    )
    if (
        model_lost
        or not model
        or len(model) > MODEL_MAX_LENGTH
        or any(count is not None and (type(count) is not int or count < 0 or count > LEDGER_TOKENS_MAX) for count in counts)
    ):
        observed_usage = PaidOverflowUsage(
            status="unavailable",
            model=None,
            prompt_tokens=None,
            completion_tokens=None,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            finished_at=None,
        )
    else:
        observed_usage = PaidOverflowUsage(
            status="observed",
            model=model,
            prompt_tokens=counts[0],
            completion_tokens=counts[1],
            cached_prompt_tokens=counts[2],
            reasoning_tokens=counts[3],
            finished_at=observed_at.isoformat(),
        )
    return PaidIncompleteChargeObservation(cost, observed_usage)


def prepare_paid_incomplete_quota_entry(
    *,
    physical: OriginalPaidOverflowPhysicalAttempt,
    context: SessionOperationContext,
    receipt: PaidIncompleteReceipt,
    requested_model: AdmittedPaidOverflowRequestedModel,
    observed_at: datetime,
) -> TokenUsageEntry:
    """Bind a distinct incomplete receipt to the original physical fence."""
    if type(receipt) is not PaidIncompleteReceipt:
        raise TypeError("paid incomplete quota requires its exact returned-incomplete receipt")
    return _prepare_paid_terminal_quota_entry(
        physical=physical, context=context, receipt=receipt, requested_model=requested_model, observed_at=observed_at
    )
