"""SDK terminal codec controls; these do not exercise gateway or SQL custody."""

from __future__ import annotations

import json
from hashlib import sha256

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowCost, PaidOverflowIdentity, PaidOverflowUsage
from elspeth.web.sessions.continuation_sdk_terminal import (
    SDK_TERMINAL_RECEIPT_MAX_UTF8_BYTES,
    SdkTerminalReceipt,
    decode_sdk_terminal_receipt,
    encode_sdk_terminal_receipt,
    sdk_terminal_receipt_keys,
)


@pytest.fixture
def receipt() -> SdkTerminalReceipt:
    return SdkTerminalReceipt(
        schema_version="composer.sdk-terminal.v1",
        identity=PaidOverflowIdentity(
            session_id="00000000-0000-4000-8000-000000000001",
            operation_id="00000000-0000-4000-8000-000000000002",
            effect_key="provider:0:0",
            attempt_id="00000000-0000-4000-8000-000000000003",
            session_operation_id="00000000-0000-4000-8000-000000000004",
            session_operation_epoch=4,
            ownership_generation=2,
            lease_sha256="a" * 64,
            policy_sha256="b" * 64,
            source_checkpoint_id="00000000-0000-4000-8000-000000000005",
            source_cursor=8,
            source_snapshot_sha256="c" * 64,
        ),
        producer="primary",
        outcome="terminal_sdk_failure",
        observation="no_observed_result",
        sdk_entered=True,
        sdk_result_observed=False,
        retryable=False,
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


@pytest.mark.parametrize("observation", ["provider_failure", "provider_timeout", "no_observed_result"])
def test_observation_round_trip_preserves_unknown_charge_without_success(receipt, observation):
    current = receipt.model_copy(update={"observation": observation})
    encoded = encode_sdk_terminal_receipt(current, expected=receipt.identity)
    restored = decode_sdk_terminal_receipt(encoded, expected=receipt.identity)
    assert restored == current
    assert restored.cost.amount is None and restored.cost.source == "not_available"
    assert restored.usage.prompt_tokens is None and restored.usage.model is None
    assert restored.sdk_entered is True and restored.sdk_result_observed is False and restored.retryable is False
    assert not ({"output", "result_hash", "provider_returned", "error_message"} & set(type(restored).model_fields))
    digest = sha256(encoded.encode("utf-8")).hexdigest()
    assert sdk_terminal_receipt_keys(encoded, expected=receipt.identity) == (
        f"sdk-terminal:{digest}",
        f"value:composer.sdk-terminal.v1:{digest}",
    )


def test_known_charge_and_partial_usage_are_preserved_without_zero_substitution(receipt):
    current = receipt.model_copy(
        update={
            "observation": "provider_failure",
            "cost": PaidOverflowCost(status="known", amount="0.000123", source="response_usage.cost"),
            "usage": PaidOverflowUsage(
                status="observed",
                model="original-observed-model",
                prompt_tokens=7,
                completion_tokens=None,
                cached_prompt_tokens=None,
                reasoning_tokens=None,
                finished_at="2026-11-03T10:00:00+00:00",
            ),
        }
    )
    encoded = encode_sdk_terminal_receipt(current, expected=receipt.identity)
    restored = decode_sdk_terminal_receipt(encoded, expected=receipt.identity)
    assert restored.cost.amount == "0.000123" and restored.cost.source == "response_usage.cost"
    assert restored.usage.model == "original-observed-model" and restored.usage.prompt_tokens == 7
    assert restored.usage.completion_tokens is None


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("observation", "sql_failure"),
        ("observation", "audit_integrity_error"),
        ("observation", "returned_empty_result"),
        ("sdk_entered", False),
        ("sdk_entered", 1),
        ("sdk_result_observed", True),
        ("sdk_result_observed", 0),
        ("retryable", True),
        ("retryable", 0),
        ("producer", "checkpoint_advisor"),
        ("outcome", "terminal_paid_incomplete"),
        ("schema_version", "composer.paid-incomplete.v1"),
    ],
)
def test_unchecked_copy_cannot_change_observation_or_disposition(receipt, field, changed):
    current = receipt.model_copy(update={field: changed})
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        encode_sdk_terminal_receipt(current, expected=receipt.identity)
    fields = receipt.model_dump(mode="json")
    fields[field] = changed
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        decode_sdk_terminal_receipt(json.dumps(fields), expected=receipt.identity)


@pytest.mark.parametrize("extra", ["output", "error_message", "provider_returned", "result_hash", "observed_bytes"])
def test_wire_rejects_output_provider_text_or_invented_evidence(receipt, extra):
    fields = receipt.model_dump(mode="json")
    fields[extra] = "unretained provider data"
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        decode_sdk_terminal_receipt(json.dumps(fields), expected=receipt.identity)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("attempt_id", "00000000-0000-4000-8000-000000000009"),
        ("source_cursor", 9),
        ("lease_sha256", "d" * 64),
        ("ownership_generation", 3),
        ("policy_sha256", "e" * 64),
    ],
)
def test_foreign_original_identity_is_refused_in_both_directions(receipt, field, changed):
    encoded = encode_sdk_terminal_receipt(receipt, expected=receipt.identity)
    foreign = receipt.identity.model_copy(update={field: changed})
    with pytest.raises(AuditIntegrityError, match="original attempt identity"):
        decode_sdk_terminal_receipt(encoded, expected=foreign)
    with pytest.raises(AuditIntegrityError, match="original attempt identity"):
        encode_sdk_terminal_receipt(receipt, expected=foreign)
    with pytest.raises(AuditIntegrityError, match="original attempt identity"):
        sdk_terminal_receipt_keys(encoded, expected=foreign)


def test_wire_budget_boundary_and_canonical_json_controls(receipt):
    encoded = encode_sdk_terminal_receipt(receipt, expected=receipt.identity)
    size = len(encoded.encode("utf-8"))
    assert size < SDK_TERMINAL_RECEIPT_MAX_UTF8_BYTES
    assert encode_sdk_terminal_receipt(receipt, expected=receipt.identity, max_bytes=size) == encoded
    assert decode_sdk_terminal_receipt(encoded, expected=receipt.identity, max_bytes=size) == receipt
    with pytest.raises(AuditIntegrityError, match="byte budget"):
        encode_sdk_terminal_receipt(receipt, expected=receipt.identity, max_bytes=size - 1)
    with pytest.raises(AuditIntegrityError, match="byte budget"):
        decode_sdk_terminal_receipt(encoded, expected=receipt.identity, max_bytes=size - 1)
    with pytest.raises(AuditIntegrityError, match="not canonical"):
        decode_sdk_terminal_receipt(" " + encoded, expected=receipt.identity)
    with pytest.raises(AuditIntegrityError, match="strict JSON"):
        decode_sdk_terminal_receipt('{"retryable":false,' + encoded[1:], expected=receipt.identity)


@pytest.mark.parametrize(
    "changed",
    [
        {"status": "unknown", "amount": "0", "source": "not_available"},
        {"status": "known", "amount": "0", "source": "not_available"},
        {"status": "known", "amount": "1.00", "source": "response_usage.cost"},
    ],
)
def test_unknown_or_noncanonical_charge_cannot_be_laundered_by_decode(receipt, changed):
    fields = receipt.model_dump(mode="json")
    fields["cost"] = changed
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        decode_sdk_terminal_receipt(json.dumps(fields), expected=receipt.identity)


@pytest.mark.parametrize("limit", [True, 0, -1, SDK_TERMINAL_RECEIPT_MAX_UTF8_BYTES + 1])
def test_caller_cannot_disable_fixed_wire_allowance(receipt, limit):
    with pytest.raises(ValueError, match="fixed wire allowance"):
        encode_sdk_terminal_receipt(receipt, expected=receipt.identity, max_bytes=limit)


def test_nested_unchecked_cost_and_identity_copies_are_revalidated(receipt):
    forged_cost = receipt.cost.model_copy(update={"amount": "0"})
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        encode_sdk_terminal_receipt(receipt.model_copy(update={"cost": forged_cost}), expected=receipt.identity)
    forged_identity = receipt.identity.model_copy(update={"source_cursor": True})
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        encode_sdk_terminal_receipt(receipt, expected=forged_identity)
