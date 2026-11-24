"""Contract tests for the bounded control-plane credential detector."""

from __future__ import annotations

import base64
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from elspeth.contracts.credential_material import (
    ADMISSION_PATTERN_SPECS,
    CONSERVATIVE_PATTERN_SPECS,
    CREDENTIAL_DETECTOR_VERSION,
    KNOWN_CREDENTIAL_DETECTOR_VERSIONS,
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
    finding = find_credential_material({"ordinary": [{"nested": value}]}, purpose="admission")

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
    assert set(examples) == {name for name, _pattern in CONSERVATIVE_PATTERN_SPECS}
    for name, pattern in CONSERVATIVE_PATTERN_SPECS:
        assert pattern.search(examples[name]) is not None, name
        assert find_credential_material(examples[name], purpose="conservative") is not None, name
    assert {name for name, _pattern in ADMISSION_PATTERN_SPECS} == set(examples) - {"credential_assignment"}
    for name, pattern in ADMISSION_PATTERN_SPECS:
        assert pattern.search(examples[name]) is not None, name
        assert find_credential_material(examples[name], purpose="admission") is not None, name


def test_conservative_patterns_are_the_version_one_set_in_order() -> None:
    """Redaction substitutes each pattern in turn, so names, patterns and order are fixed."""
    assert [(name, pattern.pattern, pattern.flags) for name, pattern in CONSERVATIVE_PATTERN_SPECS] == [
        ("aws_access_key", r"AKIA[0-9A-Z]{16}", 32),
        ("bearer_token", r"Bearer\s+[A-Za-z0-9._-]{20,}", 34),
        ("anthropic_key", r"sk-ant-[A-Za-z0-9_-]{40,}", 32),
        ("openrouter_key", r"sk-or-v1-[A-Za-z0-9_-]{20,}", 32),
        ("openai_key", r"sk-(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{20,}", 32),
        ("github_fine_grained_pat", r"github_pat_[A-Za-z0-9_]{20,}", 32),
        ("github_token", r"gh[opsr]_[A-Za-z0-9]{36,}", 32),
        ("google_api_key", r"AIza[0-9A-Za-z_-]{35}", 32),
        ("slack_token", r"xox[abpr]-[A-Za-z0-9-]{10,}", 32),
        ("slack_app_token", r"xapp-[A-Za-z0-9-]{10,}", 32),
        ("slack_workspace_token", r"xoxe-[A-Za-z0-9-]{10,}", 32),
        ("jwt", r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b", 32),
        ("azure_storage_key", r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{86}==(?=$|[^A-Za-z0-9+/=])", 32),
        ("sas_signature", r"sig=[A-Za-z0-9%/+=]{20,}", 34),
        ("connection_password", r"(?:password|pwd)=[^;\s]+", 34),
        (
            "credential_assignment",
            r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|session[_-]?token|"
            r"auth[_-]?token|id[_-]?token|bearer[_-]?token|client[_-]?secret|"
            r"private[_-]?key|secret[_-]?key|connection[_-]?string|authorization|"
            r"password|passwd|pwd|credentials?)[\"']?\s*[:=]\s*['\"]?"
            r"(?!\$\{[A-Za-z_][A-Za-z0-9_]*\}(?:['\"]?(?:\s|[,;)}\]]|$)))"
            r"[^'\"\s,;(){}\[\]]+",
            34,
        ),
    ]


@pytest.mark.parametrize(
    "prose",
    [
        "The password: field is required for the database sink.",
        "The password: must be rotated every 90 days",
        "credentials: stored in vault",
        "Authorization: Bearer header is added by the gateway",
        "Set password=<value> in the connection string.",
        "use pwd=current directory",
    ],
)
def test_prose_about_credentials_is_admitted_and_still_hidden_conservatively(prose: str) -> None:
    assert find_credential_material(prose, purpose="admission") is None
    assert find_credential_material(prose, purpose="conservative") is not None
    assert scrub_credential_material(prose) == REDACTED_CREDENTIAL_TEXT


@pytest.mark.parametrize(
    "value",
    [
        "Server=db;Uid=u;Password=Hunter2xyz;",
        "Server=db;Uid=u;Pwd=Hunter2xyz;",
        "password=hunter2",
        "pwd=hunter2",
    ],
)
def test_connection_string_password_pairs_are_still_refused(value: str) -> None:
    finding = find_credential_material(value, purpose="admission")

    assert finding is not None
    assert finding.category == "token_shape"


_CORPUS_PIECES = st.sampled_from(
    [
        "password",
        "pwd",
        "api_key",
        "Authorization",
        "Bearer ",
        "=",
        ":",
        ";",
        " ",
        "\n",
        '"',
        "http://",
        "https://",
        "[",
        "@",
        "?api_key=",
        "sk-proj-" + "a" * 24,
        "AKIAABCDEFGHIJKLMNOP",  # secret-scan: allow-this-line
        "hunter2",
        "${TOKEN}",
        "Server=db",
    ]
)


@given(st.lists(_CORPUS_PIECES, max_size=8).map("".join) | st.text(max_size=40))
def test_admission_flags_nothing_the_conservative_set_admits(value: str) -> None:
    if find_credential_material(value, purpose="conservative") is None:
        assert find_credential_material(value, purpose="admission") is None


@pytest.mark.parametrize("key", ["api_key", "API-KEY", "nested_token", "connection_string"])
@pytest.mark.parametrize("value", ["literal", 1, False, ["literal"], {"nested": "literal"}])
def test_non_empty_values_under_credential_fields_are_refused(key: str, value: object) -> None:
    finding = find_credential_material({"options": {key: value}}, purpose="admission")

    assert finding is not None
    assert finding.category == "credential_field"


def test_exact_managed_references_are_allowed_but_literal_fallback_is_not() -> None:
    policy = CredentialTraversalPolicy(env_ref_names={"SERVICE_TOKEN"})

    assert find_credential_material({"api_key": {"secret_ref": "SERVICE_TOKEN"}}, policy, purpose="admission") is None
    assert (
        find_credential_material({"api_key": {"secret_ref": "SERVICE_TOKEN", "secret_scope": "user"}}, policy, purpose="admission") is None
    )
    assert find_credential_material({"api_key": "${SERVICE_TOKEN}"}, policy, purpose="admission") is None
    fallback = "${SERVICE_TOKEN:-sk-" + "a" * 24 + "}"
    finding = find_credential_material({"api_key": fallback}, policy, purpose="admission")
    assert finding is not None
    assert finding.category == "credential_field"


def test_managed_reference_contents_are_bounded_and_scanned() -> None:
    token = "ghp_" + "a" * 36

    token_finding = find_credential_material({"api_key": {"secret_ref": token}}, purpose="admission")
    oversized_finding = find_credential_material(
        {"api_key": {"secret_ref": "a" * 500}}, CredentialTraversalPolicy(max_string_bytes=16), purpose="admission"
    )

    assert token_finding is not None
    assert token_finding.category == "token_shape"
    assert token not in repr(token_finding)
    assert oversized_finding is not None
    assert oversized_finding.category == "traversal_limit"


@pytest.mark.parametrize("key", ["access_token", "accessToken", "access-token", "custom-token"])
def test_separator_normalized_credential_fields_are_refused_and_scrubbed(key: str) -> None:
    value = {key: "low-entropy-secret"}

    finding = find_credential_material(value, purpose="admission")

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
    finding = find_credential_material({"api_key": value}, CredentialTraversalPolicy(env_ref_names={"SERVICE_TOKEN"}), purpose="admission")

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
            },
            purpose="admission",
        )
        is None
    )


def test_mapping_keys_are_scanned_without_returning_the_key() -> None:
    secret_key = "sk-" + "a" * 24
    finding = find_credential_material({secret_key: "ordinary"}, purpose="admission")

    assert finding is not None
    assert finding.location == "mapping_key"
    assert secret_key not in repr(finding)


def test_single_url_decode_does_not_become_recursive_decode() -> None:
    once_encoded = "https://example.test/path?api%5Fkey=value"
    twice_encoded = "https://example.test/path?api%255Fkey=value"

    assert find_credential_material(once_encoded, purpose="admission") is not None
    assert find_credential_material(twice_encoded, purpose="admission") is None


def test_once_decoded_url_values_are_scanned_for_token_shapes() -> None:
    candidate = "sk-proj-" + "a" * 24
    once_encoded = candidate.replace("-", "%2D")

    finding = find_credential_material(f"https://example.test/path?ordinary={once_encoded}", purpose="admission")

    assert finding is not None
    assert finding.category == "sensitive_url_parameter"


def test_malformed_http_url_is_value_free_and_non_raising() -> None:
    malformed = "http://["

    admission = find_credential_material(malformed, purpose="admission")
    conservative = find_credential_material(malformed, purpose="conservative")
    scrubbed = scrub_credential_material(malformed)

    assert admission is None
    assert conservative is not None
    assert conservative.category == "unsupported_value"
    assert malformed not in repr(conservative)
    assert scrubbed == REDACTED_CREDENTIAL_TEXT


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("see http://[ and sk-proj-" + "a" * 24, id="key-after-unparseable-url"),
        pytest.param("sk-proj-" + "a" * 24 + " then http://[", id="key-before-unparseable-url"),
        pytest.param({"first": "http://[", "second": "sk-proj-" + "a" * 24}, id="key-in-a-later-value"),
        pytest.param(["http://[", {"nested": "AKIAABCDEFGHIJKLMNOP"}], id="key-in-a-later-item"),  # secret-scan: allow-this-line
    ],
)
def test_an_unparseable_url_does_not_stop_admission_scanning(value: object) -> None:
    finding = find_credential_material(value, purpose="admission")

    assert finding is not None
    assert finding.category == "token_shape"


def test_an_over_long_secret_reference_is_a_literal_in_a_credential_field() -> None:
    finding = find_credential_material({"api_key": {"secret_ref": "a" * 300}}, purpose="admission")

    assert finding is not None
    assert finding.category == "credential_field"


def test_serialized_json_credential_assignment_is_hidden_but_not_refused() -> None:
    serialized = '{"password":"low-entropy-secret"}'

    finding = find_credential_material(serialized, purpose="conservative")

    assert finding is not None
    assert finding.category == "token_shape"
    assert find_credential_material(serialized, purpose="admission") is None


def test_credential_assignment_cannot_hide_value_across_line_breaks_from_redaction() -> None:
    split_assignment = "RuntimeError: Authorization:\nBearer opaque-token"

    finding = find_credential_material(split_assignment, purpose="conservative")

    assert finding is not None
    assert finding.category == "token_shape"
    assert split_assignment not in repr(finding)
    assert scrub_credential_material(split_assignment) == REDACTED_CREDENTIAL_TEXT


def test_json_escapes_are_scanned_after_the_owner_decodes_them() -> None:
    decoded = json.loads(r'{"ordinary":"sk\u002dproj\u002d' + "a" * 24 + r'"}')

    finding = find_credential_material(decoded, purpose="admission")

    assert finding is not None
    assert finding.category == "token_shape"


def test_unpaired_unicode_surrogates_do_not_escape_the_bounded_detector() -> None:
    assert find_credential_material({"ordinary": "\ud800"}, purpose="admission") is None

    oversized = find_credential_material({"ordinary": "\ud800" * 4}, CredentialTraversalPolicy(max_string_bytes=3), purpose="admission")
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
    assert find_credential_material(value, purpose="admission") is None


def test_traversal_limits_cycles_and_non_json_objects_fail_closed_without_repr() -> None:
    too_deep: object = "leaf"
    for _ in range(4):
        too_deep = [too_deep]
    depth_finding = find_credential_material(too_deep, CredentialTraversalPolicy(max_depth=2), purpose="admission")
    assert depth_finding is not None
    assert depth_finding.category == "traversal_limit"

    cyclic: list[object] = []
    cyclic.append(cyclic)
    cycle_finding = find_credential_material(cyclic, purpose="admission")
    assert cycle_finding is not None
    assert cycle_finding.category == "unsupported_value"

    class ReprExplodes:
        def __repr__(self) -> str:
            raise AssertionError("detector must not render foreign values")

    object_finding = find_credential_material(ReprExplodes(), purpose="admission")
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
    finding = find_credential_material(value, policy, purpose="admission")

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


def test_findings_carry_detector_version_two_and_one_stays_known() -> None:
    finding = find_credential_material("sk-proj-" + "a" * 24, purpose="admission")

    assert CREDENTIAL_DETECTOR_VERSION == "2"
    assert finding is not None
    assert finding.detector_version == "2"
    assert frozenset({"1", "2"}) == KNOWN_CREDENTIAL_DETECTOR_VERSIONS
