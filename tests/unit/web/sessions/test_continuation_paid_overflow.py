"""Codec controls only: expected identities are not SQL custody proofs."""

from __future__ import annotations

import json

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.sessions.continuation_paid_overflow import (
    PaidOverflowCost,
    PaidOverflowIdentity,
    PaidOverflowReceipt,
    PaidOverflowUsage,
    decode_paid_overflow_receipt,
    encode_paid_overflow_receipt,
)


@pytest.fixture
def receipt() -> PaidOverflowReceipt:
    identity = PaidOverflowIdentity(
        session_id="00000000-0000-4000-8000-000000000001",
        operation_id="00000000-0000-4000-8000-000000000002",
        effect_key="provider:1:0",
        attempt_id="00000000-0000-4000-8000-000000000003",
        session_operation_id="00000000-0000-4000-8000-000000000004",
        session_operation_epoch=1,
        ownership_generation=2,
        lease_sha256="a" * 64,
        policy_sha256="b" * 64,
        source_checkpoint_id="00000000-0000-4000-8000-000000000005",
        source_cursor=3,
        source_snapshot_sha256="c" * 64,
    )
    return PaidOverflowReceipt(
        schema_version="composer.paid-overflow.v1",
        identity=identity,
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
    )


@pytest.mark.parametrize("measurement", ["exact", "at_least"])
@pytest.mark.parametrize("known", [False, True])
def test_round_trip_preserves_cost_and_observation(receipt, measurement, known):
    cost = PaidOverflowCost(status="known", amount="0.000123", source="response_usage.cost") if known else receipt.cost
    original = receipt.model_copy(update={"measurement": measurement, "cost": cost})
    encoded = encode_paid_overflow_receipt(original, expected=receipt.identity)
    restored = decode_paid_overflow_receipt(encoded, expected=receipt.identity)
    assert restored == original
    assert restored.cost.amount == ("0.000123" if known else None)
    assert restored.retryable is False
    assert len(encoded.encode("utf-8")) <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("session_id", "00000000-0000-4000-8000-000000000099"),
        ("operation_id", "00000000-0000-4000-8000-000000000099"),
        ("attempt_id", "00000000-0000-4000-8000-000000000099"),
        ("session_operation_id", "00000000-0000-4000-8000-000000000099"),
        ("session_operation_epoch", 2),
        ("ownership_generation", 3),
        ("effect_key", "provider:1:1"),
        ("lease_sha256", "d" * 64),
        ("policy_sha256", "d" * 64),
        ("source_checkpoint_id", "00000000-0000-4000-8000-000000000099"),
        ("source_cursor", 4),
        ("source_snapshot_sha256", "d" * 64),
    ],
)
def test_rejects_forged_source_binding(receipt, field, changed):
    encoded = encode_paid_overflow_receipt(receipt, expected=receipt.identity)
    assert decode_paid_overflow_receipt(encoded, expected=receipt.identity) == receipt
    value = json.loads(encoded)
    value["identity"][field] = changed
    with pytest.raises(AuditIntegrityError, match="original source identity"):
        decode_paid_overflow_receipt(canonical_json(value), expected=receipt.identity)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("schema_version", "composer.paid-overflow.v2"),
        ("outcome", "success"),
        ("evidence_status", "complete"),
        ("retryable", True),
        ("retryable", 0),
        ("producer", "server"),
        ("reason", "storage_error"),
        ("representation", "raw_body"),
        ("measurement", "estimated"),
        ("observed_bytes", True),
        ("observed_bytes", 1_048_576),
        ("observed_bytes", 1_048_575),
        ("limit_bytes", 0),
    ],
)
def test_rejects_malformed_status_and_observation(receipt, field, changed):
    value = receipt.model_dump(mode="json")
    value[field] = changed
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        decode_paid_overflow_receipt(canonical_json(value), expected=receipt.identity)


@pytest.mark.parametrize(
    "cost",
    [
        {"status": "unknown", "amount": "0", "source": "not_available"},
        {"status": "known", "amount": None, "source": "response_usage.cost"},
        {"status": "known", "amount": "1", "source": "not_available"},
        {"status": "known", "amount": "NaN", "source": "response_usage.cost"},
        {"status": "known", "amount": "-1", "source": "response_usage.cost"},
        {"status": "known", "amount": 0.1, "source": "response_usage.cost"},
    ],
)
def test_unknown_cost_is_not_zero_and_known_cost_is_exact(receipt, cost):
    value = receipt.model_dump(mode="json")
    value["cost"] = cost
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        decode_paid_overflow_receipt(canonical_json(value), expected=receipt.identity)


def test_receipt_budget_boundary_on_actual_canonical_bytes(receipt):
    encoded = encode_paid_overflow_receipt(receipt, expected=receipt.identity)
    size = len(encoded.encode("utf-8"))
    for budget in (size, size + 1):
        assert encode_paid_overflow_receipt(receipt, expected=receipt.identity, max_bytes=budget) == encoded
        assert decode_paid_overflow_receipt(encoded, expected=receipt.identity, max_bytes=budget) == receipt
    with pytest.raises(AuditIntegrityError, match="byte budget"):
        encode_paid_overflow_receipt(receipt, expected=receipt.identity, max_bytes=size - 1)
    with pytest.raises(AuditIntegrityError, match="byte budget"):
        decode_paid_overflow_receipt(encoded, expected=receipt.identity, max_bytes=size - 1)


def test_rejects_oversized_input_before_parsing(receipt):
    oversized = "!" * (CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1)
    with pytest.raises(AuditIntegrityError, match="byte budget"):
        decode_paid_overflow_receipt(oversized, expected=receipt.identity)


def test_no_raw_provider_fields_or_missing_version(receipt):
    value = receipt.model_dump(mode="json")
    value["provider_content"] = "must not be retained"
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        decode_paid_overflow_receipt(canonical_json(value), expected=receipt.identity)
    del value["provider_content"]
    del value["schema_version"]
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        decode_paid_overflow_receipt(canonical_json(value), expected=receipt.identity)


def test_encoder_revalidates_unchecked_nested_copy(receipt):
    invalid = receipt.model_copy(update={"cost": receipt.cost.model_copy(update={"amount": "0"})})
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        encode_paid_overflow_receipt(invalid, expected=receipt.identity)


def test_encoder_rejects_unchecked_integer_retryable(receipt):
    invalid = receipt.model_copy(update={"retryable": 0})
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        encode_paid_overflow_receipt(invalid, expected=receipt.identity)


@pytest.mark.parametrize("tokens", [None, 0, 2**31 - 1])
def test_actual_ledger_usage_round_trip_preserves_unknown_and_zero(receipt, tokens):
    usage = PaidOverflowUsage(
        status="observed",
        model="provider/exact-model",
        prompt_tokens=tokens,
        completion_tokens=tokens,
        cached_prompt_tokens=tokens,
        reasoning_tokens=tokens,
        finished_at="2026-11-02T01:02:03.123456+00:00",
    )
    original = receipt.model_copy(update={"usage": usage})
    encoded = encode_paid_overflow_receipt(original, expected=receipt.identity)
    restored = decode_paid_overflow_receipt(encoded, expected=receipt.identity)
    assert restored.usage == usage
    assert restored.identity.attempt_id == receipt.identity.attempt_id
    assert restored.cost == receipt.cost


def test_usage_unavailable_preserves_original_identity_without_inventing_model(receipt):
    usage = PaidOverflowUsage(
        status="unavailable",
        model=None,
        prompt_tokens=None,
        completion_tokens=None,
        cached_prompt_tokens=None,
        reasoning_tokens=None,
        finished_at=None,
    )
    original = receipt.model_copy(update={"usage": usage})
    restored = decode_paid_overflow_receipt(encode_paid_overflow_receipt(original, expected=receipt.identity), expected=receipt.identity)
    assert restored == original


def test_encoder_revalidates_unchecked_usage_copy(receipt):
    usage = PaidOverflowUsage(
        status="observed",
        model="provider/model",
        prompt_tokens=1,
        completion_tokens=None,
        cached_prompt_tokens=None,
        reasoning_tokens=None,
        finished_at="2026-11-02T01:02:03+00:00",
    )
    invalid = receipt.model_copy(update={"usage": usage.model_copy(update={"completion_tokens": True})})
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        encode_paid_overflow_receipt(invalid, expected=receipt.identity)


@pytest.mark.parametrize("model", ["provider/exact-model", "unrelated ordinary text", "provider/api-key-model"])
def test_usage_model_credential_guard_accepts_unrelated_text(receipt, model):
    usage = PaidOverflowUsage(
        status="observed",
        model=model,
        prompt_tokens=1,
        completion_tokens=2,
        cached_prompt_tokens=None,
        reasoning_tokens=None,
        finished_at="2026-11-02T01:02:03+00:00",
    )
    original = receipt.model_copy(update={"usage": usage})
    assert (
        decode_paid_overflow_receipt(encode_paid_overflow_receipt(original, expected=receipt.identity), expected=receipt.identity)
        == original
    )


@pytest.mark.parametrize("boundary", ["admission", "encoder", "decoder"])
def test_planted_credential_model_remains_a_credential_refusal(receipt, boundary):
    planted = "sk-" + "a" * 24
    fields = {
        "status": "observed",
        "model": planted,
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "cached_prompt_tokens": None,
        "reasoning_tokens": None,
        "finished_at": "2026-11-02T01:02:03+00:00",
    }
    # The fixture already proves positive size overflow; credential refusal
    # still wins rather than yielding an overflow or unavailable receipt.
    with pytest.raises(CredentialMaterialRefused) as caught:
        if boundary == "admission":
            PaidOverflowUsage.model_validate(fields)
        elif boundary == "encoder":
            unchecked = PaidOverflowUsage.model_construct(**fields)
            encode_paid_overflow_receipt(receipt.model_copy(update={"usage": unchecked}), expected=receipt.identity)
        else:
            document = receipt.model_dump(mode="json")
            document["usage"] = fields
            decode_paid_overflow_receipt(canonical_json(document), expected=receipt.identity)
    assert caught.value.surface == "composer_paid_overflow_usage_model"
    assert planted not in str(caught.value)
    assert planted not in repr(caught.value.to_payload())


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("model", None),
        ("model", " "),
        ("model", "m" * 257),
        ("prompt_tokens", True),
        ("completion_tokens", -1),
        ("cached_prompt_tokens", 2**31),
        ("reasoning_tokens", "1"),
        ("finished_at", None),
        ("finished_at", "2026-11-02T01:02:03"),
        ("finished_at", "2026-11-02T01:02:03Z"),
        ("finished_at", "invalid"),
        ("status", "success"),
        ("provider_content", "must not be retained"),
    ],
)
def test_usage_rejects_unbounded_or_untruthful_ledger_projection(receipt, field, changed):
    value = receipt.model_dump(mode="json")
    value["usage"] = {
        "status": "observed",
        "model": "provider/exact-model",
        "prompt_tokens": 1,
        "completion_tokens": None,
        "cached_prompt_tokens": 0,
        "reasoning_tokens": None,
        "finished_at": "2026-11-02T01:02:03+00:00",
    }
    value["usage"][field] = changed
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        decode_paid_overflow_receipt(canonical_json(value), expected=receipt.identity)


@pytest.mark.parametrize(
    "field", ["model", "prompt_tokens", "completion_tokens", "cached_prompt_tokens", "reasoning_tokens", "finished_at"]
)
def test_unavailable_usage_cannot_claim_actual_fields(receipt, field):
    value = receipt.model_dump(mode="json")
    value["usage"] = {
        "status": "unavailable",
        "model": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "cached_prompt_tokens": None,
        "reasoning_tokens": None,
        "finished_at": None,
    }
    value["usage"][field] = "provider/model" if field == "model" else "2026-11-02T01:02:03+00:00" if field == "finished_at" else 0
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        decode_paid_overflow_receipt(canonical_json(value), expected=receipt.identity)


def test_duplicate_keys_and_noncanonical_json_are_refused(receipt):
    encoded = encode_paid_overflow_receipt(receipt, expected=receipt.identity)
    with pytest.raises(AuditIntegrityError, match="strict JSON"):
        decode_paid_overflow_receipt('{"schema_version":"x",' + encoded[1:], expected=receipt.identity)
    with pytest.raises(AuditIntegrityError, match="not canonical"):
        decode_paid_overflow_receipt(" " + encoded, expected=receipt.identity)


@pytest.mark.parametrize("budget", [True, 0, -1, CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1])
def test_caller_cannot_widen_private_cap(receipt, budget):
    with pytest.raises(ValueError, match="private document limit"):
        encode_paid_overflow_receipt(receipt, expected=receipt.identity, max_bytes=budget)
