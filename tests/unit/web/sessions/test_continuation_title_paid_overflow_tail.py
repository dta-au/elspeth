"""The V4 TITLE fallback reservation covers its whole immutable terminal batch."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import EffectFrontierEntry
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_paid_overflow import (
    CANONICAL_INTEGER_MAX,
    LEDGER_TOKENS_MAX,
    MODEL_MAX_LENGTH,
    PaidOverflowCost,
    PaidOverflowIdentity,
    PaidOverflowReceipt,
    PaidOverflowUsage,
)
from elspeth.web.sessions.continuation_title_paid_overflow_tail import (
    build_title_paid_overflow_tail_batch,
    prepare_title_paid_overflow_tail_plan,
)
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_continuation_frames import initial_stack

_ATTEMPT = "00000000-0000-4000-8000-000000000005"
_CHECKPOINT = "00000000-0000-4000-8000-000000000006"
_SESSION_OPERATION = "00000000-0000-4000-8000-000000000007"


def _source():
    policy = continuation_policy_v4_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=20000))
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
    admitted = StoredContinuationSnapshot(encode_continuation_snapshot(root), 1, _CHECKPOINT)
    identity = PaidOverflowIdentity(
        session_id=root.logical.session_id,
        operation_id=root.logical.operation_id,
        effect_key="provider:3:0",
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
        producer="title",
        outcome="terminal_paid_overflow",
        evidence_status="incomplete",
        retryable=False,
        reason="private_document_bytes",
        representation="response",
        measurement="at_least",
        observed_bytes=1_048_577,
        limit_bytes=1_048_576,
        cost=PaidOverflowCost(status="known", amount="0.000001", source="response_usage.cost"),
        usage=PaidOverflowUsage(
            status="observed",
            model="test-model",
            prompt_tokens=1,
            completion_tokens=2,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            finished_at=now.isoformat(),
        ),
    )


def test_title_tail_plan_covers_exact_receipt_events_and_full_root():
    admitted, identity = _source()
    plan = prepare_title_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=1)
    now = datetime.now(UTC)
    batch = build_title_paid_overflow_tail_batch(plan, receipt=_receipt(identity, now), published_at=now)
    assert sum(len(item.payload) for item in batch.prospective) <= plan.reserved_bytes
    assert tuple(item.codec for item in batch.prospective) == ("composer.paid-overflow.v1", "event.v2", "event.v2", "snapshot.v2")
    assert batch.append.expected_checkpoint_id == admitted.checkpoint_id
    assert batch.append.expected_cursor == admitted.cursor
    assert batch.receipt_key.startswith("paid-overflow:")
    assert batch.receipt_chunk_key == batch.prospective[0].key


def test_title_tail_plan_refuses_wrong_policy_and_receipt_identity():
    admitted, identity = _source()
    plan = prepare_title_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=1)
    now = datetime.now(UTC)
    wrong = _receipt(identity.model_copy(update={"attempt_id": "00000000-0000-4000-8000-000000000099"}), now)
    with pytest.raises(AuditIntegrityError, match="original source identity"):
        build_title_paid_overflow_tail_batch(plan, receipt=wrong, published_at=now)
    with pytest.raises(AuditIntegrityError, match="sole dispatched provider source"):
        prepare_title_paid_overflow_tail_plan(
            identity=identity.model_copy(update={"policy_sha256": "b" * 64}), admitted=admitted, creator_sequence=1
        )
    with pytest.raises(AuditIntegrityError, match="sole dispatched provider source"):
        prepare_title_paid_overflow_tail_plan(
            identity=identity.model_copy(update={"effect_key": "provider:0:0"}), admitted=admitted, creator_sequence=1
        )
    foreign_producer = _receipt(identity, now).model_copy(update={"producer": "primary"})
    with pytest.raises(AuditIntegrityError, match="closed producer"):
        build_title_paid_overflow_tail_batch(plan, receipt=foreign_producer, published_at=now)


@pytest.mark.parametrize(
    ("reason", "representation", "measurement"),
    (
        ("private_document_bytes", "physical_audit", "at_least"),
        ("private_document_bytes", "response", "at_least"),
        ("retained_payload_bytes", "retained_tail", "exact"),
    ),
)
def test_title_tail_envelope_covers_escaped_extrema_and_rejects_one_byte_short(reason, representation, measurement):
    admitted, identity = _source()
    plan = prepare_title_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=1)
    now = datetime.now(UTC)
    receipt = _receipt(identity, now).model_copy(
        update={
            "reason": reason,
            "representation": representation,
            "measurement": measurement,
            "observed_bytes": CANONICAL_INTEGER_MAX,
            "limit_bytes": CANONICAL_INTEGER_MAX - 1,
            "cost": PaidOverflowCost(status="known", amount="1.7976931348623157e308", source="litellm.cost_per_token"),
            "usage": PaidOverflowUsage(
                status="observed",
                model="\x00" * MODEL_MAX_LENGTH,
                prompt_tokens=LEDGER_TOKENS_MAX,
                completion_tokens=LEDGER_TOKENS_MAX,
                cached_prompt_tokens=LEDGER_TOKENS_MAX,
                reasoning_tokens=LEDGER_TOKENS_MAX,
                finished_at=now.isoformat(),
            ),
        }
    )
    batch = build_title_paid_overflow_tail_batch(plan, receipt=receipt, published_at=now)
    actual_bytes = sum(len(item.payload) for item in batch.prospective)
    assert "\\u0000" in batch.receipt_json
    assert actual_bytes <= plan.reserved_bytes
    assert len(batch.receipt_json.encode("utf-8")) <= plan.maximum_receipt_bytes
    with pytest.raises(AuditIntegrityError, match="exceeded its original complete terminal-tail shape"):
        build_title_paid_overflow_tail_batch(replace(plan, reserved_bytes=actual_bytes - 1), receipt=receipt, published_at=now)


def test_title_tail_measures_large_existing_root_and_decimal_cursor_boundary():
    admitted, identity = _source()
    root = decode_continuation_snapshot(admitted.payload_json)
    frame = root.frames[0].model_copy(update={"request_json": json.dumps({"content": '\\"é' * 1300})})
    enlarged = root.model_copy(update={"cursor": 9, "frames": (frame,)})
    stored = StoredContinuationSnapshot(encode_continuation_snapshot(enlarged), 9, admitted.checkpoint_id)
    source = identity.model_copy(update={"source_cursor": 8})
    plan = prepare_title_paid_overflow_tail_plan(identity=source, admitted=stored, creator_sequence=1)
    now = datetime.now(UTC)
    batch = build_title_paid_overflow_tail_batch(plan, receipt=_receipt(source, now), published_at=now)
    assert batch.append.expected_cursor == 9
    terminal = decode_continuation_snapshot(batch.append.snapshot_json)
    assert terminal.frames[0].request_json == frame.request_json
    assert terminal.cursor == 11
    assert sum(len(item.payload) for item in batch.prospective) <= plan.reserved_bytes
    assert plan.terminal_snapshot_bytes > 5000


def test_title_tail_schema_bound_drift_fails_before_reservation(monkeypatch):
    from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowUsage as UsageModel

    schema = UsageModel.model_json_schema()
    schema["properties"]["model"]["anyOf"][0]["maxLength"] += 1
    monkeypatch.setattr(UsageModel, "model_json_schema", classmethod(lambda cls: schema))
    admitted, identity = _source()
    with pytest.raises(AuditIntegrityError, match="bound no longer covers"):
        prepare_title_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=1)


def test_title_tail_closed_producer_domain_drift_fails_before_reservation(monkeypatch):
    from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowReceipt

    schema = PaidOverflowReceipt.model_json_schema()
    schema["properties"]["producer"]["enum"].append("unreviewed_paid_producer")
    monkeypatch.setattr(PaidOverflowReceipt, "model_json_schema", classmethod(lambda cls: schema))
    admitted, identity = _source()
    with pytest.raises(AuditIntegrityError, match="bound no longer covers"):
        prepare_title_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=1)
