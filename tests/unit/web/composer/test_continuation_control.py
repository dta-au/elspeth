"""The checkpoint_and_continue control (U-40): schema, sole-call rule, and refusal codes.

Design §2.2. Each test names its mutation control.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolBatch, _AdmittedToolCall, _AdmittedToolFunction
from elspeth.web.composer.continuation_controls import (
    CONTINUATION_CALL_NOT_ALONE,
    CONTINUATION_CONTROL_RESULT,
    CONTINUATION_CONTROL_TOOL,
    ContinuationControlRefused,
    continuation_control_tool_definitions,
    parse_continuation_control_batch,
)
from elspeth.web.composer.error_codes import _EMITTED_VALIDATION_ERROR_CODES
from elspeth.web.composer.tool_batch import _admit_tool_batch


def _batch(*calls: tuple[str, str, object]) -> _AdmittedToolBatch:
    admitted = tuple(_AdmittedToolCall(call_id, _AdmittedToolFunction(name, json.dumps(arguments))) for call_id, name, arguments in calls)
    return _AdmittedToolBatch(admitted, frozenset(call.id for call in admitted))


def _provider_control_batch(call_id: str) -> _AdmittedToolBatch:
    from litellm.types.utils import ChatCompletionMessageToolCall, Function

    call = ChatCompletionMessageToolCall(
        id=call_id,
        type="function",
        function=Function(name=CONTINUATION_CONTROL_TOOL, arguments="{}"),
    )
    return _admit_tool_batch((call,))


def test_a_batch_without_the_control_is_not_a_control() -> None:
    assert parse_continuation_control_batch(_batch(("call-1", "set_source", {"plugin": "csv"}))) is None


def test_the_sole_control_call_is_admitted_with_its_notes() -> None:
    intent = parse_continuation_control_batch(_batch(("call-1", CONTINUATION_CONTROL_TOOL, {"notes": "Next: wire the sink."})))
    assert intent is not None and (intent.tool_call_id, intent.notes) == ("call-1", "Next: wire the sink.")
    bare = parse_continuation_control_batch(_batch(("call-2", CONTINUATION_CONTROL_TOOL, {})))
    assert bare is not None and bare.notes is None


@pytest.mark.parametrize("length", [129, 256])
def test_control_preserves_the_provider_admitted_tool_call_id(length: int) -> None:
    call_id = "c" * length
    intent = parse_continuation_control_batch(_provider_control_batch(call_id))
    assert intent is not None
    assert intent.tool_call_id == call_id
    assert intent.notes is None


def test_oversized_control_id_is_refused_at_provider_admission() -> None:
    with pytest.raises(AuditIntegrityError, match="Composer tool batch contains an oversized provider tool-call ID"):
        _provider_control_batch("c" * 257)


def test_a_mixed_batch_is_refused_before_any_effect() -> None:
    """K-2. Control: admit the control when other calls are present -> red."""
    with pytest.raises(ContinuationControlRefused) as refused:
        parse_continuation_control_batch(_batch(("call-1", CONTINUATION_CONTROL_TOOL, {}), ("call-2", "set_source", {"plugin": "csv"})))
    assert refused.value.error_code == CONTINUATION_CALL_NOT_ALONE
    assert refused.value.to_tool_payload()["error_code"] == CONTINUATION_CALL_NOT_ALONE


@pytest.mark.parametrize("arguments", [{"notes": ""}, {"notes": "   "}, {"notes": "x" * 501}, {"notes": "ok", "extra": 1}, {"notes": 7}])
def test_bad_arguments_are_refused(arguments) -> None:
    """Control: drop the bounds or extra='forbid' -> the matching case goes red."""
    with pytest.raises((ValueError, ValidationError)):
        parse_continuation_control_batch(_batch(("call-1", CONTINUATION_CONTROL_TOOL, arguments)))


def test_the_tool_result_never_claims_a_successor_ran() -> None:
    """K-4. Control: say the request continued or ran -> red."""
    assert "continues in a new step" in CONTINUATION_CONTROL_RESULT
    assert "Nothing has run yet" in CONTINUATION_CONTROL_RESULT
    for claim in ("has continued", "was continued", "completed", "succeeded"):
        assert claim not in CONTINUATION_CONTROL_RESULT


@pytest.mark.parametrize("dialect", list(ToolContractDialect))
def test_the_schema_is_closed_and_strict_where_the_transport_is(dialect) -> None:
    (definition,) = continuation_control_tool_definitions(dialect)
    function = definition["function"]
    assert isinstance(function, dict)
    assert function["name"] == CONTINUATION_CONTROL_TOOL
    parameters = function["parameters"]
    assert isinstance(parameters, dict)
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"notes"}
    assert ("strict" in function) == (dialect is ToolContractDialect.OPENAI_STRICT)


def test_both_new_codes_are_in_the_closed_registry() -> None:
    """K-6 (the census test_error_code_redaction.py also scans producers)."""
    assert {"CONTINUATION_CALL_NOT_ALONE", "CONTINUATION_UNAVAILABLE"} <= _EMITTED_VALIDATION_ERROR_CODES
