"""PRIMARY reserves the largest original terminal alternative before dispatch."""

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import EffectFrontierEntry
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_paid_incomplete import PaidIncompleteReceipt, maximum_paid_incomplete_receipt_bytes
from elspeth.web.sessions.continuation_paid_overflow import (
    LEDGER_TOKENS_MAX,
    MODEL_MAX_LENGTH,
    PaidOverflowCost,
    PaidOverflowIdentity,
    PaidOverflowUsage,
)
from elspeth.web.sessions.continuation_primary_paid_incomplete_tail import (
    build_primary_paid_incomplete_tail_batch,
    prepare_primary_paid_incomplete_tail_plan,
)
from elspeth.web.sessions.continuation_primary_paid_overflow_no_sdk import prepare_primary_no_sdk_tail_plan
from elspeth.web.sessions.continuation_primary_paid_overflow_tail import prepare_primary_paid_overflow_tail_plan
from elspeth.web.sessions.continuation_primary_terminal_reservation import primary_terminal_reservation
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_continuation_cut_coverage import _compose
from tests.unit.web.composer.test_continuation_frames import initial_stack

_INPUT_SHA256 = sha256(b"primary-tail-structural-input").hexdigest()


def _source():
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    base = initial_stack()
    frame = _compose(
        phase="awaiting_response",
        provider_effect_key="provider:0:0",
        provider_input_sha256=_INPUT_SHA256,
        pending_response=None,
    )
    root = base.model_copy(
        update={
            "policy_hash": policy.policy_hash,
            "admission_policy_json": policy.model_dump_json(),
            "cursor": 9,
            "frames": (frame,),
            "effect_frontier": (EffectFrontierEntry(effect_id="provider:0:0", disposition="dispatched", receipt_id=None),),
        }
    )
    admitted = StoredContinuationSnapshot(encode_continuation_snapshot(root), 9, "00000000-0000-4000-8000-000000000006")
    identity = PaidOverflowIdentity(
        session_id=root.logical.session_id,
        operation_id=root.logical.operation_id,
        effect_key="provider:0:0",
        attempt_id="00000000-0000-4000-8000-000000000005",
        session_operation_id="00000000-0000-4000-8000-000000000007",
        session_operation_epoch=1,
        ownership_generation=1,
        lease_sha256="a" * 64,
        policy_sha256=policy.policy_hash,
        source_checkpoint_id="00000000-0000-4000-8000-000000000008",
        source_cursor=8,
        source_snapshot_sha256=sha256(b"original").hexdigest(),
    )
    return admitted, identity


def _receipt(identity):
    return PaidIncompleteReceipt(
        schema_version="composer.paid-incomplete.v1",
        identity=identity,
        producer="primary",
        outcome="terminal_paid_incomplete",
        evidence_status="incomplete",
        retryable=False,
        provider_returned=True,
        reason="missing_required_field",
        capture_surface="provider_metadata",
        cost=PaidOverflowCost(status="known", amount="1.7976931348623157e308", source="litellm.cost_per_token"),
        usage=PaidOverflowUsage(
            status="observed",
            model="\x00" * MODEL_MAX_LENGTH,
            prompt_tokens=LEDGER_TOKENS_MAX,
            completion_tokens=LEDGER_TOKENS_MAX,
            cached_prompt_tokens=LEDGER_TOKENS_MAX,
            reasoning_tokens=LEDGER_TOKENS_MAX,
            finished_at="2026-11-02T10:00:00+00:00",
        ),
    )


def _plans():
    admitted, identity = _source()
    paid = prepare_primary_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=1)
    no_sdk = prepare_primary_no_sdk_tail_plan(
        admitted=admitted,
        attempt_id=identity.attempt_id,
        requested_model="test-model",
        input_sha256=_INPUT_SHA256,
        effect_key=identity.effect_key,
        creator_sequence=1,
        creator_generation=1,
        claim_attempt=1,
        operation_epoch=1,
        action_chunk=None,
    )
    return paid, no_sdk


def test_incomplete_tail_covers_escaped_maxima_and_decimal_cursor_growth():
    admitted, identity = _source()
    plan = prepare_primary_paid_incomplete_tail_plan(identity=identity, admitted=admitted, creator_sequence=1)
    batch = build_primary_paid_incomplete_tail_batch(plan, receipt=_receipt(identity), published_at=datetime.now(UTC))
    assert "\\u0000" in batch.receipt_json
    assert len(batch.receipt_json.encode()) <= plan.maximum_receipt_bytes <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    actual = sum(len(chunk.payload) for chunk in batch.prospective)
    assert actual <= plan.reserved_bytes
    assert plan.maximum_receipt_bytes == maximum_paid_incomplete_receipt_bytes(identity, "primary")
    assert plan.reserved_bytes == plan.maximum_receipt_bytes + sum(plan.terminal_event_bytes) + plan.terminal_snapshot_bytes
    assert decode_continuation_snapshot(batch.append.snapshot_json).cursor == 11
    assert tuple(chunk.codec for chunk in batch.prospective) == ("composer.paid-incomplete.v1", "event.v2", "event.v2", "snapshot.v2")
    with pytest.raises(AuditIntegrityError, match="complete tail bound"):
        build_primary_paid_incomplete_tail_batch(
            replace(plan, reserved_bytes=actual - 1), receipt=_receipt(identity), published_at=datetime.now(UTC)
        )
    with pytest.raises(AuditIntegrityError, match="receipt exceeded the original closed bound"):
        build_primary_paid_incomplete_tail_batch(
            replace(plan, maximum_receipt_bytes=len(batch.receipt_json.encode("utf-8")) - 1),
            receipt=_receipt(identity),
            published_at=datetime.now(UTC),
        )


def test_combined_reserve_covers_each_alternative_and_binds_attempt():
    paid, no_sdk = _plans()
    incomplete, reserved, contract = primary_terminal_reservation(paid, no_sdk)
    assert reserved == max(paid.reserved_bytes, incomplete.reserved_bytes, no_sdk.reserved_bytes)
    assert incomplete.identity == paid.identity
    assert incomplete.admitted == paid.admitted
    for changed in (replace(paid, reserved_bytes=reserved + 1), replace(paid, fallback_contract_sha256="e" * 64)):
        assert primary_terminal_reservation(changed, no_sdk)[2] != contract
    assert primary_terminal_reservation(paid, replace(no_sdk, reserved_bytes=reserved + 1))[1] == reserved + 1
    with pytest.raises(AuditIntegrityError, match="original attempt or admitted head"):
        primary_terminal_reservation(paid, replace(no_sdk, attempt_id="00000000-0000-4000-8000-000000000099"))
    with pytest.raises(AuditIntegrityError, match="original attempt or admitted head"):
        primary_terminal_reservation(paid, replace(no_sdk, admitted=replace(no_sdk.admitted, checkpoint_id="foreign")))


def test_incomplete_tail_rejects_foreign_receipt_and_changed_policy():
    admitted, identity = _source()
    plan = prepare_primary_paid_incomplete_tail_plan(identity=identity, admitted=admitted, creator_sequence=1)
    wrong = identity.model_copy(update={"attempt_id": "00000000-0000-4000-8000-000000000099"})
    with pytest.raises(AuditIntegrityError, match="original attempt identity"):
        build_primary_paid_incomplete_tail_batch(plan, receipt=_receipt(wrong), published_at=datetime.now(UTC))
    with pytest.raises(AuditIntegrityError, match="producer"):
        build_primary_paid_incomplete_tail_batch(
            plan, receipt=_receipt(identity).model_copy(update={"producer": "title"}), published_at=datetime.now(UTC)
        )
    with pytest.raises(AuditIntegrityError, match="original source"):
        prepare_primary_paid_incomplete_tail_plan(
            identity=identity.model_copy(update={"policy_sha256": "b" * 64}), admitted=admitted, creator_sequence=1
        )


def test_incomplete_tail_schema_drift_refuses_before_reservation(monkeypatch):
    schema = PaidIncompleteReceipt.model_json_schema()
    schema["properties"]["provider_returned"]["const"] = False
    monkeypatch.setattr(PaidIncompleteReceipt, "model_json_schema", classmethod(lambda cls: schema))
    admitted, identity = _source()
    with pytest.raises(AuditIntegrityError, match="bound no longer covers"):
        prepare_primary_paid_incomplete_tail_plan(identity=identity, admitted=admitted, creator_sequence=1)


def test_incomplete_tail_cannot_reinterpret_a_legacy_policy():
    admitted, identity = _source()
    legacy = initial_stack()
    root = decode_continuation_snapshot(admitted.payload_json).model_copy(
        update={"policy_hash": legacy.policy_hash, "admission_policy_json": legacy.admission_policy_json}
    )
    stored = replace(admitted, payload_json=encode_continuation_snapshot(root))
    with pytest.raises(AuditIntegrityError, match="original source"):
        prepare_primary_paid_incomplete_tail_plan(
            identity=identity.model_copy(update={"policy_sha256": legacy.policy_hash}), admitted=stored, creator_sequence=1
        )


def test_incomplete_tail_shared_usage_bound_drift_refuses_before_reservation(monkeypatch):
    schema = PaidOverflowUsage.model_json_schema()
    schema["properties"]["model"]["anyOf"][0]["maxLength"] += 1
    monkeypatch.setattr(PaidOverflowUsage, "model_json_schema", classmethod(lambda cls: schema))
    admitted, identity = _source()
    with pytest.raises(AuditIntegrityError, match="bound no longer covers"):
        prepare_primary_paid_incomplete_tail_plan(identity=identity, admitted=admitted, creator_sequence=1)


def test_incomplete_tail_named_producer_domain_drift_refuses_before_reservation(monkeypatch):
    schema = PaidIncompleteReceipt.model_json_schema()
    schema["$defs"]["PaidIncompleteProducer"]["enum"].append("unreviewed_paid_producer")
    monkeypatch.setattr(PaidIncompleteReceipt, "model_json_schema", classmethod(lambda cls: schema))
    admitted, identity = _source()
    with pytest.raises(AuditIntegrityError, match="bound no longer covers"):
        prepare_primary_paid_incomplete_tail_plan(identity=identity, admitted=admitted, creator_sequence=1)
