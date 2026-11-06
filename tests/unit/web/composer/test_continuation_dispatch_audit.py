"""Real audit owner and finishers under finite controlled monotonic observations."""

import json
from dataclasses import replace

import pytest

from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import audit as audit_owner
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _AdmittedToolFunction
from elspeth.web.composer.audit import begin_dispatch, finish_arg_error, finish_success
from elspeth.web.composer.continuation_dispatch_audit import (
    capture_dispatch_audit,
    decode_dispatch_audit,
    encode_dispatch_audit,
    re_admit_dispatch_audit,
)
from elspeth.web.composer.state import CompositionState
from elspeth.web.credential_guard import CredentialMaterialRefused


def state():
    return CompositionState.from_dict(
        {"sources": {}, "nodes": [], "edges": [], "outputs": [], "metadata": {"name": "audit", "description": ""}, "version": 3}
    )


def ticks(monkeypatch, values):
    observed = iter(values)
    monkeypatch.setattr(audit_owner.time, "monotonic_ns", lambda: next(observed))


def initial(tool="get_pipeline_state", arguments=None):
    arguments = {"component": "summary"} if arguments is None else arguments
    call = _AdmittedToolCall(
        "actual-call", _AdmittedToolFunction(tool, canonical_json(arguments) if not isinstance(arguments, str) else arguments)
    )
    audit = begin_dispatch(
        call.id, tool, arguments, version_before=3, actor="composer-web:user:alice", strict_sent=True, wire_conformant=False
    )
    return call, arguments, audit


def resume(value, call, arguments, **changes):
    kwargs = {
        "selected_call": call,
        "pre_state": state(),
        "audit_arguments": arguments,
        "actor": "composer-web:user:alice",
        "strict_sent": True,
        "wire_conformant": False,
    }
    kwargs.update(changes)
    return re_admit_dispatch_audit(value, **kwargs)


def test_new_clock_epoch_preserves_active_latency_without_reusing_old_anchor(monkeypatch):
    ticks(monkeypatch, [1_000_000_000, 1_006_000_000, 100, 2_000_100])
    call, arguments, original = initial()
    retained = capture_dispatch_audit(original)
    encoded = encode_dispatch_audit(retained)
    assert retained.elapsed_active_ns == 6_000_000
    assert "started_ns" not in json.loads(encoded)
    restored = resume(decode_dispatch_audit(encoded), call, arguments)
    assert restored.started_ns == 100 - 6_000_000
    invocation = finish_success(restored, result_payload={"status": "complete"}, version_after=3)
    assert invocation.latency_ms == 8
    assert invocation.started_at == original.started_at
    assert invocation.arguments_canonical == original.arguments_canonical
    assert invocation.arguments_hash == original.arguments_hash
    assert invocation.actor == original.actor
    assert invocation.strict_sent is True and invocation.wire_conformant is False
    assert original.started_ns == 1_000_000_000


def test_repeated_capture_accumulates_only_resumed_active_segments(monkeypatch):
    ticks(monkeypatch, [100_000_000, 103_000_000, 9_000_000_000, 9_004_000_000, 500, 2_000_500])
    call, arguments, original = initial()
    first = capture_dispatch_audit(original)
    resumed = resume(first, call, arguments)
    second = capture_dispatch_audit(resumed)
    assert second.elapsed_active_ns == 7_000_000
    again = resume(second, call, arguments)
    invocation = finish_arg_error(
        again,
        error_class="ValueError",
        error_category=ToolArgumentErrorCategory.MODEL_VALIDATION,
        error_message="invalid argument",
        error_payload={"error": "invalid argument"},
    )
    assert invocation.latency_ms == 9
    assert invocation.error_class == "ValueError"
    assert invocation.error_category is ToolArgumentErrorCategory.MODEL_VALIDATION
    assert invocation.started_at == original.started_at


def test_future_anchor_refuses_instead_of_fabricating_zero_duration(monkeypatch):
    ticks(monkeypatch, [100, 99])
    _call, _arguments, original = initial()
    with pytest.raises(AuditIntegrityError, match="anchor is ahead"):
        capture_dispatch_audit(original)
    assert original.started_ns == 100


@pytest.mark.parametrize("change", ["actor", "call", "state", "arguments", "strict", "wire"])
def test_current_actual_audit_binding_must_match_every_retained_input(monkeypatch, change):
    ticks(monkeypatch, [100, 200, 300])
    call, arguments, original = initial()
    retained = capture_dispatch_audit(original)
    changes = {}
    if change == "actor":
        changes["actor"] = "composer-web:user:bob"
    elif change == "call":
        changes["selected_call"] = replace(call, id="other-call")
    elif change == "state":
        changes["pre_state"] = replace(state(), version=4)
    elif change == "arguments":
        changes["audit_arguments"] = {"component": "nodes"}
    elif change == "strict":
        changes["strict_sent"] = None
    else:
        changes["wire_conformant"] = True
    with pytest.raises(AuditIntegrityError, match="selected audit binding"):
        resume(retained, call, arguments, **changes)


def test_order_bearing_authority_hash_is_rederived_by_actual_owner(monkeypatch):
    ticks(monkeypatch, [100, 200, 300])
    arguments = {"sources": {"z_source": {}, "a_source": {}}, "nodes": []}
    call, arguments, original = initial("set_pipeline", arguments)
    retained = capture_dispatch_audit(original)
    changed = {"sources": {"a_source": {}, "z_source": {}}, "nodes": []}
    assert canonical_json(arguments) == canonical_json(changed)
    with pytest.raises(AuditIntegrityError, match="selected audit binding"):
        resume(retained, call, arguments, audit_arguments=changed)


def test_actual_raw_argument_sentinel_and_wire_absence_survive(monkeypatch):
    ticks(monkeypatch, [100, 200, 300, 1_000_300])
    raw = '{"unfinished":'
    call = _AdmittedToolCall("raw-call", _AdmittedToolFunction("get_pipeline_state", raw))
    original = begin_dispatch(call.id, call.function.name, raw, version_before=3, actor="actor", strict_sent=None, wire_conformant=None)
    retained = capture_dispatch_audit(original)
    restored = re_admit_dispatch_audit(
        decode_dispatch_audit(encode_dispatch_audit(retained)),
        selected_call=call,
        pre_state=state(),
        audit_arguments=raw,
        actor="actor",
        strict_sent=None,
        wire_conformant=None,
    )
    invocation = finish_arg_error(
        restored,
        error_class="JSONDecodeError",
        error_category=ToolArgumentErrorCategory.WIRE_JSON_INVALID,
        error_message="JSONDecodeError",
        error_payload={"error": "invalid JSON"},
    )
    assert invocation.arguments_canonical == original.arguments_canonical
    assert invocation.authority_arguments_canonical is None
    assert invocation.strict_sent is None and invocation.wire_conformant is None


@pytest.mark.parametrize("change", ["missing", "unknown", "negative", "hash", "naive", "timing"])
def test_complete_portable_value_rejects_missing_normalized_or_invalid_facts(monkeypatch, change):
    ticks(monkeypatch, [100, 200])
    _call, _arguments, original = initial()
    value = json.loads(encode_dispatch_audit(capture_dispatch_audit(original)))
    if change == "missing":
        value.pop("wire_conformant")
    elif change == "unknown":
        value["unknown"] = None
    elif change == "negative":
        value["elapsed_active_ns"] = -1
    elif change == "hash":
        value["arguments_hash"] = "0" * 64
    elif change == "naive":
        value["started_at"] = "2026-01-01T00:00:00"
    else:
        value["timing_semantics"] = "wall_elapsed"
    with pytest.raises(AuditIntegrityError):
        decode_dispatch_audit(canonical_json(value))


def test_credentials_in_actual_audit_arguments_are_not_retained(monkeypatch):
    ticks(monkeypatch, [100, 200])
    _call, _arguments, original = initial(arguments={"api_key": "opaque-private-value"})
    with pytest.raises(CredentialMaterialRefused):
        capture_dispatch_audit(original)


@pytest.mark.asyncio
async def test_capture_leaves_live_exception_identity_and_original_timer_owner_unchanged(monkeypatch):
    ticks(monkeypatch, [100_000_000, 101_000_000, 110_000_000])
    _call, _arguments, original = initial()
    retained = capture_dispatch_audit(original)
    assert retained.elapsed_active_ns == 1_000_000
    recorder = audit_owner.BufferingRecorder()
    failure, cause = AssertionError("original invariant"), ValueError("original cause")

    async def fail():
        raise failure from cause

    with pytest.raises(AssertionError) as caught:
        await audit_owner.dispatch_with_audit(
            recorder=recorder,
            audit=original,
            do_dispatch=fail,
            version_after_provider=lambda result: 3,
            arg_error_payload_factory=lambda error: {"error": "argument"},
        )
    assert caught.value is failure and caught.value.__cause__ is cause
    assert original.started_ns == 100_000_000
    assert len(recorder.invocations) == 1
    assert recorder.invocations[0].latency_ms == 10
    assert recorder.invocations[0].error_class == "AssertionError"


def test_ordered_sources_and_real_field_mapper_domain_readmit_without_rewriting(monkeypatch):
    ticks(monkeypatch, [100, 200, 300])
    arguments = {
        "sources": {"z_source": {}, "a_source": {}},
        "nodes": [
            {
                "id": "rename",
                "node_type": "transform",
                "plugin": "field_mapper",
                "input": "rows",
                "on_success": "out",
                "on_error": "discard",
                "options": {"mapping": {"customer_key": "customer_id"}, "schema": {"mode": "observed"}},
            }
        ],
    }
    call, arguments, original = initial("set_pipeline", arguments)
    retained = decode_dispatch_audit(encode_dispatch_audit(capture_dispatch_audit(original)))
    restored = resume(retained, call, arguments)
    assert restored.authority_arguments_canonical == original.authority_arguments_canonical
    assert restored.authority_arguments_hash == original.authority_arguments_hash
    assert tuple(arguments["sources"]) == ("z_source", "a_source")
    assert arguments["nodes"][0]["options"]["mapping"] == {"customer_key": "customer_id"}
    assert restored.started_at == original.started_at


def test_standalone_zero_version_audit_refuses_portable_capture_without_fabricating_state(monkeypatch):
    from pydantic import ValidationError

    ticks(monkeypatch, [100, 200])
    original = begin_dispatch(
        "standalone-call",
        "get_pipeline_state",
        {"component": "summary"},
        version_before=0,
        actor="standalone",
        strict_sent=None,
        wire_conformant=None,
    )
    assert original.version_before == 0
    with pytest.raises(ValidationError) as caught:
        capture_dispatch_audit(original)
    errors = caught.value.errors(include_url=False)
    assert len(errors) == 1
    assert errors[0]["loc"] == ("version_before",)
    assert errors[0]["type"] == "greater_than_equal"
    assert errors[0]["input"] == 0
    assert original.version_before == 0 and original.started_ns == 100


def test_minimum_actual_state_version_preserves_selected_captured_segments(monkeypatch):
    ticks(monkeypatch, [1_000_000, 3_000_000, 100, 1_000_100])
    pre_state = CompositionState.from_dict(
        {"sources": {}, "nodes": [], "edges": [], "outputs": [], "metadata": {"name": "minimum-version", "description": ""}, "version": 1}
    )
    arguments = {"component": "summary"}
    call = _AdmittedToolCall("version-one-call", _AdmittedToolFunction("get_pipeline_state", canonical_json(arguments)))
    original = begin_dispatch(
        call.id,
        call.function.name,
        arguments,
        version_before=pre_state.version,
        actor="composer-web:user:alice",
        strict_sent=None,
        wire_conformant=None,
    )
    retained = decode_dispatch_audit(encode_dispatch_audit(capture_dispatch_audit(original)))
    assert retained.version_before == 1
    assert retained.timing_semantics == "captured_dispatch_segments"
    assert retained.elapsed_active_ns == 2_000_000
    restored = re_admit_dispatch_audit(
        retained,
        selected_call=call,
        pre_state=pre_state,
        audit_arguments=arguments,
        actor="composer-web:user:alice",
        strict_sent=None,
        wire_conformant=None,
    )
    assert restored.started_ns == 100 - 2_000_000
    assert restored.started_at == original.started_at
    invocation = finish_success(restored, result_payload={"status": "complete"}, version_after=pre_state.version)
    assert invocation.version_before == invocation.version_after == 1
    assert invocation.latency_ms == 3
    assert original.started_ns == 1_000_000 and pre_state.version == 1
