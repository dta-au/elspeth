"""Checkpoint H covers the longer producer and every unknown outer event."""

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import EffectFrontierEntry
from elspeth.web.composer.continuation_checkpoint_no_sdk import CheckpointNoSdkRefusalReason
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_checkpoint_no_sdk_tail import build_checkpoint_no_sdk_tail_batch
from elspeth.web.sessions.continuation_checkpoint_paid_tail import (
    build_checkpoint_paid_overflow_tail_batch,
    prepare_checkpoint_paid_overflow_tail_plan,
)
from elspeth.web.sessions.continuation_checkpoint_terminal_hold import prepare_checkpoint_terminal_hold_plan
from elspeth.web.sessions.continuation_paid_overflow import (
    CANONICAL_INTEGER_MAX,
    LEDGER_TOKENS_MAX,
    MODEL_MAX_LENGTH,
    PaidOverflowCost,
    PaidOverflowIdentity,
    PaidOverflowReceipt,
    PaidOverflowUsage,
)
from elspeth.web.sessions.continuation_title_paid_overflow_tail import _maximum_receipt_bytes
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_continuation_frames import initial_stack

_ATTEMPT = "00000000-0000-4000-8000-000000000005"
_CHECKPOINT = "00000000-0000-4000-8000-000000000006"
_SESSION_OPERATION = "00000000-0000-4000-8000-000000000007"
_OUTER = "advance-early:" + "b" * 64
_CHILD = "provider:1:0"


def _source() -> tuple[StoredContinuationSnapshot, PaidOverflowIdentity]:
    policy = continuation_policy_v4_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=20000))
    assert policy is not None
    base = initial_stack()
    root = base.model_copy(
        update={
            "policy_hash": policy.policy_hash,
            "admission_policy_json": policy.model_dump_json(),
            "cursor": 1,
            "effect_frontier": (
                EffectFrontierEntry(effect_id=_OUTER, disposition="dispatched", receipt_id=None),
                EffectFrontierEntry(effect_id=_CHILD, disposition="dispatched", receipt_id=None),
            ),
            "pending_settlement_ids": (_ATTEMPT,),
        }
    )
    admitted = StoredContinuationSnapshot(encode_continuation_snapshot(root), 1, _CHECKPOINT)
    identity = PaidOverflowIdentity(
        session_id=root.logical.session_id,
        operation_id=root.logical.operation_id,
        effect_key=_CHILD,
        attempt_id=_ATTEMPT,
        session_operation_id=_SESSION_OPERATION,
        session_operation_epoch=1,
        ownership_generation=1,
        lease_sha256="a" * 64,
        policy_sha256=policy.policy_hash,
        source_checkpoint_id="00000000-0000-4000-8000-000000000008",
        source_cursor=0,
        source_snapshot_sha256=sha256(b"original").hexdigest(),
    )
    return admitted, identity


def _receipt(identity: PaidOverflowIdentity, now: datetime) -> PaidOverflowReceipt:
    return PaidOverflowReceipt(
        schema_version="composer.paid-overflow.v1",
        identity=identity,
        producer="checkpoint_advisor",
        outcome="terminal_paid_overflow",
        evidence_status="incomplete",
        retryable=False,
        reason="private_document_bytes",
        representation="response",
        measurement="at_least",
        observed_bytes=CANONICAL_INTEGER_MAX,
        limit_bytes=CANONICAL_INTEGER_MAX - 1,
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


def test_checkpoint_pre_sdk_hold_covers_longer_producer_and_outer_unknown() -> None:
    admitted, identity = _source()
    plan = prepare_checkpoint_paid_overflow_tail_plan(identity=identity, admitted=admitted, phase="early", creator_sequence=1)
    now = datetime.now(UTC)
    batch = build_checkpoint_paid_overflow_tail_batch(plan, receipt=_receipt(identity, now), published_at=now)
    producer_delta = len(canonical_json("checkpoint_advisor").encode()) - len(canonical_json("title").encode())
    assert plan.maximum_receipt_bytes == _maximum_receipt_bytes(identity) + producer_delta
    assert len(batch.receipt_json.encode()) <= plan.maximum_receipt_bytes
    assert sum(len(item.payload) for item in batch.prospective) <= plan.reserved_bytes
    assert tuple(item.codec for item in batch.prospective) == (
        "composer.paid-overflow.v1",
        "event.v2",
        "event.v2",
        "event.v2",
        "snapshot.v2",
    )
    final = decode_continuation_snapshot(batch.append.snapshot_json)
    assert [(item.effect_id, item.disposition) for item in final.effect_frontier] == [(_OUTER, "unknown"), (_CHILD, "paid_overflow")]
    assert final.pending_settlement_ids == ()
    with pytest.raises(AuditIntegrityError, match="exceeded its original pre-SDK bound"):
        build_checkpoint_paid_overflow_tail_batch(
            replace(plan, reserved_bytes=sum(len(item.payload) for item in batch.prospective) - 1),
            receipt=_receipt(identity, now),
            published_at=now,
        )


def test_checkpoint_tail_refuses_manual_advisor_receipt() -> None:
    admitted, identity = _source()
    plan = prepare_checkpoint_paid_overflow_tail_plan(identity=identity, admitted=admitted, phase="end", creator_sequence=1)
    now = datetime.now(UTC)
    manual = _receipt(identity, now).model_copy(update={"producer": "manual_advisor"})
    with pytest.raises(AuditIntegrityError, match="original paid source"):
        build_checkpoint_paid_overflow_tail_batch(plan, receipt=manual, published_at=now)


@pytest.mark.parametrize("reason", tuple(CheckpointNoSdkRefusalReason))
def test_checkpoint_pre_sdk_h_binds_paid_and_zero_use_alternatives(reason: CheckpointNoSdkRefusalReason) -> None:
    admitted, identity = _source()
    combined = prepare_checkpoint_terminal_hold_plan(
        identity=identity,
        admitted=admitted,
        phase="end",
        creator_sequence=1,
        requested_model="test/model",
    )
    assert combined.reserved_bytes == max(combined.paid_overflow.reserved_bytes, combined.no_sdk.reserved_bytes)
    assert combined.contract_sha256 != combined.paid_overflow.fallback_contract_sha256
    assert combined.contract_sha256 != combined.no_sdk.contract_sha256
    changed_model = prepare_checkpoint_terminal_hold_plan(
        identity=identity,
        admitted=admitted,
        phase="end",
        creator_sequence=1,
        requested_model="other/model",
    )
    assert changed_model.contract_sha256 != combined.contract_sha256
    now = datetime.now(UTC)
    receipt_key = str(uuid4())
    batch = build_checkpoint_no_sdk_tail_batch(
        combined.no_sdk,
        reason=reason,
        receipt_key=receipt_key,
        published_at=now,
    )
    assert sum(len(item.payload) for item in batch.prospective) <= combined.reserved_bytes
    assert '"usage":"zero"' in batch.receipt_json
    final = decode_continuation_snapshot(batch.append.snapshot_json)
    assert [(item.effect_id, item.disposition) for item in final.effect_frontier] == [(_OUTER, "unknown"), (_CHILD, "refused")]
    assert final.pending_settlement_ids == ()
    with pytest.raises(AuditIntegrityError, match="issued UUID"):
        build_checkpoint_no_sdk_tail_batch(
            combined.no_sdk,
            reason=reason,
            receipt_key="not-an-issued-key",
            published_at=now,
        )
