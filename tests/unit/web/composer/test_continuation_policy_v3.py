"""Current retained bounds and explicit refusal of preserved old policy bytes."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import _CANONICAL_SAFE_INTEGER_MAX, canonical_json, stable_hash
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.config import settings_from_env
from tests.helpers.continuation_policy import continuation_test_settings


@pytest.mark.parametrize("version", ("v1", "v2", "v3"))
def test_old_policy_bytes_refuse_even_with_their_matching_hash(version: str) -> None:
    policy = continuation_policy_from_settings(continuation_test_settings())
    assert policy is not None
    value = policy.model_dump(mode="json")
    value["schema_version"] = "composer.continuation.policy." + version
    del value["paid_provider_overflow_disposition"]
    if version in ("v1", "v2"):
        del value["max_retained_payload_bytes"]
    if version == "v1":
        del value["provider_cost"]
    encoded = canonical_json(value)
    with pytest.raises(AuditIntegrityError, match="unsupported schema version"):
        decode_continuation_policy(encoded, stable_hash(value))
    assert canonical_json(value) == encoded


@pytest.mark.parametrize("value", (False, True, 0, -1, 1.5, "17", _CANONICAL_SAFE_INTEGER_MAX + 1))
def test_direct_settings_refuse_nonexact_or_unrepresentable_allowance(value: object) -> None:
    with pytest.raises(ValidationError):
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=value)


def test_canonical_safe_integer_boundary_is_representable() -> None:
    settings = continuation_test_settings(composer_continuation_max_retained_payload_bytes=_CANONICAL_SAFE_INTEGER_MAX)
    policy = continuation_policy_from_settings(settings)
    assert policy is not None and policy.max_retained_payload_bytes == _CANONICAL_SAFE_INTEGER_MAX
    assert decode_continuation_policy(policy.model_dump_json(), policy.policy_hash) == policy


@pytest.mark.parametrize("change", ("missing", "extra", "wrong_type", "zero", "float"))
def test_current_decode_refuses_changed_complete_shape_even_with_matching_hash(change: str) -> None:
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=17))
    assert policy is not None
    value = json.loads(policy.model_dump_json())
    if change == "missing":
        del value["max_retained_payload_bytes"]
    elif change == "extra":
        value["unexpected"] = 1
    elif change == "wrong_type":
        value["max_retained_payload_bytes"] = True
    elif change == "zero":
        value["max_retained_payload_bytes"] = 0
    else:
        value["max_retained_payload_bytes"] = 1.5
    with pytest.raises(AuditIntegrityError, match="complete owned contract"):
        decode_continuation_policy(canonical_json(value), stable_hash(value))


def test_current_decode_refuses_changed_value_against_original_hash() -> None:
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=17))
    assert policy is not None
    changed = policy.model_dump(mode="json")
    changed["max_retained_payload_bytes"] = 18
    with pytest.raises(AuditIntegrityError, match="hash mismatch"):
        decode_continuation_policy(canonical_json(changed), policy.policy_hash)


def test_environment_accepts_explicit_integer_and_null_is_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS": "15",
        "ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS": "10",
        "ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS": "85",
        "ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE": "10",
        "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY": "0" * 64,
        "ELSPETH_WEB__COMPOSER_CONTINUATION_MAX_RETAINED_PAYLOAD_BYTES": "17",
    }.items():
        monkeypatch.setenv(key, value)
    settings = settings_from_env()
    assert settings.composer_continuation_max_retained_payload_bytes == 17
    assert continuation_policy_from_settings(settings) is not None
    monkeypatch.setenv("ELSPETH_WEB__COMPOSER_CONTINUATION_MAX_RETAINED_PAYLOAD_BYTES", "null")
    assert continuation_policy_from_settings(settings_from_env()) is None
