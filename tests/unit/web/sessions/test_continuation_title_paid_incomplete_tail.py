"""A returned incomplete TITLE result has one bounded, non-byte terminal tail."""

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import EffectFrontierEntry
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_paid_incomplete import PaidIncompleteReceipt
from elspeth.web.sessions.continuation_paid_overflow import (
    LEDGER_TOKENS_MAX,
    MODEL_MAX_LENGTH,
    PaidOverflowCost,
    PaidOverflowIdentity,
    PaidOverflowUsage,
)
from elspeth.web.sessions.continuation_title_no_sdk_tail import prepare_title_no_sdk_tail_plan
from elspeth.web.sessions.continuation_title_paid_incomplete_tail import (
    build_title_paid_incomplete_tail_batch,
    prepare_title_paid_incomplete_tail_plan,
    title_combined_terminal_hold_contract,
)
from elspeth.web.sessions.continuation_title_paid_overflow_tail import prepare_title_paid_overflow_tail_plan
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_continuation_frames import initial_stack

_ATTEMPT = "00000000-0000-4000-8000-000000000005"


def _source() -> tuple[StoredContinuationSnapshot, PaidOverflowIdentity]:
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=20000))
    assert policy is not None
    base = initial_stack()
    frame = base.frames[0].model_copy(update={"ingress_phase": "title_pending", "title_attempt_id": _ATTEMPT})
    root = base.model_copy(
        update={
            "policy_hash": policy.policy_hash,
            "admission_policy_json": policy.model_dump_json(),
            "cursor": 1,
            "frames": (frame,),
            "effect_frontier": (EffectFrontierEntry(effect_id="provider:3:0", disposition="dispatched", receipt_id=None),),
            "pending_settlement_ids": (_ATTEMPT,),
        }
    )
    admitted = StoredContinuationSnapshot(encode_continuation_snapshot(root), 1, "00000000-0000-4000-8000-000000000006")
    identity = PaidOverflowIdentity(
        session_id=root.logical.session_id,
        operation_id=root.logical.operation_id,
        effect_key="provider:3:0",
        attempt_id=_ATTEMPT,
        session_operation_id="00000000-0000-4000-8000-000000000007",
        session_operation_epoch=1,
        ownership_generation=1,
        lease_sha256="a" * 64,
        policy_sha256=policy.policy_hash,
        source_checkpoint_id="00000000-0000-4000-8000-000000000008",
        source_cursor=0,
        source_snapshot_sha256=sha256(b"original").hexdigest(),
    )
    return admitted, identity


def _receipt(identity: PaidOverflowIdentity, now: datetime) -> PaidIncompleteReceipt:
    return PaidIncompleteReceipt(
        schema_version="composer.paid-incomplete.v1",
        identity=identity,
        producer="title",
        outcome="terminal_paid_incomplete",
        evidence_status="incomplete",
        retryable=False,
        provider_returned=True,
        reason="capture_budget_exceeded",
        capture_surface="provider_metadata",
        cost=PaidOverflowCost(status="known", amount="1.7976931348623157e308", source="litellm.cost_per_token"),
        usage=PaidOverflowUsage(
            status="observed",
            model="\x00" * MODEL_MAX_LENGTH,
            prompt_tokens=LEDGER_TOKENS_MAX,
            completion_tokens=LEDGER_TOKENS_MAX,
            cached_prompt_tokens=LEDGER_TOKENS_MAX,
            reasoning_tokens=LEDGER_TOKENS_MAX,
            finished_at=now.isoformat(),
        ),
    )


def test_title_paid_incomplete_tail_covers_receipt_events_and_full_root():
    admitted, identity = _source()
    plan = prepare_title_paid_incomplete_tail_plan(identity=identity, admitted=admitted, creator_sequence=1)
    now = datetime.now(UTC)
    batch = build_title_paid_incomplete_tail_batch(plan, receipt=_receipt(identity, now), published_at=now)
    terminal = decode_continuation_snapshot(batch.append.snapshot_json)
    assert batch.append.expected_checkpoint_id == admitted.checkpoint_id
    assert batch.append.expected_cursor == admitted.cursor
    assert tuple(item.codec for item in batch.prospective) == ("composer.paid-incomplete.v1", "event.v2", "event.v2", "snapshot.v2")
    assert terminal.effect_frontier[0].disposition == "paid_incomplete"
    assert terminal.pending_settlement_ids == ()
    assert batch.receipt_key.startswith("paid-incomplete:")
    assert batch.receipt_chunk_key == batch.prospective[0].key
    actual_bytes = sum(len(item.payload) for item in batch.prospective)
    assert actual_bytes <= plan.reserved_bytes
    with pytest.raises(AuditIntegrityError, match="predispatch terminal bound"):
        build_title_paid_incomplete_tail_batch(
            replace(plan, reserved_bytes=actual_bytes - 1), receipt=_receipt(identity, now), published_at=now
        )


def test_title_paid_incomplete_tail_rejects_foreign_identity_producer_and_policy():
    admitted, identity = _source()
    plan = prepare_title_paid_incomplete_tail_plan(identity=identity, admitted=admitted, creator_sequence=1)
    now = datetime.now(UTC)
    with pytest.raises(AuditIntegrityError, match="original producer or identity"):
        build_title_paid_incomplete_tail_batch(
            plan,
            receipt=_receipt(identity.model_copy(update={"attempt_id": "00000000-0000-4000-8000-000000000099"}), now),
            published_at=now,
        )
    with pytest.raises(AuditIntegrityError, match="original producer or identity"):
        build_title_paid_incomplete_tail_batch(
            plan, receipt=_receipt(identity, now).model_copy(update={"producer": "primary"}), published_at=now
        )
    with pytest.raises(AuditIntegrityError, match="original dispatched source"):
        prepare_title_paid_incomplete_tail_plan(
            identity=identity.model_copy(update={"policy_sha256": "b" * 64}), admitted=admitted, creator_sequence=1
        )


def test_title_combined_hold_covers_three_original_alternatives_and_rejects_tampering():
    admitted, identity = _source()
    paid = prepare_title_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=1)
    incomplete = prepare_title_paid_incomplete_tail_plan(identity=identity, admitted=admitted, creator_sequence=1)
    no_sdk = prepare_title_no_sdk_tail_plan(identity=identity, admitted=admitted, creator_sequence=1, requested_model="test-model")
    reserved, contract_sha256 = title_combined_terminal_hold_contract(paid, incomplete, no_sdk)
    assert reserved == max(paid.reserved_bytes, incomplete.reserved_bytes, no_sdk.reserved_bytes)
    assert len(contract_sha256) == 64
    with pytest.raises(AuditIntegrityError, match="original terminal alternatives"):
        title_combined_terminal_hold_contract(paid, replace(incomplete, reserved_bytes=incomplete.reserved_bytes - 1), no_sdk)
    with pytest.raises(AuditIntegrityError, match="original terminal alternatives"):
        title_combined_terminal_hold_contract(paid, incomplete, replace(no_sdk, requested_model="other-model"))
