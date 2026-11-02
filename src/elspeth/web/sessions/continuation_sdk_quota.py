"""Original SDK terminal charge observations and exact physical ledger binding."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.provider_sdk_observation import V4SdkFailureObservation, require_original_v4_sdk_failure
from elspeth.web.coordination.quota_authority import TokenUsageEntry
from elspeth.web.sessions.continuation_paid_overflow import (
    LEDGER_TOKENS_MAX,
    MODEL_MAX_LENGTH,
    PaidOverflowCost,
    PaidOverflowIdentity,
    PaidOverflowUsage,
    encode_paid_overflow_cost_amount,
)
from elspeth.web.sessions.continuation_paid_overflow_quota import AdmittedPaidOverflowRequestedModel, OriginalPaidOverflowPhysicalAttempt
from elspeth.web.sessions.continuation_sdk_terminal import SdkTerminalReceipt


def prepare_sdk_terminal_receipt(
    *,
    observation: V4SdkFailureObservation,
    custody: ProviderCallCustody,
    identity: PaidOverflowIdentity,
    call: ComposerLLMCall | None,
    observed_at: datetime,
) -> SdkTerminalReceipt:
    require_original_v4_sdk_failure(observation, custody=custody)
    if type(identity) is not PaidOverflowIdentity or identity.attempt_id != observation.attempt.attempt_id:
        raise AuditIntegrityError("SDK receipt lost its original observed attempt")
    if type(observed_at) is not datetime or observed_at.utcoffset() is None:
        raise ValueError("SDK observation requires aware database time")
    cost = PaidOverflowCost(status="unknown", amount=None, source="not_available")
    usage = PaidOverflowUsage(
        status="unavailable",
        model=None,
        prompt_tokens=None,
        completion_tokens=None,
        cached_prompt_tokens=None,
        reasoning_tokens=None,
        finished_at=None,
    )
    if call is not None:
        if (
            type(call) is not ComposerLLMCall
            or call is not custody.call
            or call.call_id != identity.attempt_id
            or call.started_at != observation.attempt.started_at
            or call.status is ComposerLLMCallStatus.SUCCESS
        ):
            raise AuditIntegrityError("SDK receipt changed its original failure audit")
        if call.provider_cost is not None:
            cost = PaidOverflowCost(
                status="known", amount=encode_paid_overflow_cost_amount(call.provider_cost), source=call.provider_cost_source
            )
        model = call.model_returned if call.model_returned is not None else call.model_requested
        counts = (call.prompt_tokens, call.completion_tokens, call.cached_prompt_tokens, call.reasoning_tokens)
        if (
            model
            and len(model) <= MODEL_MAX_LENGTH
            and any(count is not None for count in counts)
            and all(count is None or (type(count) is int and 0 <= count <= LEDGER_TOKENS_MAX) for count in counts)
        ):
            usage = PaidOverflowUsage(
                status="observed",
                model=model,
                prompt_tokens=counts[0],
                completion_tokens=counts[1],
                cached_prompt_tokens=counts[2],
                reasoning_tokens=counts[3],
                finished_at=observed_at.isoformat(),
            )
    return SdkTerminalReceipt(
        schema_version="composer.sdk-terminal.v1",
        identity=identity,
        producer="primary",
        outcome="terminal_sdk_failure",
        observation=observation.observation,
        sdk_entered=True,
        sdk_result_observed=False,
        retryable=False,
        cost=cost,
        usage=usage,
    )


def prepare_sdk_terminal_quota_entry(
    *,
    physical: OriginalPaidOverflowPhysicalAttempt,
    context: SessionOperationContext,
    receipt: SdkTerminalReceipt,
    requested_model: AdmittedPaidOverflowRequestedModel,
    observed_at: datetime,
) -> TokenUsageEntry:
    """Unknown token measures stay NULL; never consume a budget as a bill."""
    if type(physical) is not OriginalPaidOverflowPhysicalAttempt or type(receipt) is not SdkTerminalReceipt:
        raise TypeError("SDK quota requires original nominal attempt and receipt")
    if type(context) is not SessionOperationContext or context.operation_kind is not SessionOperationKind.COMPOSE:
        raise AuditIntegrityError("SDK quota requires original COMPOSE authority")
    if type(requested_model) is not AdmittedPaidOverflowRequestedModel:
        raise TypeError("SDK quota requires original preflight model")
    requested_model = AdmittedPaidOverflowRequestedModel(requested_model.value)
    receipt = SdkTerminalReceipt.model_validate(receipt)
    if type(observed_at) is not datetime or observed_at.utcoffset() is None:
        raise ValueError("SDK quota observation requires aware database time")
    if type(physical.started_at) is not datetime or physical.started_at.utcoffset() is None:
        raise AuditIntegrityError("SDK quota lost its original admission time")
    identity, fence = receipt.identity, context.fence
    if (
        physical.attempt_id != identity.attempt_id
        or physical.session_id != identity.session_id
        or physical.source != "composer"
        or physical.run_id is not None
        or physical.operation_id != identity.session_operation_id
        or type(physical.operation_epoch) is not int
        or physical.operation_epoch != identity.session_operation_epoch
        or type(physical.lease_token) is not str
        or not physical.lease_token
        or sha256(physical.lease_token.encode("utf-8")).hexdigest() != identity.lease_sha256
        or physical.session_id != fence.session_id
        or physical.operation_id != fence.operation_id
        or physical.operation_epoch != fence.operation_epoch
        or physical.lease_token != fence.lease_token
        or observed_at < physical.started_at
    ):
        raise AuditIntegrityError("SDK quota differs from its original physical fence")
    usage = receipt.usage
    if usage.status == "observed" and usage.finished_at != observed_at.isoformat():
        raise AuditIntegrityError("SDK usage changed its original observation time")
    return TokenUsageEntry(
        model=requested_model.value,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cached_prompt_tokens=usage.cached_prompt_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        recorded_at=observed_at.astimezone(UTC),
        call_id=physical.attempt_id,
    )
