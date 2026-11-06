"""Retained timing policy preserves admitted bounds across process changes."""

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_policy as policy_module
from elspeth.web.composer.continuation_policy import (
    ContinuationActionPolicy,
    decode_continuation_policy,
)
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from tests.helpers.continuation_policy import continuation_test_settings, minimal_continuation_policy


def test_current_admission_retains_operator_cost_overrides_and_refuses_tampering():
    settings = continuation_test_settings(
        composer_planner_max_provider_calls=3,
        composer_planner_max_request_bytes=4096,
        composer_planner_max_completion_tokens=31,
        composer_planner_max_cumulative_provider_cost=Decimal("1.25"),
    )
    policy = continuation_policy_from_settings(settings)
    assert policy is not None
    assert policy.schema_version == "composer.continuation.policy.v4"
    assert policy.provider_cost.max_provider_calls == 3
    assert policy.provider_cost.max_request_bytes == 4096
    assert policy.provider_cost.max_completion_tokens == 31
    assert policy.provider_cost.max_cumulative_provider_cost == Decimal("1.25")
    assert policy.provider_cost.repair_budget == 2
    assert decode_continuation_policy(policy.model_dump_json(), policy.policy_hash) == policy

    changed = json.loads(policy.model_dump_json())
    changed["provider_cost"]["max_provider_calls"] = 4
    with pytest.raises(AuditIntegrityError, match="hash mismatch"):
        decode_continuation_policy(json.dumps(changed), policy.policy_hash)


def test_complete_current_policy_roundtrips():
    policy = minimal_continuation_policy()
    assert decode_continuation_policy(policy.model_dump_json(), policy.policy_hash) == policy


def test_changed_bound_is_refused_under_original_policy_hash():
    policy = minimal_continuation_policy()
    changed = json.loads(policy.model_dump_json())
    changed["actions"]["primary"]["physical"] = 500.0
    with pytest.raises(AuditIntegrityError, match="hash mismatch"):
        decode_continuation_policy(json.dumps(changed), policy.policy_hash)


@pytest.mark.parametrize("change", ("missing_role", "boolean_stage", "extra_role", "infinite_stage"))
def test_incomplete_or_invalid_owned_policy_is_refused(change):
    policy = minimal_continuation_policy()
    changed = json.loads(policy.model_dump_json())
    if change == "missing_role":
        del changed["actions"]["title"]
    elif change == "boolean_stage":
        changed["actions"]["primary"]["physical"] = True
    elif change == "extra_role":
        changed["actions"]["other"] = changed["actions"]["primary"]
    else:
        changed["actions"]["primary"]["physical"] = float("inf")
    with pytest.raises(AuditIntegrityError, match=r"not strict JSON|complete owned contract"):
        decode_continuation_policy(json.dumps(changed), policy.policy_hash)


@pytest.mark.parametrize("encoded", ('{"segment":', '{"segment":1,"segment":2}', "NaN"))
def test_non_strict_json_is_refused(encoded):
    with pytest.raises(AuditIntegrityError):
        decode_continuation_policy(encoded, minimal_continuation_policy().policy_hash)


def test_complete_policy_retains_all_stage_allowances_and_is_immutable():
    policy = minimal_continuation_policy()
    primary, read = policy.actions.primary, policy.actions.read
    assert primary.total == primary.preparation + primary.guard + primary.quota + primary.physical + primary.retention
    assert read.guard is None and read.quota is None
    assert read.total == read.preparation + read.physical + read.retention
    with pytest.raises(ValidationError):
        policy.actions.primary.physical = 500.0


def test_finite_stages_cannot_overflow_the_total_action_allowance():
    with pytest.raises(ValidationError, match="finite"):
        ContinuationActionPolicy(preparation=1e308, guard=None, quota=None, physical=1e308, retention=1.0)


@pytest.mark.parametrize("reserve,watermark,physical", ((1e308, 1e308, 120.0), (7e307, 7e307, 5e307)))
def test_finite_action_and_segment_bounds_cannot_overflow_segment_sizing(reserve, watermark, physical):
    data = minimal_continuation_policy().model_dump(mode="python")
    data["segment"].update(nominal=1.7e308, watermark=watermark, reserve=reserve)
    data["actions"]["primary"]["physical"] = physical
    with pytest.raises(ValidationError, match="finite"):
        type(minimal_continuation_policy()).model_validate(data)


def test_json_exponent_overflow_has_the_owned_refusal_type():
    policy = minimal_continuation_policy()
    # A complete, correctly versioned payload whose one numeric value overflows.
    encoded = policy.model_dump_json().replace('"reserve":25.0', '"reserve":1e999')
    assert '"reserve":1e999' in encoded
    with pytest.raises(AuditIntegrityError, match="complete owned contract"):
        decode_continuation_policy(encoded, policy.policy_hash)


@pytest.fixture
def policy_decode_calls(monkeypatch):
    policy_module._decode_continuation_policy_cached.cache_clear()
    original = policy_module._decode_continuation_policy_uncached
    calls = []

    def observe(encoded, expected_hash):
        calls.append((encoded, expected_hash))
        return original(encoded, expected_hash)

    monkeypatch.setattr(policy_module, "_decode_continuation_policy_uncached", observe)
    try:
        yield calls
    finally:
        policy_module._decode_continuation_policy_cached.cache_clear()


def test_policy_reuse_requires_both_complete_original_strings(policy_decode_calls):
    policy = minimal_continuation_policy()
    encoded, digest = policy.model_dump_json(), policy.policy_hash
    original = decode_continuation_policy(encoded, digest)
    assert original == policy
    assert decode_continuation_policy(encoded, digest) is original
    assert policy_decode_calls == [(encoded, digest)]

    alternate = " " + encoded
    assert decode_continuation_policy(alternate, digest) == original
    assert policy_decode_calls == [(encoded, digest), (alternate, digest)]
    for wrong_hash in ("0" * 64, digest[:-1]):
        assert wrong_hash != digest
        with pytest.raises(AuditIntegrityError, match=r"^continuation policy hash mismatch$"):
            decode_continuation_policy(encoded, wrong_hash)

    changed = policy.model_copy(update={"max_retained_payload_bytes": policy.max_retained_payload_bytes + 1})
    changed_encoded = changed.model_dump_json()
    with pytest.raises(AuditIntegrityError, match=r"^continuation policy hash mismatch$"):
        decode_continuation_policy(changed_encoded, digest)
    admitted = decode_continuation_policy(changed_encoded, changed.policy_hash)
    assert admitted == changed and admitted != original
    assert decode_continuation_policy(changed_encoded, changed.policy_hash) is admitted
    assert decode_continuation_policy(encoded, digest) is original


@pytest.mark.parametrize(
    "encoded,refusal",
    (
        ('{"segment":', "is not strict JSON"),
        ('{"segment":1,"segment":2}', "is not strict JSON"),
        ("NaN", "is not strict JSON"),
        ("[]", "lost its schema version"),
        ("{}", "lost its schema version"),
        ('{"schema_version":false}', "has an unsupported schema version"),
        ('{"schema_version":"composer.continuation.policy.v1"}', "has an unsupported schema version"),
        ('{"schema_version":"composer.continuation.policy.v2"}', "has an unsupported schema version"),
        ('{"schema_version":"composer.continuation.policy.v3"}', "has an unsupported schema version"),
        ('{"schema_version":"composer.continuation.policy.v9"}', "has an unsupported schema version"),
        ('{"schema_version":"composer.continuation.policy.v4"}', "violates its complete owned contract"),
        ('{"schema_version":"composer.continuation.policy.v4","extra":"é"}', "violates its complete owned contract"),
    ),
)
def test_warm_policy_reuse_keeps_original_refusals_and_does_not_cache_errors(policy_decode_calls, encoded, refusal):
    policy = minimal_continuation_policy()
    original = decode_continuation_policy(policy.model_dump_json(), policy.policy_hash)
    for _ in range(2):
        with pytest.raises(AuditIntegrityError, match=rf"^continuation policy {refusal}$"):
            decode_continuation_policy(encoded, policy.policy_hash)
    assert policy_decode_calls[1:] == [(encoded, policy.policy_hash)] * 2
    assert policy_module._decode_continuation_policy_cached.cache_info().currsize == 1
    assert policy_module._decode_continuation_policy_cached.cache_info().misses == (3 if encoded.isascii() else 1)
    assert decode_continuation_policy(policy.model_dump_json(), policy.policy_hash) is original
    # A bad hash cannot move malformed/version/contract refusal behind hashing.
    with pytest.raises(AuditIntegrityError, match=rf"^continuation policy {refusal}$"):
        decode_continuation_policy(encoded, "wrong hash")


@pytest.mark.parametrize("size,reused", ((8192, True), (8193, False)))
def test_policy_cache_size_limit_preserves_valid_uncached_acceptance(policy_decode_calls, size, reused):
    policy = minimal_continuation_policy()
    encoded = policy.model_dump_json()
    assert len(encoded) < 8192 and encoded.isascii()
    padded = encoded + " " * (size - len(encoded))
    first = decode_continuation_policy(padded, policy.policy_hash)
    second = decode_continuation_policy(padded, policy.policy_hash)
    assert first == second == policy
    assert (first is second) is reused
    assert policy_decode_calls == [(padded, policy.policy_hash)] * (1 if reused else 2)
    assert policy_module._decode_continuation_policy_cached.cache_info().currsize == int(reused)
    oversized_bad = " " * 8193 + "{"
    with pytest.raises(AuditIntegrityError, match=r"^continuation policy is not strict JSON$"):
        decode_continuation_policy(oversized_bad, policy.policy_hash)


@pytest.mark.parametrize("subclass_input", ("encoded", "hash"))
def test_policy_cache_exact_string_gate_preserves_uncached_decode(policy_decode_calls, subclass_input):
    class PolicyText(str):
        pass

    policy = minimal_continuation_policy()
    encoded = PolicyText(policy.model_dump_json()) if subclass_input == "encoded" else policy.model_dump_json()
    digest = PolicyText(policy.policy_hash) if subclass_input == "hash" else policy.policy_hash
    assert decode_continuation_policy(encoded, digest) == policy
    assert decode_continuation_policy(encoded, digest) == policy
    assert policy_decode_calls == [(encoded, digest)] * 2
    assert policy_module._decode_continuation_policy_cached.cache_info().currsize == 0


def test_policy_reuse_keeps_frozen_values_independent_of_supported_dump_and_copy_changes(policy_decode_calls):
    policy = minimal_continuation_policy()
    encoded, digest = policy.model_dump_json(), policy.policy_hash
    shared = decode_continuation_policy(encoded, digest)
    with pytest.raises(ValidationError, match="frozen"):
        shared.actions.read.physical = shared.actions.read.physical + 1
    with pytest.raises(ValidationError, match="frozen"):
        shared.max_retained_payload_bytes = shared.max_retained_payload_bytes + 1
    dumped = shared.model_dump(mode="json")
    dumped["actions"]["read"]["physical"] += 1
    dumped["provider_cost"]["max_provider_calls"] += 1
    copied = shared.model_copy(update={"segment": shared.segment.model_copy(update={"reserve": shared.segment.reserve + 1})})
    assert copied.segment.reserve != shared.segment.reserve
    assert copied.policy_hash != digest
    assert decode_continuation_policy(encoded, digest) is shared
    assert shared.model_dump_json() == encoded and shared.policy_hash == digest
    assert policy_decode_calls == [(encoded, digest)]


def test_policy_reuse_evicts_without_changing_decoded_values(policy_decode_calls):
    base = minimal_continuation_policy()
    policies = [base.model_copy(update={"max_retained_payload_bytes": base.max_retained_payload_bytes + index}) for index in range(17)]
    decoded = [decode_continuation_policy(policy.model_dump_json(), policy.policy_hash) for policy in policies]
    assert decoded == policies and len(policy_decode_calls) == 17
    assert policy_module._decode_continuation_policy_cached.cache_info().currsize == 16
    assert decode_continuation_policy(policies[-1].model_dump_json(), policies[-1].policy_hash) is decoded[-1]
    assert len(policy_decode_calls) == 17
    restored = decode_continuation_policy(policies[0].model_dump_json(), policies[0].policy_hash)
    assert restored == decoded[0] and restored is not decoded[0]
    assert len(policy_decode_calls) == 18
    assert policy_module._decode_continuation_policy_cached.cache_info().currsize == 16
