"""A malformed ``expected_content_hash`` gets a repair line saying what to send.

The field is ``["string", "null"]`` with a 64-hex pattern, so the string
``"null"`` fails the pattern and the model saw only "Validation failed". The
compose loop's argument-error payload now says to send JSON null (not the
string) or the hash, and, for a paged read, which hash continues it. The value
is never coerced: a wrong argument is still refused.
"""

from __future__ import annotations

import json

import pytest

from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.tool_error_payloads import arg_error_payload
from elspeth.web.composer.tools._dispatch import require_schema_valid_arguments

_BLOB_ID = "2e9e41eb-e34d-4918-b334-3c1e9ee0f8ff"


def _payload(tool: str, arguments: dict[str, object]) -> dict[str, object]:
    with pytest.raises(ToolArgumentError) as caught:
        require_schema_valid_arguments(tool, arguments)
    return dict(arg_error_payload(caught.value, tool))


@pytest.mark.parametrize("bad", ["null", "None", "ABC", "0" * 63])
def test_paged_read_with_a_malformed_hash_is_told_what_to_send(bad: str) -> None:
    payload = _payload("get_blob_content", {"blob_id": _BLOB_ID, "offset_chars": 0, "expected_content_hash": bad})

    assert payload["validation_errors"] == [{"loc": ["expected_content_hash"], "msg": "Validation failed", "type": "invalid"}]
    instruction = payload.get("repair_instruction")
    assert isinstance(instruction, str), payload
    assert 'JSON null (not the string "null")' in instruction
    assert "64-character lowercase hex" in instruction
    assert "send the `content_hash` the previous page returned" in instruction
    assert bad not in instruction or bad == "null"  # the rejected value is not echoed back


def test_whole_attachment_read_with_a_malformed_hash_is_told_what_to_send() -> None:
    payload = _payload("read_attachment", {"blob_id": _BLOB_ID, "expected_content_hash": "null"})

    instruction = payload.get("repair_instruction")
    assert isinstance(instruction, str), payload
    assert 'JSON null (not the string "null")' in instruction
    assert "previous page" not in instruction  # this tool has no pages


def test_other_argument_errors_on_the_same_tool_get_no_hash_line() -> None:
    """Control: a negative offset is refused without the hash repair line."""
    payload = _payload("get_blob_content", {"blob_id": _BLOB_ID, "offset_chars": -1})

    assert "repair_instruction" not in payload or "expected_content_hash" not in json.dumps(payload["repair_instruction"])


def test_json_null_and_a_real_hash_still_pass_the_schema() -> None:
    """Control: the accepted forms are not refused, so nothing is coerced or loosened."""
    require_schema_valid_arguments("get_blob_content", {"blob_id": _BLOB_ID, "offset_chars": 0, "expected_content_hash": None})
    require_schema_valid_arguments("get_blob_content", {"blob_id": _BLOB_ID, "offset_chars": 10, "expected_content_hash": "a" * 64})
