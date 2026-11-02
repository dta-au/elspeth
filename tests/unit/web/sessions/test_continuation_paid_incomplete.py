"""Pure codec controls; a receipt is not proof that the provider returned."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.sessions.continuation_paid_incomplete import (
    PaidIncompleteReceipt,
    decode_paid_incomplete_receipt,
    encode_paid_incomplete_receipt,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowCost, PaidOverflowIdentity, PaidOverflowUsage


@pytest.fixture
def receipt() -> PaidIncompleteReceipt:
    identity = PaidOverflowIdentity(
        session_id="00000000-0000-4000-8000-000000000001",
        operation_id="00000000-0000-4000-8000-000000000002",
        effect_key="provider:3:0",
        attempt_id="00000000-0000-4000-8000-000000000003",
        session_operation_id="00000000-0000-4000-8000-000000000004",
        session_operation_epoch=4,
        ownership_generation=2,
        lease_sha256="a" * 64,
        policy_sha256="b" * 64,
        source_checkpoint_id="00000000-0000-4000-8000-000000000005",
        source_cursor=8,
        source_snapshot_sha256="c" * 64,
    )
    return PaidIncompleteReceipt(
        schema_version="composer.paid-incomplete.v1",
        identity=identity,
        producer="title",
        outcome="terminal_paid_incomplete",
        evidence_status="incomplete",
        retryable=False,
        provider_returned=True,
        reason="field_unavailable",
        capture_surface="provider_metadata",
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


def test_returned_incomplete_round_trip_preserves_original_identity_unknown_facts_and_no_output(receipt):
    encoded = encode_paid_incomplete_receipt(receipt, expected=receipt.identity)
    restored = decode_paid_incomplete_receipt(encoded, expected=receipt.identity)
    assert restored == receipt
    assert restored.provider_returned is True and restored.retryable is False
    assert restored.cost.status == "unknown" and restored.usage.status == "unavailable"
    assert not any(key in type(restored).model_fields for key in ("measurement", "observed_bytes", "limit_bytes", "result_hash", "output"))
    assert len(encoded.encode("utf-8")) <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES


def test_known_cost_and_observed_usage_are_explicit_and_bounded(receipt):
    known = receipt.model_copy(
        update={
            "cost": PaidOverflowCost(status="known", amount="0.000123", source="response_usage.cost"),
            "usage": PaidOverflowUsage(
                status="observed",
                model="actual-model",
                prompt_tokens=5,
                completion_tokens=3,
                cached_prompt_tokens=None,
                reasoning_tokens=None,
                finished_at="2026-11-02T10:00:00+00:00",
            ),
        }
    )
    encoded = encode_paid_incomplete_receipt(known, expected=receipt.identity)
    restored = decode_paid_incomplete_receipt(encoded, expected=receipt.identity)
    assert restored.cost.amount == "0.000123" and restored.usage.model == "actual-model"
    assert restored.usage.prompt_tokens == 5 and restored.usage.completion_tokens == 3


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("provider_returned", False),
        ("provider_returned", 1),
        ("retryable", True),
        ("retryable", 0),
        ("reason", "arbitrary_error"),
        ("capture_surface", "provider_secret_header"),
        ("producer", "unreviewed_family"),
        ("outcome", "terminal_paid_overflow"),
    ],
)
def test_closed_return_and_disposition_reject_unchecked_copies(receipt, field, changed):
    unchecked = receipt.model_copy(update={field: changed})
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        encode_paid_incomplete_receipt(unchecked, expected=receipt.identity)


@pytest.mark.parametrize("extra", ["measurement", "observed_bytes", "limit_bytes", "result_hash", "output"])
def test_non_byte_receipt_refuses_fabricated_size_or_missing_output(extra, receipt):
    fields = receipt.model_dump(mode="json")
    fields[extra] = "provider output or invented bytes"
    with pytest.raises(ValidationError, match=extra):
        PaidIncompleteReceipt.model_validate(fields)
    encoded = json.dumps(fields, separators=(",", ":"))
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        decode_paid_incomplete_receipt(encoded, expected=receipt.identity)


def test_foreign_attempt_and_noncanonical_or_oversized_wire_fail_closed(receipt):
    encoded = encode_paid_incomplete_receipt(receipt, expected=receipt.identity)
    foreign = receipt.identity.model_copy(update={"attempt_id": "00000000-0000-4000-8000-000000000009"})
    with pytest.raises(AuditIntegrityError, match="original attempt identity"):
        decode_paid_incomplete_receipt(encoded, expected=foreign)
    with pytest.raises(AuditIntegrityError, match="original attempt identity"):
        encode_paid_incomplete_receipt(receipt, expected=foreign)
    with pytest.raises(AuditIntegrityError, match="not canonical"):
        decode_paid_incomplete_receipt(" " + encoded, expected=receipt.identity)
    with pytest.raises(AuditIntegrityError, match="byte budget"):
        decode_paid_incomplete_receipt(encoded, expected=receipt.identity, max_bytes=len(encoded.encode("utf-8")) - 1)


def test_new_writer_rejects_noncanonical_known_amount_and_legacy_overflow_schema(receipt):
    noncanonical = receipt.model_copy(update={"cost": PaidOverflowCost(status="known", amount="1.00", source="response_usage.cost")})
    with pytest.raises(AuditIntegrityError, match="canonical amount"):
        encode_paid_incomplete_receipt(noncanonical, expected=receipt.identity)
    overflow = receipt.model_dump(mode="json")
    overflow["schema_version"] = "composer.paid-overflow.v1"
    with pytest.raises(AuditIntegrityError, match="owned contract"):
        decode_paid_incomplete_receipt(json.dumps(overflow), expected=receipt.identity)
