"""Versioned paid-provider overflow policy and explicit opt-out."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import _CANONICAL_SAFE_INTEGER_MAX, canonical_json, stable_hash
from elspeth.web.composer.continuation_policy import (
    CONTINUATION_POLICY_SCHEMA_VERSION_V4,
    ContinuationAdmissionPolicy,
    ContinuationAdmissionPolicyV2,
    ContinuationAdmissionPolicyV3,
    ContinuationAdmissionPolicyV4,
    decode_continuation_policy,
)
from elspeth.web.composer.continuation_policy_settings import (
    continuation_policy_from_settings,
    continuation_policy_v3_from_settings,
    continuation_policy_v4_from_settings,
)
from tests.helpers.continuation_policy import continuation_test_settings


def test_explicit_null_v4_builder_preserves_current_v2_admission() -> None:
    settings = continuation_test_settings(composer_continuation_max_retained_payload_bytes=None)
    assert continuation_policy_v4_from_settings(settings) is None
    assert type(continuation_policy_from_settings(settings)) is ContinuationAdmissionPolicyV2


def test_default_retained_allowance_builds_v4_without_replacing_v2_admission() -> None:
    settings = continuation_test_settings()
    policy = continuation_policy_v4_from_settings(settings)
    assert type(policy) is ContinuationAdmissionPolicyV4
    assert policy.max_retained_payload_bytes == 16 * 1_048_576
    assert decode_continuation_policy(policy.model_dump_json(), policy.policy_hash) == policy
    assert type(continuation_policy_from_settings(settings)) is ContinuationAdmissionPolicyV2


def test_explicit_v4_policy_round_trips_without_changing_older_policy_contracts() -> None:
    settings = continuation_test_settings(composer_continuation_max_retained_payload_bytes=17)
    v2 = continuation_policy_from_settings(settings)
    v3 = continuation_policy_v3_from_settings(settings)
    v4 = continuation_policy_v4_from_settings(settings)
    assert type(v3) is ContinuationAdmissionPolicyV3
    assert type(v4) is ContinuationAdmissionPolicyV4
    assert v4.schema_version == CONTINUATION_POLICY_SCHEMA_VERSION_V4 == "composer.continuation.policy.v4"
    assert v4.paid_provider_overflow_disposition == "terminal_paid_overflow"
    assert v4.max_retained_payload_bytes == 17
    assert (v4.segment, v4.actions, v4.provider_cost) == (v3.segment, v3.actions, v3.provider_cost)
    assert v4.policy_hash not in (v2.policy_hash, v3.policy_hash)
    assert type(decode_continuation_policy(v4.model_dump_json(), v4.policy_hash)) is ContinuationAdmissionPolicyV4
    assert type(decode_continuation_policy(v3.model_dump_json(), v3.policy_hash)) is ContinuationAdmissionPolicyV3
    assert type(decode_continuation_policy(v2.model_dump_json(), v2.policy_hash)) is ContinuationAdmissionPolicyV2
    legacy = ContinuationAdmissionPolicy(segment=v2.segment, actions=v2.actions)
    assert type(decode_continuation_policy(legacy.model_dump_json(), legacy.policy_hash)) is ContinuationAdmissionPolicy


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("paid_provider_overflow_disposition", None),
        ("paid_provider_overflow_disposition", "retry_paid_call"),
        ("paid_provider_overflow_disposition", False),
        ("max_retained_payload_bytes", 0),
        ("max_retained_payload_bytes", True),
        ("max_retained_payload_bytes", "17"),
        ("max_retained_payload_bytes", 17.5),
    ),
)
def test_v4_decode_refuses_invalid_mandatory_fields_even_with_matching_hash(field: str, value: object) -> None:
    policy = continuation_policy_v4_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=17))
    assert policy is not None
    changed = json.loads(policy.model_dump_json())
    changed[field] = value
    with pytest.raises(AuditIntegrityError, match="complete owned contract"):
        decode_continuation_policy(canonical_json(changed), stable_hash(changed))


def test_v4_model_refuses_allowance_above_canonical_integer_domain() -> None:
    policy = continuation_policy_v4_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=17))
    assert policy is not None
    changed = policy.model_dump(mode="json")
    changed["max_retained_payload_bytes"] = _CANONICAL_SAFE_INTEGER_MAX + 1
    with pytest.raises(ValidationError, match="max_retained_payload_bytes"):
        ContinuationAdmissionPolicyV4.model_validate(changed)


@pytest.mark.parametrize("field", ("paid_provider_overflow_disposition", "max_retained_payload_bytes"))
def test_v4_decode_refuses_missing_required_field_even_with_matching_hash(field: str) -> None:
    policy = continuation_policy_v4_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=17))
    assert policy is not None
    changed = json.loads(policy.model_dump_json())
    del changed[field]
    with pytest.raises(AuditIntegrityError, match="complete owned contract"):
        decode_continuation_policy(canonical_json(changed), stable_hash(changed))


def test_v4_decode_refuses_added_field_and_changed_original_hash() -> None:
    policy = continuation_policy_v4_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=17))
    assert policy is not None
    changed = json.loads(policy.model_dump_json())
    changed["unexpected"] = True
    with pytest.raises(AuditIntegrityError, match="complete owned contract"):
        decode_continuation_policy(canonical_json(changed), stable_hash(changed))

    changed = json.loads(policy.model_dump_json())
    changed["max_retained_payload_bytes"] = 18
    with pytest.raises(AuditIntegrityError, match="hash mismatch"):
        decode_continuation_policy(canonical_json(changed), policy.policy_hash)


def test_v4_decode_refuses_unknown_version_even_with_matching_hash() -> None:
    policy = continuation_policy_v4_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=17))
    assert policy is not None
    changed = json.loads(policy.model_dump_json())
    changed["schema_version"] = "composer.continuation.policy.v5"
    with pytest.raises(AuditIntegrityError, match="unknown schema version"):
        decode_continuation_policy(canonical_json(changed), stable_hash(changed))
