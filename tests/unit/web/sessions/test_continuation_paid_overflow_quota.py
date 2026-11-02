"""Pure V4 paid-overflow quota projection; SQL settlement is owned elsewhere."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowCost, PaidOverflowIdentity, PaidOverflowReceipt, PaidOverflowUsage
from elspeth.web.sessions.continuation_paid_overflow_quota import (
    AdmittedPaidOverflowRequestedModel,
    OriginalPaidOverflowPhysicalAttempt,
    preflight_v4_requested_model,
    prepare_paid_overflow_quota_entry,
)

SESSION = "00000000-0000-4000-8000-000000000001"
OPERATION = "00000000-0000-4000-8000-000000000002"
ATTEMPT = "00000000-0000-4000-8000-000000000003"
FENCE_OPERATION = "00000000-0000-4000-8000-000000000004"
LEASE = "original-lease"
START = datetime(2026, 11, 2, 1, tzinfo=UTC)
OBSERVED = START + timedelta(seconds=2)


def _source() -> tuple[OriginalPaidOverflowPhysicalAttempt, SessionOperationContext, PaidOverflowReceipt]:
    physical = OriginalPaidOverflowPhysicalAttempt(
        attempt_id=ATTEMPT,
        session_id=SESSION,
        source="composer",
        run_id=None,
        operation_id=FENCE_OPERATION,
        operation_epoch=3,
        lease_token=LEASE,
        started_at=START,
    )
    context = SessionOperationContext(
        fence=SessionOperationFence(session_id=SESSION, operation_id=FENCE_OPERATION, operation_epoch=3, lease_token=LEASE),
        operation_kind=SessionOperationKind.COMPOSE,
    )
    receipt = PaidOverflowReceipt(
        schema_version="composer.paid-overflow.v1",
        identity=PaidOverflowIdentity(
            session_id=SESSION,
            operation_id=OPERATION,
            effect_key="provider:1:0",
            attempt_id=ATTEMPT,
            session_operation_id=FENCE_OPERATION,
            session_operation_epoch=3,
            ownership_generation=1,
            lease_sha256=sha256(LEASE.encode()).hexdigest(),
            policy_sha256="a" * 64,
            source_checkpoint_id="00000000-0000-4000-8000-000000000005",
            source_cursor=1,
            source_snapshot_sha256="b" * 64,
        ),
        producer="primary",
        outcome="terminal_paid_overflow",
        evidence_status="incomplete",
        retryable=False,
        reason="private_document_bytes",
        representation="response",
        measurement="at_least",
        observed_bytes=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1,
        limit_bytes=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
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
    return physical, context, receipt


def _prepare(physical: OriginalPaidOverflowPhysicalAttempt, context: SessionOperationContext, receipt: PaidOverflowReceipt):
    return prepare_paid_overflow_quota_entry(
        physical=physical,
        context=context,
        receipt=receipt,
        requested_model=preflight_v4_requested_model("openai/original"),
        observed_at=OBSERVED,
    )


def test_unavailable_returned_model_and_tokens_keep_original_requested_model_and_nulls() -> None:
    physical, context, receipt = _source()
    entry = _prepare(physical, context, receipt)
    assert entry.call_id == ATTEMPT
    assert entry.model == "openai/original"
    assert (entry.prompt_tokens, entry.completion_tokens, entry.cached_prompt_tokens, entry.reasoning_tokens) == (None, None, None, None)
    assert entry.recorded_at == OBSERVED


def test_v4_requires_explicit_unknown_usage_disposition() -> None:
    physical, context, receipt = _source()
    with pytest.raises(AuditIntegrityError, match="explicit usage disposition"):
        _prepare(physical, context, receipt.model_copy(update={"usage": None}))


def test_title_uses_original_auto_title_source_with_unknown_usage() -> None:
    physical, context, receipt = _source()
    title_identity = receipt.identity.model_copy(update={"effect_key": "provider:3:0"})
    title_receipt = receipt.model_copy(update={"producer": "title", "identity": title_identity})
    title_physical = replace(physical, source="auto_title")
    entry = _prepare(title_physical, context, title_receipt)
    assert entry.call_id == ATTEMPT
    assert entry.model == "openai/original"
    assert (entry.prompt_tokens, entry.completion_tokens) == (None, None)


@pytest.mark.parametrize("receipt_producer,physical_source", [("title", "composer"), ("primary", "auto_title")])
def test_receipt_producer_refuses_wrong_original_source(receipt_producer: str, physical_source: str) -> None:
    physical, context, receipt = _source()
    wrong_receipt = receipt.model_copy(update={"producer": receipt_producer})
    wrong_physical = replace(physical, source=physical_source)
    with pytest.raises(AuditIntegrityError, match="original physical fence"):
        _prepare(wrong_physical, context, wrong_receipt)


def test_observed_counts_retain_exact_independent_fields_and_cost_stays_separate() -> None:
    physical, context, receipt = _source()
    receipt = receipt.model_copy(
        update={
            "cost": PaidOverflowCost(status="known", amount="0.001", source="response_usage.cost"),
            "usage": PaidOverflowUsage(
                status="observed",
                model="openai/returned",
                prompt_tokens=0,
                completion_tokens=None,
                cached_prompt_tokens=7,
                reasoning_tokens=2,
                finished_at=OBSERVED.isoformat(),
            ),
        }
    )
    entry = _prepare(physical, context, receipt)
    assert entry.model == "openai/original"
    assert (entry.prompt_tokens, entry.completion_tokens, entry.cached_prompt_tokens, entry.reasoning_tokens) == (0, None, 7, 2)
    assert not hasattr(entry, "cost")


@pytest.mark.parametrize("field", ["attempt_id", "session_id", "operation_id", "operation_epoch", "lease_token", "source", "run_id"])
def test_original_physical_mismatch_is_refused(field: str) -> None:
    physical, context, receipt = _source()
    bad = replace(physical, **{field: "other" if field != "operation_epoch" else 4})
    with pytest.raises(AuditIntegrityError, match="original physical fence"):
        _prepare(bad, context, receipt)


def test_receipt_call_id_and_live_fence_mismatch_are_refused() -> None:
    physical, context, receipt = _source()
    bad_identity = receipt.identity.model_copy(update={"attempt_id": "00000000-0000-4000-8000-000000000099"})
    with pytest.raises(AuditIntegrityError, match="original physical fence"):
        _prepare(physical, context, receipt.model_copy(update={"identity": bad_identity}))
    stale = replace(context, fence=replace(context.fence, operation_epoch=4))
    with pytest.raises(AuditIntegrityError, match="original physical fence"):
        _prepare(physical, stale, receipt)


def test_preflight_refuses_unbounded_or_credential_model() -> None:
    with pytest.raises(ValueError, match="safe retention bound"):
        preflight_v4_requested_model("x" * 257)
    with pytest.raises(CredentialMaterialRefused):
        preflight_v4_requested_model("sk-" + "a" * 48)
    with pytest.raises(ValueError, match="nonblank"):
        AdmittedPaidOverflowRequestedModel(" openai/original")


def test_observation_time_must_be_the_original_observation() -> None:
    physical, context, receipt = _source()
    with pytest.raises(AuditIntegrityError, match="predates"):
        prepare_paid_overflow_quota_entry(
            physical=physical,
            context=context,
            receipt=receipt,
            requested_model=preflight_v4_requested_model("openai/original"),
            observed_at=START - timedelta(seconds=1),
        )
    observed = PaidOverflowUsage(
        status="observed",
        model="openai/returned",
        prompt_tokens=1,
        completion_tokens=2,
        cached_prompt_tokens=None,
        reasoning_tokens=None,
        finished_at=OBSERVED.isoformat(),
    )
    with pytest.raises(AuditIntegrityError, match="completion time"):
        prepare_paid_overflow_quota_entry(
            physical=physical,
            context=context,
            receipt=receipt.model_copy(update={"usage": observed}),
            requested_model=preflight_v4_requested_model("openai/original"),
            observed_at=OBSERVED + timedelta(seconds=1),
        )
