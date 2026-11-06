"""The TITLE zero-use alternative is a closed source-bound terminal batch."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_title_no_sdk import TitleNoSdkRefusalReason
from elspeth.web.sessions.continuation_paid_overflow import MODEL_MAX_LENGTH
from elspeth.web.sessions.continuation_title_no_sdk_tail import build_title_no_sdk_tail_batch, prepare_title_no_sdk_tail_plan
from tests.unit.web.sessions.test_continuation_title_paid_incomplete_tail import _source


@pytest.mark.parametrize("reason", tuple(TitleNoSdkRefusalReason))
def test_title_no_sdk_tail_covers_every_reason_and_exact_original_model(reason):
    admitted, identity = _source()
    model = "é" * (MODEL_MAX_LENGTH // 2)
    plan = prepare_title_no_sdk_tail_plan(identity=identity, admitted=admitted, creator_sequence=1, requested_model=model)
    receipt_key = str(uuid4())
    batch = build_title_no_sdk_tail_batch(plan, reason=reason, receipt_key=receipt_key, published_at=datetime.now(UTC))
    receipt, error = parse_json_strict(batch.receipt_json)
    assert error is None
    assert receipt["attempt_id"] == identity.attempt_id
    assert receipt["requested_model"] == model
    assert receipt["usage"] == "zero"
    assert receipt["reason"] == reason.value
    assert receipt["receipt_key"] == receipt_key
    assert batch.receipt_chunk_key == batch.prospective[0].key
    assert tuple(item.codec for item in batch.prospective) == ("composer.title-no-sdk.v1", "event.v2", "event.v2", "snapshot.v2")
    terminal = decode_continuation_snapshot(batch.append.snapshot_json)
    assert terminal.pending_settlement_ids == ()
    assert terminal.frames == decode_continuation_snapshot(admitted.payload_json).frames
    assert terminal.effect_frontier[0].disposition == "refused"
    assert terminal.effect_frontier[0].receipt_id == receipt_key
    actual = sum(len(item.payload) for item in batch.prospective)
    assert actual <= plan.reserved_bytes
    with pytest.raises(AuditIntegrityError, match="original pre-SDK bound"):
        build_title_no_sdk_tail_batch(
            replace(plan, reserved_bytes=actual - 1), reason=reason, receipt_key=receipt_key, published_at=datetime.now(UTC)
        )


@pytest.mark.parametrize("field,value", (("effect_key", "provider:0:0"), ("policy_sha256", "b" * 64), ("source_cursor", 1)))
def test_title_no_sdk_tail_rejects_a_foreign_source(field, value):
    admitted, identity = _source()
    with pytest.raises(AuditIntegrityError, match="original dispatched child"):
        prepare_title_no_sdk_tail_plan(
            identity=identity.model_copy(update={field: value}), admitted=admitted, creator_sequence=1, requested_model="model"
        )


def test_title_no_sdk_tail_rejects_unpaid_root_and_foreign_receipt_identity():
    admitted, identity = _source()
    root = decode_continuation_snapshot(admitted.payload_json)
    unentered = replace(admitted, payload_json=encode_continuation_snapshot(root.model_copy(update={"pending_settlement_ids": ()})))
    with pytest.raises(AuditIntegrityError, match="original dispatched child"):
        prepare_title_no_sdk_tail_plan(identity=identity, admitted=unentered, creator_sequence=1, requested_model="model")
    plan = prepare_title_no_sdk_tail_plan(identity=identity, admitted=admitted, creator_sequence=1, requested_model="model")
    with pytest.raises(AuditIntegrityError, match="issued UUID"):
        build_title_no_sdk_tail_batch(
            plan,
            reason=TitleNoSdkRefusalReason.COST_REFUSED,
            receipt_key="title:no-sdk:forged",
            published_at=datetime.now(UTC),
        )


def test_title_no_sdk_tail_refuses_a_historical_v3_policy_and_unbounded_model():
    admitted, identity = _source()
    root = decode_continuation_snapshot(admitted.payload_json)
    old_policy = json.loads(root.admission_policy_json)
    old_policy["schema_version"] = "composer.continuation.policy.v3"
    old_policy_json = canonical_json(old_policy)
    old_hash = sha256(old_policy_json.encode("utf-8")).hexdigest()
    historical_document = json.loads(admitted.payload_json)
    historical_document.update(policy_hash=old_hash, admission_policy_json=old_policy_json)
    historical = replace(admitted, payload_json=canonical_json(historical_document))
    with pytest.raises(AuditIntegrityError, match="unsupported schema version"):
        prepare_title_no_sdk_tail_plan(
            identity=identity.model_copy(update={"policy_sha256": old_hash}),
            admitted=historical,
            creator_sequence=1,
            requested_model="model",
        )
    with pytest.raises(ValueError, match="safe retention bound"):
        prepare_title_no_sdk_tail_plan(identity=identity, admitted=admitted, creator_sequence=1, requested_model="m" * 257)
