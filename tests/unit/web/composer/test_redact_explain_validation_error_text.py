"""``explain_validation_error.error_text`` must not persist verbatim.

The tool description tells the planner to pass a validation entry's
``message``, and validator messages echo authored option values (for example
``Invalid schema mode '<value>'``). The manifest used to declare the argument
non-sensitive, so those option bytes reached ``chat_messages.tool_calls``
unredacted while the response side summarised the same message text.

The argument is now sensitive: a string that is exactly a closed validation
``error_code`` is kept (a public catalogue constant); any other string becomes
a fixed sentinel carrying only its length; a non-string becomes a value-free
shape sentinel. The summariser never raises.
"""

from __future__ import annotations

import json

import pytest

from elspeth.web.composer.redaction import MANIFEST, redact_tool_call_arguments
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.tools.generation import _CLOSED_VALIDATION_ERROR_CODES
from tests.unit.web.composer.test_tools import _empty_state, _mock_catalog, execute_tool

_CANARY = "SK-LIVE-CANARY-7731"


def _redact(error_text: object) -> dict[str, object]:
    return redact_tool_call_arguments(
        "explain_validation_error",
        {"error_text": error_text},
        telemetry=NoopRedactionTelemetry(),
    )


def test_validator_message_echoing_an_option_value_is_not_persisted() -> None:
    result = execute_tool(
        "set_source",
        {
            "plugin": "csv",
            "on_success": "t1",
            "options": {"path": "/tmp/a.csv", "schema": {"mode": _CANARY}},
            "on_validation_failure": "quarantine",
        },
        _empty_state(),
        _mock_catalog(),
    ).to_dict()
    messages = [entry["message"] for key in ("errors", "warnings") for entry in result["validation"][key] if _CANARY in entry["message"]]
    # Precondition: the validator really echoes the authored value.
    assert messages, "set_source no longer echoes the schema mode; pick another echoing option"

    redacted = _redact(messages[0])

    assert _CANARY not in json.dumps(redacted)
    assert redacted == {"error_text": f"<redacted-validation-error-text:{len(messages[0])}-chars>"}


def test_free_text_error_text_becomes_a_length_sentinel() -> None:
    text = f"Invalid schema mode '{_CANARY}'"

    assert _redact(text) == {"error_text": f"<redacted-validation-error-text:{len(text)}-chars>"}


@pytest.mark.parametrize("code", _CLOSED_VALIDATION_ERROR_CODES)
def test_exact_closed_error_code_is_kept(code: str) -> None:
    assert _redact(code) == {"error_text": code}


def test_closed_code_embedded_in_free_text_is_not_kept_verbatim() -> None:
    code = _CLOSED_VALIDATION_ERROR_CODES[0]
    text = f"{_CANARY} {code}"

    assert _redact(text) == {"error_text": f"<redacted-validation-error-text:{len(text)}-chars>"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (7, "<redacted-response-integer>"),
        ([_CANARY], "<redacted-response-sequence>"),
        ({"message": _CANARY}, "<redacted-response-mapping>"),
        (None, "<redacted-response-null>"),
        (True, "<redacted-response-boolean>"),
    ],
)
def test_non_string_error_text_never_raises_and_is_value_free(value: object, expected: str) -> None:
    assert _redact(value) == {"error_text": expected}


def test_manifest_declares_error_text_sensitive() -> None:
    policy = MANIFEST["explain_validation_error"].policy
    assert policy is not None
    assert policy.sensitive_argument_keys == ("error_text",)
    assert set(policy.argument_summarizers) == {"error_text"}
    assert policy.known_argument_keys == ("error_text",)
