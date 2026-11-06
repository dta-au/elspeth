"""Versioned paid-provider overflow policy and explicit opt-out."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import _CANONICAL_SAFE_INTEGER_MAX, canonical_json, stable_hash
from elspeth.web.composer.continuation_policy import (
    CONTINUATION_POLICY_SCHEMA_VERSION_V4,
    ContinuationAdmissionPolicyV4,
    decode_continuation_policy,
)
from elspeth.web.composer.continuation_policy_settings import (
    continuation_policy_from_settings,
)
from tests.helpers.continuation_policy import continuation_test_settings


def test_explicit_null_disables_fresh_current_admission() -> None:
    settings = continuation_test_settings(composer_continuation_max_retained_payload_bytes=None)
    assert continuation_policy_from_settings(settings) is None


def test_default_admission_is_current_with_approved_retained_allowance() -> None:
    policy = continuation_policy_from_settings(continuation_test_settings())
    assert type(policy) is ContinuationAdmissionPolicyV4
    assert policy.max_retained_payload_bytes == 16 * 1_048_576
    assert policy.schema_version == CONTINUATION_POLICY_SCHEMA_VERSION_V4
    assert policy.paid_provider_overflow_disposition == "terminal_paid_overflow"
    assert decode_continuation_policy(policy.model_dump_json(), policy.policy_hash) == policy


def test_explicit_current_allowance_changes_only_retained_bound() -> None:
    default = continuation_policy_from_settings(continuation_test_settings())
    changed = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=17))
    assert default is not None and changed is not None
    assert changed.max_retained_payload_bytes == 17
    assert (changed.segment, changed.actions, changed.provider_cost) == (default.segment, default.actions, default.provider_cost)
    assert changed.policy_hash != default.policy_hash
    assert decode_continuation_policy(changed.model_dump_json(), changed.policy_hash) == changed


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
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=17))
    assert policy is not None
    changed = json.loads(policy.model_dump_json())
    changed[field] = value
    with pytest.raises(AuditIntegrityError, match="complete owned contract"):
        decode_continuation_policy(canonical_json(changed), stable_hash(changed))


def test_v4_model_refuses_allowance_above_canonical_integer_domain() -> None:
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=17))
    assert policy is not None
    changed = policy.model_dump(mode="json")
    changed["max_retained_payload_bytes"] = _CANONICAL_SAFE_INTEGER_MAX + 1
    with pytest.raises(ValidationError, match="max_retained_payload_bytes"):
        ContinuationAdmissionPolicyV4.model_validate(changed)


@pytest.mark.parametrize("field", ("paid_provider_overflow_disposition", "max_retained_payload_bytes"))
def test_v4_decode_refuses_missing_required_field_even_with_matching_hash(field: str) -> None:
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=17))
    assert policy is not None
    changed = json.loads(policy.model_dump_json())
    del changed[field]
    with pytest.raises(AuditIntegrityError, match="complete owned contract"):
        decode_continuation_policy(canonical_json(changed), stable_hash(changed))


def test_v4_decode_refuses_added_field_and_changed_original_hash() -> None:
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=17))
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
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=17))
    assert policy is not None
    changed = json.loads(policy.model_dump_json())
    changed["schema_version"] = "composer.continuation.policy.v5"
    with pytest.raises(AuditIntegrityError, match="unsupported schema version"):
        decode_continuation_policy(canonical_json(changed), stable_hash(changed))
