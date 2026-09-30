"""Contract tests for the bounded control-plane credential detector."""

from __future__ import annotations

import base64
import json

import pytest

from elspeth.contracts.credential_material import (
    CREDENTIAL_PATTERN_SPECS,
    REDACTED_CREDENTIAL_TEXT,
    CredentialTraversalPolicy,
    find_credential_material,
    scrub_credential_material,
)


@pytest.mark.parametrize(
    ("value", "category"),
    [
        ("AKIAABCDEFGHIJKLMNOP", "token_shape"),  # secret-scan: allow-this-line
        ("Bearer abcdefghijklmnopqrstuvwxyz1234", "token_shape"),
        ("sk-ant-" + "a" * 40, "token_shape"),
        ("sk-or-v1-" + "a" * 24, "token_shape"),
        ("sk-proj-" + "a" * 24, "token_shape"),
        ("github_pat_" + "a" * 24, "token_shape"),
        ("-----BEGIN PRIVATE KEY-----", "private_key"),
        ("https://user:pass@example.test/path", "credential_url"),
        ("https://example.test/path?api%5Fkey=value", "sensitive_url_parameter"),
        ("mongodb://user:pass@example.test/db", "credential_url"),  # secret-scan: allow-this-line
    ],
)
def test_recognized_string_forms_return_value_free_categories(value: str, category: str) -> None:
    finding = find_credential_material({"ordinary": [{"nested": value}]})

    assert finding is not None
    assert finding.category == category
    assert value not in repr(finding)


def test_every_registered_token_pattern_has_a_positive_control() -> None:
    examples = {
        "aws_access_key": "AKIAABCDEFGHIJKLMNOP",  # secret-scan: allow-this-line
        "bearer_token": "Bearer abcdefghijklmnopqrstuvwxyz1234",
        "anthropic_key": "sk-ant-" + "a" * 40,
        "openrouter_key": "sk-or-v1-" + "a" * 24,
        "openai_key": "sk-" + "a" * 24,
        "github_fine_grained_pat": "github_pat_" + "a" * 24,
        "github_token": "ghp_" + "a" * 36,
        "google_api_key": "AIza" + "a" * 35,
        "slack_token": "xoxb-1234567890abcdef",
        "slack_app_token": "xapp-1234567890abcdef",
        "slack_workspace_token": "xoxe-1234567890abcdef",
        "jwt": "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20,
        "azure_storage_key": "a" * 86 + "==",
        "sas_signature": "sig=" + "a" * 24,
        "connection_password": "PWD=hunter2",
        "credential_assignment": "client_secret=hunter2",
    }
    assert set(examples) == {name for name, _pattern in CREDENTIAL_PATTERN_SPECS}
    for name, pattern in CREDENTIAL_PATTERN_SPECS:
        assert pattern.search(examples[name]) is not None, name
        assert find_credential_material(examples[name]) is not None, name


@pytest.mark.parametrize("key", ["api_key", "API-KEY", "nested_token", "connection_string"])
@pytest.mark.parametrize("value", ["literal", 1, False, ["literal"], {"nested": "literal"}])
def test_non_empty_values_under_credential_fields_are_refused(key: str, value: object) -> None:
    finding = find_credential_material({"options": {key: value}})

    assert finding is not None
    assert finding.category == "credential_field"


def test_exact_managed_references_are_allowed_but_literal_fallback_is_not() -> None:
    policy = CredentialTraversalPolicy(env_ref_names={"SERVICE_TOKEN"})

    assert find_credential_material({"api_key": {"secret_ref": "SERVICE_TOKEN"}}, policy) is None
    assert find_credential_material({"api_key": {"secret_ref": "SERVICE_TOKEN", "secret_scope": "user"}}, policy) is None
    assert find_credential_material({"api_key": "${SERVICE_TOKEN}"}, policy) is None
    fallback = "${SERVICE_TOKEN:-sk-" + "a" * 24 + "}"
    finding = find_credential_material({"api_key": fallback}, policy)
    assert finding is not None
    assert finding.category == "credential_field"


def test_managed_reference_contents_are_bounded_and_scanned() -> None:
    token = "ghp_" + "a" * 36

    token_finding = find_credential_material({"api_key": {"secret_ref": token}})
    oversized_finding = find_credential_material(
        {"api_key": {"secret_ref": "a" * 500}},
        CredentialTraversalPolicy(max_string_bytes=16),
    )

    assert token_finding is not None
    assert token_finding.category == "token_shape"
    assert token not in repr(token_finding)
    assert oversized_finding is not None
    assert oversized_finding.category == "traversal_limit"


@pytest.mark.parametrize("key", ["access_token", "accessToken", "access-token", "custom-token"])
def test_separator_normalized_credential_fields_are_refused_and_scrubbed(key: str) -> None:
    value = {key: "low-entropy-secret"}

    finding = find_credential_material(value)

    assert finding is not None
    assert finding.category == "credential_field"
    assert scrub_credential_material(value) == {key: REDACTED_CREDENTIAL_TEXT}


@pytest.mark.parametrize(
    "value",
    [
        {"secret_ref": ""},
        {"secret_ref": "SERVICE_TOKEN", "extra": "value"},
        {"secret_ref": "SERVICE_TOKEN", "secret_scope": "unknown"},
        "${UNDECLARED_TOKEN}",
        "prefix-${SERVICE_TOKEN}",
        base64.b64encode(b"credential bytes").decode(),
    ],
)
def test_inexact_or_unapproved_values_under_credential_fields_are_refused(value: object) -> None:
    finding = find_credential_material(
        {"api_key": value},
        CredentialTraversalPolicy(env_ref_names={"SERVICE_TOKEN"}),
    )

    assert finding is not None
    assert finding.category == "credential_field"


def test_structural_key_exemptions_remain_ordinary_values() -> None:
    assert (
        find_credential_material(
            {
                "data_key": "results",
                "data-key": "results",
                "dataKey": "results",
                "alternate_key": "customer_code",
                "alternate-key": "customer_code",
                "alternateKey": "customer_code",
            }
        )
        is None
    )


def test_mapping_keys_are_scanned_without_returning_the_key() -> None:
    secret_key = "sk-" + "a" * 24
    finding = find_credential_material({secret_key: "ordinary"})

    assert finding is not None
    assert finding.location == "mapping_key"
    assert secret_key not in repr(finding)


def test_single_url_decode_does_not_become_recursive_decode() -> None:
    once_encoded = "https://example.test/path?api%5Fkey=value"
    twice_encoded = "https://example.test/path?api%255Fkey=value"

    assert find_credential_material(once_encoded) is not None
    assert find_credential_material(twice_encoded) is None


def test_once_decoded_url_values_are_scanned_for_token_shapes() -> None:
    candidate = "sk-proj-" + "a" * 24
    once_encoded = candidate.replace("-", "%2D")

    finding = find_credential_material(f"https://example.test/path?ordinary={once_encoded}")

    assert finding is not None
    assert finding.category == "sensitive_url_parameter"


def test_malformed_http_url_is_value_free_and_non_raising() -> None:
    malformed = "http://["

    finding = find_credential_material(malformed)
    scrubbed = scrub_credential_material(malformed)

    assert finding is not None
    assert finding.category == "unsupported_value"
    assert malformed not in repr(finding)
    assert scrubbed == REDACTED_CREDENTIAL_TEXT


def test_serialized_json_credential_assignment_is_recognized_as_text() -> None:
    serialized = '{"password":"low-entropy-secret"}'

    finding = find_credential_material(serialized)

    assert finding is not None
    assert finding.category == "token_shape"


def test_credential_assignment_cannot_hide_value_across_line_breaks() -> None:
    split_assignment = "RuntimeError: Authorization:\nBearer opaque-token"

    finding = find_credential_material(split_assignment)

    assert finding is not None
    assert finding.category == "token_shape"
    assert split_assignment not in repr(finding)


def test_json_escapes_are_scanned_after_the_owner_decodes_them() -> None:
    decoded = json.loads(r'{"ordinary":"sk\u002dproj\u002d' + "a" * 24 + r'"}')

    finding = find_credential_material(decoded)

    assert finding is not None
    assert finding.category == "token_shape"


def test_unpaired_unicode_surrogates_do_not_escape_the_bounded_detector() -> None:
    assert find_credential_material({"ordinary": "\ud800"}) is None

    oversized = find_credential_material(
        {"ordinary": "\ud800" * 4},
        CredentialTraversalPolicy(max_string_bytes=3),
    )
    assert oversized is not None
    assert oversized.category == "traversal_limit"


@pytest.mark.parametrize(
    "value",
    [
        "Use https://example.test/docs?topic=authentication",
        "This prose mentions token count and password length.",
        "person@example.test",
        "123-45-6789",
        "4111-1111-1111-1111",
        base64.b64encode(b"ordinary opaque content").decode(),
        "736b2d6f70617175652d6279746573",
        {"left": "sk-", "right": "a" * 24},
    ],
)
def test_out_of_scope_and_false_positive_controls_are_allowed(value: object) -> None:
    assert find_credential_material(value) is None


def test_traversal_limits_cycles_and_non_json_objects_fail_closed_without_repr() -> None:
    too_deep: object = "leaf"
    for _ in range(4):
        too_deep = [too_deep]
    depth_finding = find_credential_material(too_deep, CredentialTraversalPolicy(max_depth=2))
    assert depth_finding is not None
    assert depth_finding.category == "traversal_limit"

    cyclic: list[object] = []
    cyclic.append(cyclic)
    cycle_finding = find_credential_material(cyclic)
    assert cycle_finding is not None
    assert cycle_finding.category == "unsupported_value"

    class ReprExplodes:
        def __repr__(self) -> str:
            raise AssertionError("detector must not render foreign values")

    object_finding = find_credential_material(ReprExplodes())
    assert object_finding is not None
    assert object_finding.category == "unsupported_value"


@pytest.mark.parametrize(
    ("value", "policy"),
    [
        ({str(index): index for index in range(3)}, CredentialTraversalPolicy(max_width=2)),
        ({"a": [1, 2]}, CredentialTraversalPolicy(max_nodes=2)),
        ({"ordinary": "abcd"}, CredentialTraversalPolicy(max_string_bytes=3)),
    ],
)
def test_each_resource_budget_fails_closed(value: object, policy: CredentialTraversalPolicy) -> None:
    finding = find_credential_material(value, policy)

    assert finding is not None
    assert finding.category == "traversal_limit"


def test_audit_scrub_uses_fixed_whole_value_sentinel_and_handles_cycles() -> None:
    secret = "sk-" + "a" * 24
    cyclic: list[object] = [secret]
    cyclic.append(cyclic)

    scrubbed = scrub_credential_material({"ordinary": secret, "password": "short", "cycle": cyclic})

    assert scrubbed["ordinary"] == REDACTED_CREDENTIAL_TEXT
    assert scrubbed["password"] == REDACTED_CREDENTIAL_TEXT
    assert scrubbed["cycle"] == [REDACTED_CREDENTIAL_TEXT, REDACTED_CREDENTIAL_TEXT]
    assert secret not in repr(scrubbed)
