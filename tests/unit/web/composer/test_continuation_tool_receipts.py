import asyncio
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

from elspeth.contracts.composer_audit import ComposerToolInvocation, PrivateToolAuthoring, ToolArgumentErrorCategory
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _AdmittedToolFunction
from elspeth.web.composer.audit import begin_dispatch, finish_arg_error, finish_cancelled, finish_plugin_crash, finish_success
from elspeth.web.composer.continuation_tool_batch import _dump_owned, _read_json_value
from elspeth.web.composer.continuation_tool_receipts import (
    capture_tool_receipt,
    decode_retained_tool_receipt,
    encode_retained_tool_receipt,
    invocation_from_receipt,
    require_receipt_result,
)
from elspeth.web.composer.discovery_response import admit_discovery_result
from elspeth.web.composer.state import CompositionState, ValidationSummary
from elspeth.web.composer.tools._common import ToolResult, _serialize_full_pipeline_state
from elspeth.web.credential_guard import CredentialMaterialRefused


def actual_completion(*, changed=False, cache_hit=False):
    pre = CompositionState.from_dict(
        {"version": 1, "sources": {}, "nodes": [], "edges": [], "outputs": [], "metadata": {"name": "before", "description": ""}}
    )
    post = CompositionState.from_dict({**pre.to_dict(), "version": 2, "metadata": {"name": "after", "description": ""}}) if changed else pre
    arguments = {} if cache_hit else {"patch": {"name": "after"}}
    raw = canonical_json(arguments)
    call = _AdmittedToolCall("actual-call", _AdmittedToolFunction("get_pipeline_state" if cache_hit else "set_metadata", raw))
    audit = begin_dispatch(
        call.id, call.function.name, arguments, version_before=pre.version, actor="composer", strict_sent=True, wire_conformant=True
    )
    result = ToolResult(
        True,
        post,
        ValidationSummary(False, ()),
        (),
        prior_validation=None,
        data=_serialize_full_pipeline_state(post, requested_component=None) if cache_hit else None,
    )
    payload = {"success": result.success, "data": result.data, "cache_hit": True} if cache_hit else result.to_dict()
    invocation = finish_success(audit, result_payload=payload, version_after=post.version, cache_hit=cache_hit)
    authoring = PrivateToolAuthoring(call.id, call.function.name, raw, None, raw)
    return call, pre, audit, result, replace(invocation, private_authoring=authoring)


@pytest.mark.parametrize("changed", [False, True])
def test_completed_receipt_preserves_actual_timing_values_and_graph_alias_fact(changed):
    call, pre, audit, result, invocation = actual_completion(changed=changed)
    receipt = capture_tool_receipt(effect_key="memory:actual-call", invocation=invocation, result=result, pre_state=pre)
    encoded = encode_retained_tool_receipt(receipt)
    restored = decode_retained_tool_receipt(encoded)
    assert encode_retained_tool_receipt(restored) == encoded
    assert restored.result_state_is_pre_state is (not changed)
    completed = require_receipt_result(restored, selected_call=call, pre_state=pre, result=result)
    assert completed == invocation
    assert invocation_from_receipt(restored, audit=audit) == invocation
    assert completed.started_at == invocation.started_at
    assert completed.finished_at == invocation.finished_at
    assert completed.latency_ms == invocation.latency_ms
    assert (result.updated_state is pre) is (not changed)


def test_receipt_keeps_actual_cache_hit_envelope():
    call, pre, audit, result, invocation = actual_completion(cache_hit=True)
    receipt = capture_tool_receipt(effect_key="memory:actual-call", invocation=invocation, result=result, pre_state=pre)
    assert invocation_from_receipt(receipt, audit=audit) == invocation
    assert require_receipt_result(receipt, selected_call=call, pre_state=pre, result=result).cache_hit


@pytest.mark.parametrize("failure", ["argument", "plugin"])
def test_failed_receipt_retains_original_private_facts_without_exception_reconstruction(failure):
    call, pre, audit, _result, success = actual_completion()
    original = RuntimeError("original ordinary failure")
    if failure == "argument":
        invocation = finish_arg_error(
            audit,
            error_class="ToolArgumentError",
            error_category=ToolArgumentErrorCategory.MISSING_REQUIRED_PATH,
            error_message="required path missing",
            error_payload=None,
        )
    else:
        invocation = finish_plugin_crash(audit, exc=original)
    invocation = replace(invocation, private_authoring=success.private_authoring)
    receipt = capture_tool_receipt(effect_key="memory:actual-call", invocation=invocation, result=None, pre_state=pre)
    assert receipt.tool_result is None and receipt.result_state_is_pre_state is None
    actual = require_receipt_result(
        decode_retained_tool_receipt(encode_retained_tool_receipt(receipt)), selected_call=call, pre_state=pre, result=None
    )
    assert actual == invocation
    assert actual.private_failure == invocation.private_failure
    assert original.args == ("original ordinary failure",)


@pytest.mark.parametrize("changed", ["raw", "call_id", "pre_alias", "result", "audit_actor"])
def test_receipt_refuses_independent_selected_call_result_alias_and_audit_changes(changed):
    call, pre, audit, result, invocation = actual_completion()
    receipt = capture_tool_receipt(effect_key="memory:actual-call", invocation=invocation, result=result, pre_state=pre)
    assert require_receipt_result(receipt, selected_call=call, pre_state=pre, result=result) == invocation
    if changed == "audit_actor":
        with pytest.raises(AuditIntegrityError, match="separately admitted dispatch"):
            invocation_from_receipt(receipt, audit=replace(audit, actor="another-current-actor"))
        return
    selected = (
        replace(call, id="another-call")
        if changed == "call_id"
        else replace(call, function=replace(call.function, arguments=canonical_json({"patch": {"name": "other"}})))
        if changed == "raw"
        else call
    )
    before = CompositionState.from_dict(pre.to_dict()) if changed == "pre_alias" else pre
    selected_result = replace(result, success=False) if changed == "result" else result
    with pytest.raises(AuditIntegrityError):
        require_receipt_result(receipt, selected_call=selected, pre_state=before, result=selected_result)


@pytest.mark.parametrize("channel", ["effect", "actor", "raw", "error_message", "unknown_inner"])
def test_receipt_scans_complete_metadata_raw_and_error_channels(channel):
    _call, pre, audit, result, invocation = actual_completion()
    normal = capture_tool_receipt(effect_key="memory:actual-call", invocation=invocation, result=result, pre_state=pre)
    raw = _read_json_value(encode_retained_tool_receipt(normal))
    marker = "postgresql://user:opaque-password@example.invalid/database"  # secret-scan: allow-this-line
    if channel == "effect":
        raw["effect_key"] = marker
    else:
        inner = _read_json_value(raw["invocation_json"])
        if channel == "actor":
            inner["actor"] = marker
        elif channel == "raw":
            inner["private_authoring"]["raw_arguments"] = canonical_json({"credential": marker})
        elif channel == "unknown_inner":
            inner["unknown_retained_fact"] = "not allowed"
        else:
            failed = replace(
                finish_arg_error(
                    audit,
                    error_class="ToolArgumentError",
                    error_category=ToolArgumentErrorCategory.MISSING_REQUIRED_PATH,
                    error_message="safe refusal",
                ),
                private_authoring=invocation.private_authoring,
            )
            failure_receipt = capture_tool_receipt(effect_key="memory:actual-call", invocation=failed, result=None, pre_state=pre)
            raw = _read_json_value(encode_retained_tool_receipt(failure_receipt))
            inner = _read_json_value(raw["invocation_json"])
            inner["error_message"] = marker
        raw["invocation_json"] = canonical_json(inner)
    expected_refusal = AuditIntegrityError if channel == "unknown_inner" else CredentialMaterialRefused
    with pytest.raises(expected_refusal):
        decode_retained_tool_receipt(canonical_json(raw))


def test_receipt_refuses_valid_rehashed_result_substitution():
    _call, pre, _audit, result, invocation = actual_completion()
    normal = capture_tool_receipt(effect_key="memory:actual-call", invocation=invocation, result=result, pre_state=pre)
    raw = _read_json_value(encode_retained_tool_receipt(normal))
    inner = _read_json_value(raw["invocation_json"])
    payload = _read_json_value(inner["result_canonical"])
    payload["success"] = False
    inner["result_canonical"] = canonical_json(payload)
    inner["result_hash"] = sha256(inner["result_canonical"].encode("utf-8")).hexdigest()
    raw["invocation_json"] = canonical_json(inner)
    with pytest.raises(AuditIntegrityError, match="actual completed result"):
        decode_retained_tool_receipt(canonical_json(raw))


def test_receipt_accepts_actual_selected_discovery_result_without_storing_contract_callbacks():
    call, pre, audit, result, _cache = actual_completion(cache_hit=True)
    admitted = admit_discovery_result(call.function.name, result)
    completed = finish_success(audit, result_payload=admitted.to_dict(), version_after=pre.version)
    completed = replace(
        completed,
        private_authoring=PrivateToolAuthoring(call.id, call.function.name, call.function.arguments, None, call.function.arguments),
    )
    receipt = capture_tool_receipt(effect_key="read:actual-call", invocation=completed, result=admitted, pre_state=pre)
    restored = decode_retained_tool_receipt(encode_retained_tool_receipt(receipt))
    assert require_receipt_result(restored, selected_call=call, pre_state=pre, result=admitted) == completed
    assert invocation_from_receipt(restored, audit=audit).latency_ms == completed.latency_ms


@pytest.mark.parametrize("member", ["arguments_hash", "result_hash"])
def test_receipt_recomputes_actual_canonical_digest(member):
    _call, pre, _audit, result, invocation = actual_completion()
    normal = capture_tool_receipt(effect_key="memory:actual-call", invocation=invocation, result=result, pre_state=pre)
    raw = _read_json_value(encode_retained_tool_receipt(normal))
    inner = _read_json_value(raw["invocation_json"])
    inner[member] = "0" * 64
    raw["invocation_json"] = canonical_json(inner)
    with pytest.raises(AuditIntegrityError, match="canonical mapping hash"):
        decode_retained_tool_receipt(canonical_json(raw))


def actual_failed_completion(status):
    call, pre, audit, _result, success = actual_completion()
    if status == "argument":
        invocation = finish_arg_error(
            audit,
            error_class="ToolArgumentError",
            error_category=ToolArgumentErrorCategory.MISSING_REQUIRED_PATH,
            error_message="required path missing",
            error_payload={"error": "required path missing"},
        )
    elif status == "cancelled":
        invocation = finish_cancelled(audit, exc=asyncio.CancelledError("coordinator_cancelled"))
    else:
        invocation = finish_plugin_crash(audit, exc=RuntimeError("ordinary private failure"))
    return call, pre, audit, replace(invocation, private_authoring=success.private_authoring)


@pytest.mark.parametrize("status", ["argument", "cancelled", "plugin"])
def test_receipt_accepts_actual_failure_finisher_shapes(status):
    call, pre, audit, invocation = actual_failed_completion(status)
    receipt = capture_tool_receipt(effect_key="memory:actual-call", invocation=invocation, result=None, pre_state=pre)
    restored = decode_retained_tool_receipt(encode_retained_tool_receipt(receipt))
    assert require_receipt_result(restored, selected_call=call, pre_state=pre, result=None) == invocation
    assert invocation_from_receipt(restored, audit=audit) == invocation
    assert restored.tool_result is None and restored.result_state_is_pre_state is None
    assert (invocation.result_canonical is not None) is (status == "argument")
    assert (invocation.private_failure is not None) is (status == "plugin")
    assert invocation.cache_hit is False


@pytest.mark.parametrize(
    "status,changed",
    [
        ("cancelled", "payload"),
        ("plugin", "payload"),
        ("argument", "cache"),
        ("cancelled", "cache"),
        ("plugin", "cache"),
        ("argument", "private_failure"),
        ("cancelled", "private_failure"),
        ("plugin", "private_failure"),
        ("cancelled", "error_class"),
        ("cancelled", "error_message"),
        ("plugin", "error_message"),
    ],
)
@pytest.mark.parametrize("route", ["capture", "decode"])
def test_receipt_refuses_cross_status_failure_shapes(status, changed, route):
    _call, pre, _audit, invocation = actual_failed_completion(status)
    receipt = capture_tool_receipt(effect_key="memory:actual-call", invocation=invocation, result=None, pre_state=pre)
    encoded = encode_retained_tool_receipt(receipt)
    assert decode_retained_tool_receipt(encoded) == receipt
    if changed == "payload":
        payload = canonical_json({"safe": "extra"})
        bad = replace(invocation, result_canonical=payload, result_hash=sha256(payload.encode("utf-8")).hexdigest())
    elif changed == "cache":
        bad = replace(invocation, cache_hit=True)
    elif changed == "private_failure":
        _other_call, _other_pre, _other_audit, plugin = actual_failed_completion("plugin")
        bad = replace(invocation, private_failure=None if status == "plugin" else plugin.private_failure)
    elif changed == "error_class":
        bad = replace(invocation, error_class="AnotherCancellation")
    else:
        bad = replace(invocation, error_message="another safe description")
    with pytest.raises(AuditIntegrityError):
        if route == "capture":
            capture_tool_receipt(effect_key="memory:actual-call", invocation=bad, result=None, pre_state=pre)
        else:
            wire = _read_json_value(encoded)
            wire["invocation_json"] = _dump_owned(TypeAdapter(ComposerToolInvocation), bad)
            decode_retained_tool_receipt(canonical_json(wire))


def test_receipt_refuses_structural_selected_function_impostor():
    call, pre, _audit, result, invocation = actual_completion()
    receipt = capture_tool_receipt(effect_key="memory:actual-call", invocation=invocation, result=result, pre_state=pre)
    assert require_receipt_result(receipt, selected_call=call, pre_state=pre, result=result) == invocation
    impostor = SimpleNamespace(name=call.function.name, arguments=call.function.arguments)
    selected = replace(call, function=impostor)
    with pytest.raises(TypeError, match="actual selected function"):
        require_receipt_result(receipt, selected_call=selected, pre_state=pre, result=result)
