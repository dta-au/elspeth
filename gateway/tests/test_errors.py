import pytest
from elspeth_llm_gateway.core.errors import (
    HTTP_STATUS,
    RETRYABLE,
    SAFE_MESSAGE,
    GatewayError,
    GatewayErrorCode,
    error_envelope,
)
from elspeth_llm_gateway.sdk.protocol import CLASSIFIABLE_CODES

ALL_CODES = {
    "invalid_request",
    "inbound_authentication_failed",
    "contract_mismatch",
    "model_not_allowed",
    "capability_unsupported",
    "context_length_exceeded",
    "content_policy_rejected",
    "oauth_token_unavailable",
    "upstream_unauthorized",
    "upstream_rate_limited",
    "upstream_timeout",
    "upstream_unavailable",
    "upstream_response_invalid",
    "internal_error",
}

RETRYABLE_CODES = {
    "oauth_token_unavailable",
    "upstream_rate_limited",
    "upstream_timeout",
    "upstream_unavailable",
}

EXPECTED_STATUS = {
    "invalid_request": 400,
    "inbound_authentication_failed": 401,
    "contract_mismatch": 400,
    "model_not_allowed": 404,
    "capability_unsupported": 422,
    "context_length_exceeded": 400,
    "content_policy_rejected": 400,
    "oauth_token_unavailable": 503,
    "upstream_unauthorized": 502,
    "upstream_rate_limited": 429,
    "upstream_timeout": 504,
    "upstream_unavailable": 503,
    "upstream_response_invalid": 502,
    "internal_error": 500,
}


# --- GatewayErrorCode: exactly 14 codes, no extras --------------------------


def test_exactly_fourteen_codes_no_extras():
    assert {member.value for member in GatewayErrorCode} == ALL_CODES


def test_code_count_is_fourteen():
    assert len(GatewayErrorCode) == 14


# --- HTTP_STATUS -------------------------------------------------------------


def test_http_status_has_entry_for_every_code():
    assert set(HTTP_STATUS.keys()) == set(GatewayErrorCode)


@pytest.mark.parametrize("code_value,expected_status", sorted(EXPECTED_STATUS.items()))
def test_http_status_matches_design_table(code_value, expected_status):
    code = GatewayErrorCode(code_value)
    assert HTTP_STATUS[code] == expected_status


# --- SAFE_MESSAGE -------------------------------------------------------------


def test_safe_message_has_entry_for_every_code():
    assert set(SAFE_MESSAGE.keys()) == set(GatewayErrorCode)


def test_safe_message_values_are_nonempty_strings():
    for code in GatewayErrorCode:
        message = SAFE_MESSAGE[code]
        assert isinstance(message, str)
        assert message.strip() != ""


def test_safe_message_has_no_formatting_placeholders():
    for code in GatewayErrorCode:
        message = SAFE_MESSAGE[code]
        assert "{" not in message
        assert "}" not in message
        assert "%s" not in message
        assert "%d" not in message


def test_safe_message_does_not_name_internal_modules_or_classes():
    forbidden_substrings = [
        "elspeth_llm_gateway",
        "GatewayError",
        "Traceback",
        "Exception",
        ".py",
        "core.",
        "sdk.",
        "adapter",
    ]
    for code in GatewayErrorCode:
        message = SAFE_MESSAGE[code]
        for forbidden in forbidden_substrings:
            assert forbidden not in message


def test_safe_messages_are_all_distinct():
    messages = [SAFE_MESSAGE[code] for code in GatewayErrorCode]
    assert len(messages) == len(set(messages))


# --- RETRYABLE -----------------------------------------------------------------


def test_retryable_has_entry_for_every_code():
    assert set(RETRYABLE.keys()) == set(GatewayErrorCode)


def test_retryable_true_only_for_designated_codes():
    true_codes = {code.value for code, retryable in RETRYABLE.items() if retryable is True}
    assert true_codes == RETRYABLE_CODES


def test_retryable_values_are_bool():
    for code in GatewayErrorCode:
        assert isinstance(RETRYABLE[code], bool)


# --- GatewayError --------------------------------------------------------------


@pytest.mark.parametrize("code", list(GatewayErrorCode))
def test_gateway_error_derives_status_retryable_and_message(code):
    error = GatewayError(code)
    assert error.code == code
    assert error.status == HTTP_STATUS[code]
    assert error.retryable == RETRYABLE[code]
    assert error.safe_message == SAFE_MESSAGE[code]


def test_gateway_error_is_an_exception():
    error = GatewayError(GatewayErrorCode.INTERNAL_ERROR)
    assert isinstance(error, Exception)


def test_gateway_error_accepts_no_free_text_message():
    with pytest.raises(TypeError):
        GatewayError(GatewayErrorCode.INTERNAL_ERROR, "some free text")


def test_gateway_error_str_contains_only_code_and_safe_message():
    error = GatewayError(GatewayErrorCode.UPSTREAM_TIMEOUT)
    rendered = str(error)
    assert GatewayErrorCode.UPSTREAM_TIMEOUT.value in rendered
    assert SAFE_MESSAGE[GatewayErrorCode.UPSTREAM_TIMEOUT] in rendered
    # nothing else should have leaked in: the rendered string should be
    # fully accounted for by the code and the safe message.
    remainder = rendered.replace(GatewayErrorCode.UPSTREAM_TIMEOUT.value, "").replace(SAFE_MESSAGE[GatewayErrorCode.UPSTREAM_TIMEOUT], "")
    assert remainder.strip(" :-") == ""


# --- error_envelope --------------------------------------------------------------


def test_error_envelope_shape_matches_design_json():
    error = GatewayError(GatewayErrorCode.UPSTREAM_UNAVAILABLE)
    envelope = error_envelope(error, request_id="req-123")
    assert envelope == {
        "error": {
            "message": SAFE_MESSAGE[GatewayErrorCode.UPSTREAM_UNAVAILABLE],
            "type": "gateway_error",
            "code": "upstream_unavailable",
            "retryable": True,
            "request_id": "req-123",
        }
    }


@pytest.mark.parametrize("code", list(GatewayErrorCode))
def test_error_envelope_matches_shape_for_every_code(code):
    error = GatewayError(code)
    envelope = error_envelope(error, request_id="req-abc")
    assert set(envelope.keys()) == {"error"}
    inner = envelope["error"]
    assert set(inner.keys()) == {"message", "type", "code", "retryable", "request_id"}
    assert inner["message"] == SAFE_MESSAGE[code]
    assert inner["type"] == "gateway_error"
    assert inner["code"] == code.value
    assert type(inner["code"]) is str
    assert inner["retryable"] == RETRYABLE[code]
    assert inner["request_id"] == "req-abc"


# --- SDK seam: CLASSIFIABLE_CODES containment -------------------------------


def test_classifiable_codes_is_subset_of_gateway_error_codes():
    assert {code.value for code in GatewayErrorCode} >= CLASSIFIABLE_CODES
