"""Pure returned-incomplete charge projection; SQL remains family-owned."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer.provider_result_admission import (
    V4ProviderResultIncomplete,
    V4ProviderResultLoss,
    V4ProviderSdkReturn,
)
from elspeth.web.sessions.continuation_paid_incomplete import PaidIncompleteReceipt
from elspeth.web.sessions.continuation_paid_incomplete_quota import (
    prepare_paid_incomplete_quota_entry,
    prepare_paid_incomplete_receipt_facts,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowCost, PaidOverflowIdentity, PaidOverflowUsage
from elspeth.web.sessions.continuation_paid_overflow_quota import (
    OriginalPaidOverflowPhysicalAttempt,
    preflight_v4_requested_model,
)
from tests.unit.web.composer.test_continuation_title_values import title_result_inputs


def _returned_incomplete(*, loss: V4ProviderResultLoss) -> tuple[V4ProviderResultIncomplete, ComposerLLMCall]:
    values = title_result_inputs()
    call = replace(
        values["audit_call"],
        status=ComposerLLMCallStatus.MALFORMED_RESPONSE,
        error_class="V4ProviderResultIncomplete",
        error_message="returned_paid_result_incomplete",
    )
    assert call.call_id is not None
    incomplete = V4ProviderResultIncomplete(
        sdk_return=V4ProviderSdkReturn(call.call_id, 1), metadata=values["completion"].provider_metadata, losses=(loss,)
    )
    return incomplete, call


def test_original_returned_failure_retains_unknown_charge_without_invented_token_zero():
    incomplete, call = _returned_incomplete(loss=V4ProviderResultLoss("reasoning_details", "text_budget"))
    facts = prepare_paid_incomplete_receipt_facts(incomplete, call, observed_at=datetime(2026, 11, 2, tzinfo=UTC))
    assert facts.cost.status == "unknown" and facts.cost.amount is None
    assert facts.usage.status == "observed" and facts.usage.model == call.model_returned
    assert (facts.usage.prompt_tokens, facts.usage.completion_tokens) == (13, 5)
    assert facts.usage.cached_prompt_tokens is None


def test_known_charge_stays_known_and_lost_returned_model_does_not_substitute_requested_model():
    incomplete, call = _returned_incomplete(loss=V4ProviderResultLoss("model_returned", "invalid_type"))
    metadata = replace(incomplete.metadata, provider_cost=0.000123, provider_cost_source="response_usage.cost")
    incomplete = V4ProviderResultIncomplete(sdk_return=incomplete.sdk_return, metadata=metadata, losses=incomplete.losses)
    call = replace(call, provider_cost=0.000123, provider_cost_source="response_usage.cost")
    facts = prepare_paid_incomplete_receipt_facts(incomplete, call, observed_at=datetime(2026, 11, 2, tzinfo=UTC))
    assert facts.cost.status == "known" and facts.cost.amount == "0.000123"
    assert facts.usage.status == "unavailable" and facts.usage.model is None


def test_charge_projection_refuses_foreign_attempt_audit_and_status():
    incomplete, call = _returned_incomplete(loss=V4ProviderResultLoss("content", "missing"))
    with pytest.raises(AuditIntegrityError, match="original returned physical audit"):
        prepare_paid_incomplete_receipt_facts(incomplete, replace(call, call_id="foreign"), observed_at=datetime.now(UTC))
    with pytest.raises(AuditIntegrityError, match="original returned physical audit"):
        prepare_paid_incomplete_receipt_facts(
            incomplete,
            replace(call, status=ComposerLLMCallStatus.SUCCESS, error_class=None, error_message=None),
            observed_at=datetime.now(UTC),
        )


def test_quota_entry_binds_exact_original_fence_and_unknown_usage_remains_null():
    sid = "00000000-0000-4000-8000-000000000001"
    operation = "00000000-0000-4000-8000-000000000002"
    attempt = "00000000-0000-4000-8000-000000000003"
    token = "live-lease"
    start = datetime(2026, 11, 2, tzinfo=UTC)
    context = SessionOperationContext(SessionOperationFence(sid, operation, token, 4), SessionOperationKind.COMPOSE)
    physical = OriginalPaidOverflowPhysicalAttempt(attempt, sid, "composer", None, operation, 4, token, start)
    identity = PaidOverflowIdentity(
        session_id=sid,
        operation_id=operation,
        effect_key="provider:3:0",
        attempt_id=attempt,
        session_operation_id=operation,
        session_operation_epoch=4,
        ownership_generation=2,
        lease_sha256=sha256(token.encode()).hexdigest(),
        policy_sha256="b" * 64,
        source_checkpoint_id="00000000-0000-4000-8000-000000000005",
        source_cursor=8,
        source_snapshot_sha256="c" * 64,
    )
    receipt = PaidIncompleteReceipt(
        schema_version="composer.paid-incomplete.v1",
        identity=identity,
        producer="primary",
        outcome="terminal_paid_incomplete",
        evidence_status="incomplete",
        retryable=False,
        provider_returned=True,
        reason="missing_required_field",
        capture_surface="assistant_message",
        cost=PaidOverflowCost(status="unknown", amount=None, source="not_available"),
        usage=PaidOverflowUsage(
            status="unavailable",
            model=None,
            prompt_tokens=None,
            completion_tokens=None,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            finished_at=None,
        ),
    )
    observed = start + timedelta(seconds=1)
    entry = prepare_paid_incomplete_quota_entry(
        physical=physical,
        context=context,
        receipt=receipt,
        requested_model=preflight_v4_requested_model("requested-model"),
        observed_at=observed,
    )
    assert entry.call_id == attempt and entry.model == "requested-model"
    assert entry.prompt_tokens is entry.completion_tokens is entry.cached_prompt_tokens is entry.reasoning_tokens is None
    with pytest.raises(AuditIntegrityError, match="original physical fence"):
        prepare_paid_incomplete_quota_entry(
            physical=replace(physical, attempt_id="foreign"),
            context=context,
            receipt=receipt,
            requested_model=preflight_v4_requested_model("requested-model"),
            observed_at=observed,
        )
