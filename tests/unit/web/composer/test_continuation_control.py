"""The checkpoint_and_continue control (U-40): schema, sole-call rule, and refusal codes.

Design §2.2. Each test names its mutation control.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_controls as controls
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
from elspeth.web.credential_guard import CredentialMaterialRefused


def _batch(*calls: tuple[str, str, object]) -> _AdmittedToolBatch:
    admitted = tuple(_AdmittedToolCall(call_id, _AdmittedToolFunction(name, json.dumps(arguments))) for call_id, name, arguments in calls)
    return _AdmittedToolBatch(admitted, frozenset(call.id for call in admitted))


def _provider_control_batch(call_id: str, *, raw_arguments: str = "{}") -> _AdmittedToolBatch:
    from litellm.types.utils import ChatCompletionMessageToolCall, Function

    call = ChatCompletionMessageToolCall(
        id=call_id,
        type="function",
        function=Function(name=CONTINUATION_CONTROL_TOOL, arguments=raw_arguments),
    )
    return _admit_tool_batch((call,))


def _assert_safe_argument_refusal(refusal: ContinuationControlRefused, *, hidden: tuple[str, ...] = ()) -> None:
    payload = refusal.to_tool_payload()
    assert set(payload) == {"status", "error", "error_code"}
    assert payload["status"] == "ARG_ERROR"
    assert payload["error_code"] == refusal.error_code == "argument_error"
    assert payload["error_code"] in _EMITTED_VALIDATION_ERROR_CODES
    assert payload["error"] == refusal.detail == str(refusal)
    assert payload["error"].strip()
    public = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for candidate in hidden:
        assert candidate.encode("utf-8") not in public
        assert candidate not in str(refusal)
        assert candidate not in repr(refusal)


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


@pytest.mark.parametrize(
    "raw",
    [
        '{"notes":"first candidate","notes":"second candidate"}',
        '{"notes":null,"notes":"second candidate"}',
        r'{"notes":"first candidate","no\u0074es":"second candidate"}',
    ],
)
def test_duplicate_json_keys_are_safe_control_argument_refusals_without_losing_raw_arguments(raw: str) -> None:
    batch = _provider_control_batch("actual-paid-control", raw_arguments=raw)
    call = batch.calls[0]
    original_bytes = call.function.arguments.encode("utf-8")
    with pytest.raises(ContinuationControlRefused) as refused:
        parse_continuation_control_batch(batch)
    _assert_safe_argument_refusal(refused.value, hidden=("first candidate", "second candidate"))
    assert batch.calls[0] is call and call.id == "actual-paid-control"
    assert call.function.arguments.encode("utf-8") == original_bytes == raw.encode("utf-8")


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param('{"notes":"untrusted-control-marker",}', id="malformed-json"),
        pytest.param('{"notes":', id="truncated-json"),
        pytest.param("", id="empty-json"),
        pytest.param("null", id="null-document"),
        pytest.param("[]", id="array-document"),
        pytest.param('"untrusted-control-marker"', id="string-document"),
        pytest.param("7", id="number-document"),
        pytest.param('{"notes":7}', id="number-notes"),
        pytest.param('{"notes":true}', id="boolean-notes"),
        pytest.param('{"notes":{}}', id="object-notes"),
        pytest.param('{"notes":[]}', id="array-notes"),
        pytest.param('{"notes":"ok","untrusted-control-marker":"extra"}', id="extra-field"),
        pytest.param('{"notes":""}', id="empty-notes"),
        pytest.param(json.dumps({"notes": " \n\t "}), id="blank-notes"),
        pytest.param(json.dumps({"notes": "untrusted-control-marker" + "x" * 501}), id="oversized-notes"),
        pytest.param('{"notes":NaN}', id="nonfinite-json"),
        pytest.param(r'{"notes":"\ud800"}', id="unpaired-surrogate-notes"),
    ],
)
def test_invalid_arguments_become_safe_normal_control_refusals_and_retain_the_paid_call(raw: str) -> None:
    batch = _provider_control_batch("actual-paid-invalid-control", raw_arguments=raw)
    call = batch.calls[0]
    original_bytes = call.function.arguments.encode("utf-8")
    with pytest.raises(ContinuationControlRefused) as refused:
        parse_continuation_control_batch(batch)
    _assert_safe_argument_refusal(refused.value, hidden=("untrusted-control-marker",))
    assert batch.calls[0] is call and call.id == "actual-paid-invalid-control"
    assert call.function.arguments.encode("utf-8") == original_bytes == raw.encode("utf-8")


def test_credential_notes_are_safe_normal_control_refusals_without_public_candidate_bytes() -> None:
    candidate = "Bearer " + "a" * 24
    notes = candidate + " untrusted-control-marker"
    # Positive control: these exact notes reach the real credential refusal.
    with pytest.raises(CredentialMaterialRefused):
        controls.require_no_credential_material(notes, surface="composer_continuation_notes")
    raw = json.dumps({"notes": notes})
    batch = _provider_control_batch("actual-paid-credential-control", raw_arguments=raw)
    call = batch.calls[0]
    with pytest.raises(ContinuationControlRefused) as refused:
        parse_continuation_control_batch(batch)
    _assert_safe_argument_refusal(refused.value, hidden=(candidate, "untrusted-control-marker"))
    assert batch.calls[0] is call and call.id == "actual-paid-credential-control"
    assert call.function.arguments.encode("utf-8") == raw.encode("utf-8")


@pytest.mark.parametrize("notes", ["a" * 500, "\u00e9" * 250])
def test_notes_at_the_utf8_byte_limit_keep_the_original_paid_call_and_raw_arguments(notes: str) -> None:
    raw = json.dumps({"notes": notes}, ensure_ascii=False)
    batch = _provider_control_batch("actual-paid-limit-control", raw_arguments=raw)
    call = batch.calls[0]
    intent = parse_continuation_control_batch(batch)
    assert intent is not None and intent.notes == notes and intent.tool_call_id == call.id
    assert len(intent.notes.encode("utf-8")) == 500
    assert batch.calls[0] is call and call.function.arguments.encode("utf-8") == raw.encode("utf-8")


def test_notes_one_utf8_byte_over_limit_are_a_safe_control_refusal() -> None:
    notes = "\u00e9" * 250 + "x"
    assert len(notes.encode("utf-8")) == 501
    raw = json.dumps({"notes": notes}, ensure_ascii=False)
    batch = _provider_control_batch("actual-paid-limit-plus-one", raw_arguments=raw)
    call = batch.calls[0]
    with pytest.raises(ContinuationControlRefused) as refused:
        parse_continuation_control_batch(batch)
    _assert_safe_argument_refusal(refused.value, hidden=(notes,))
    assert batch.calls[0] is call and call.id == "actual-paid-limit-plus-one"
    assert call.function.arguments.encode("utf-8") == raw.encode("utf-8")


def test_reparsing_the_same_paid_call_preserves_identity_and_exact_raw_arguments() -> None:
    raw = ' {\n "notes" : "  Continue this same request.  "\n}\n'
    batch = _provider_control_batch("c" * 256, raw_arguments=raw)
    call = batch.calls[0]
    first = parse_continuation_control_batch(batch)
    second = parse_continuation_control_batch(batch)
    assert first is not None and second == first
    assert first.tool_call_id == second.tool_call_id == call.id == "c" * 256
    assert first.notes == second.notes == "  Continue this same request.  "
    assert batch.calls[0] is call
    assert call.function.arguments.encode("utf-8") == raw.encode("utf-8")


@pytest.mark.parametrize("control_first", [True, False])
@pytest.mark.parametrize("raw", ['{"notes":"ordinary notes"}', '{"notes":'])
def test_mixed_control_batch_refuses_before_argument_or_credential_work(
    monkeypatch: pytest.MonkeyPatch, control_first: bool, raw: str
) -> None:
    observed: list[tuple[object, str]] = []
    real_guard = controls.require_no_credential_material

    def observe_guard(value: object, *, surface: str) -> None:
        observed.append((value, surface))
        real_guard(value, surface=surface)

    monkeypatch.setattr(controls, "require_no_credential_material", observe_guard)
    # Prove the observation seam is reached by a real positive sole call.
    positive = parse_continuation_control_batch(_batch(("positive", CONTINUATION_CONTROL_TOOL, {"notes": "ordinary notes"})))
    assert positive is not None and observed == [("ordinary notes", "composer_continuation_notes")]
    observed.clear()
    control = _provider_control_batch("actual-paid-mixed-control", raw_arguments=raw).calls[0]
    mutation = _batch(("actual-paid-mutation", "set_source", {"plugin": "csv"})).calls[0]
    calls = (control, mutation) if control_first else (mutation, control)
    batch = _AdmittedToolBatch(calls, frozenset(call.id for call in calls))
    originals = tuple((call.id, call.function.arguments.encode("utf-8")) for call in calls)
    with pytest.raises(ContinuationControlRefused) as refused:
        parse_continuation_control_batch(batch)
    assert refused.value.error_code == CONTINUATION_CALL_NOT_ALONE
    assert refused.value.to_tool_payload()["status"] == "ARG_ERROR"
    assert refused.value.to_tool_payload()["error_code"] == CONTINUATION_CALL_NOT_ALONE
    assert observed == [] and batch.calls is calls
    assert tuple((call.id, call.function.arguments.encode("utf-8")) for call in batch.calls) == originals


def test_two_control_calls_are_a_sole_call_refusal_not_a_successful_duplicate() -> None:
    first = _provider_control_batch("actual-paid-first-control", raw_arguments='{"notes":"first"}').calls[0]
    second = _provider_control_batch("actual-paid-second-control", raw_arguments='{"notes":').calls[0]
    calls = (first, second)
    batch = _AdmittedToolBatch(calls, frozenset(call.id for call in calls))
    originals = tuple((call.id, call.function.arguments.encode("utf-8")) for call in calls)
    with pytest.raises(ContinuationControlRefused) as refused:
        parse_continuation_control_batch(batch)
    assert refused.value.error_code == CONTINUATION_CALL_NOT_ALONE
    assert batch.calls is calls
    assert tuple((call.id, call.function.arguments.encode("utf-8")) for call in batch.calls) == originals


def test_ordinary_malformed_tool_arguments_are_left_to_their_own_parser() -> None:
    raw = '{"plugin":'
    call = _AdmittedToolCall("ordinary-paid-call", _AdmittedToolFunction("set_source", raw))
    batch = _AdmittedToolBatch((call,), frozenset({call.id}))
    assert parse_continuation_control_batch(batch) is None
    assert batch.calls[0] is call and call.function.arguments == raw


def test_unexpected_credential_guard_defect_is_not_relabelled_as_model_argument_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    original = RuntimeError("first-party credential guard defect")

    def broken_guard(_value: object, *, surface: str) -> None:
        assert surface == "composer_continuation_notes"
        raise original

    monkeypatch.setattr(controls, "require_no_credential_material", broken_guard)
    batch = _provider_control_batch("actual-paid-guard-defect", raw_arguments='{"notes":"ordinary notes"}')
    with pytest.raises(RuntimeError) as caught:
        parse_continuation_control_batch(batch)
    assert caught.value is original


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
