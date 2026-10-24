"""Retained timing policy preserves admitted bounds across process changes."""

import json

import pytest
from pydantic import ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_policy import (
    ContinuationActionPolicy,
    ContinuationAdmissionPolicy,
    decode_continuation_policy,
)
from tests.helpers.continuation_policy import minimal_continuation_policy


def test_complete_policy_roundtrips_for_flag_off_draining():
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
        ContinuationAdmissionPolicy.model_validate(data)


def test_json_exponent_overflow_has_the_owned_refusal_type():
    policy = minimal_continuation_policy()
    # A complete, correctly versioned payload whose one numeric value overflows.
    encoded = policy.model_dump_json().replace('"reserve":25.0', '"reserve":1e999')
    assert '"reserve":1e999' in encoded
    with pytest.raises(AuditIntegrityError, match="complete owned contract"):
        decode_continuation_policy(encoded, policy.policy_hash)
