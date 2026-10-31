"""Actual response/argument owners precede retained P4 domain projections."""

import json
from dataclasses import replace
from hashlib import sha256

import pytest

from elspeth.contracts.composer_audit import ComposerToolStatus, PrivateToolAuthoring, ToolArgumentErrorCategory
from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer.audit import begin_dispatch, finish_arg_error
from elspeth.web.composer.continuation_prepared_p4 import (
    capture_prepared_p4,
    decode_prepared_p4,
    encode_prepared_p4,
    prepared_p4_members,
)
from elspeth.web.composer.continuation_result_credentials import (
    require_retained_argument_credentials,
    require_retained_graph_credentials,
    require_retained_result_credentials,
)
from elspeth.web.composer.continuation_tool_batch import decode_tool_result_value, encode_tool_result_value
from elspeth.web.composer.diagnostic_evidence import build_private_tool_evidence
from elspeth.web.composer.discovery_response import admit_discovery_result
from elspeth.web.composer.state import ValidationSummary
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.sessions._persist_payload import RedactedToolRow
from tests.unit.web.composer.test_applied_component_echo import _empty_state
from tests.unit.web.composer.test_continuation_prepared_p4 import actual_result_input, prepared_input, schema_result


def _context():
    return SessionOperationContext(SessionOperationFence("session", "creator", "fixture-lease", 7), SessionOperationKind.COMPOSE)


def _mapper_node():
    return {
        "node_type": "transform",
        "plugin": "field_mapper",
        "options": {
            "mapping": {"api_key": "customer_label"},
            "schema": {"mode": "observed"},
        },
    }


def _arg_error_input(tool, arguments, *, raw_arguments=None):
    """Use the real pre-dispatch owner; no ToolResult or current state is invented."""
    arguments_json = canonical_json(arguments)
    authoring = PrivateToolAuthoring("arg-error", tool, arguments_json, None, arguments_json if raw_arguments is None else raw_arguments)
    audit = begin_dispatch("arg-error", tool, arguments, version_before=0, actor="composer", strict_sent=True, wire_conformant=False)
    invocation = finish_arg_error(
        audit,
        error_class="ToolArgumentError",
        error_category=ToolArgumentErrorCategory.MODEL_VALIDATION,
        error_message="Argument was refused",
        error_payload={"status": "ARG_ERROR", "error": "Argument was refused"},
    )
    invocation = replace(invocation, private_authoring=authoring)
    assert invocation.status is ComposerToolStatus.ARG_ERROR
    assert invocation.result_canonical is not None
    selected = invocation.result_canonical
    event = VisibleToolEvent.capture(
        context=_context(),
        request_id="argument-request",
        tool_call_id="arg-error",
        tool_name=tool,
        received_arguments=authoring.raw_arguments,
        effective_arguments=arguments_json,
        selected_result=selected,
        display_arguments=arguments_json,
        display_result=selected,
    )
    rows = (RedactedToolRow("arg-error", selected, None, event),)
    evidence = build_private_tool_evidence(
        session_id="session",
        operation_id="creator",
        operation_epoch=7,
        tool_call_id="arg-error",
        tool_name=tool,
        status=invocation.status.value,
        arguments_canonical=arguments_json,
        raw_arguments=authoring.raw_arguments,
        result_canonical=selected,
        error_category=ToolArgumentErrorCategory.MODEL_VALIDATION.value,
    )
    return capture_prepared_p4(
        context=_context(),
        assistant_content="Argument repair required",
        raw_content=None,
        redacted_calls=(
            {
                "id": "arg-error",
                "type": "function",
                "function": {"name": tool, "arguments": arguments_json},
                "strict_sent": True,
                "wire_conformant": False,
            },
        ),
        redacted_rows=rows,
        revoked_rows=rows,
        mutation_ceiling=None,
        authority_revocation=None,
        private_tool_evidence=(evidence,),
        private_tool_failures=(),
        rejection_records=(),
        withheld_replies=(),
        invocations=(invocation,),
        authorings=(authoring,),
        parent_composition_state_id=None,
        expected_current_state_id=None,
        assistant_row_uses_current_dispatch=True,
        crash_pending=False,
    )


def _replace_selected_result(value, wire):
    """Reproduce real evidence from changed source, rather than corrupt its hash."""
    members = prepared_p4_members(value)
    invocation, authoring = members.invocations[0], members.authorings[0]
    selected = canonical_json(wire)
    audit = json.loads(value.source_audit_json)
    audit["invocations"][0]["result_canonical"] = selected
    audit["invocations"][0]["result_hash"] = sha256(selected.encode()).hexdigest()
    rows = json.loads(value.redacted_rows_json)
    calls = json.loads(value.redacted_calls_json)
    original_event = members.redacted_rows[0].visible_event
    assert original_event is not None
    event = VisibleToolEvent.capture(
        context=_context(),
        request_id=original_event.producer_declared_request_id,
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        received_arguments=authoring.raw_arguments,
        effective_arguments=invocation.arguments_canonical,
        selected_result=selected,
        display_arguments=calls[0]["function"]["arguments"],
        display_result=rows[0]["content"],
    )
    rows[0]["visible_event_json"] = canonical_json(event.to_envelope())
    evidence = build_private_tool_evidence(
        session_id="session",
        operation_id="creator",
        operation_epoch=7,
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        status=invocation.status.value,
        arguments_canonical=authoring.arguments_canonical,
        raw_arguments=authoring.raw_arguments,
        authority_arguments_canonical=authoring.authority_arguments_canonical,
        result_canonical=selected,
        error_category=None if invocation.error_category is None else invocation.error_category.value,
    )
    return value.model_copy(
        update={
            "source_audit_json": canonical_json(audit),
            "private_tool_evidence_json": canonical_json([evidence.to_envelope()]),
            "redacted_rows_json": canonical_json(rows),
            "revoked_rows_json": canonical_json(rows),
        }
    )


@pytest.mark.parametrize("node_type", ["gate", "aggregation", None])
def test_field_mapper_projection_requires_actual_node_type(node_type):
    node = _mapper_node()
    require_retained_argument_credentials("upsert_node", node, env_ref_names=frozenset())
    require_retained_graph_credentials({"nodes": [node]}, env_ref_names=frozenset())
    if node_type is None:
        node.pop("node_type")
    else:
        node["node_type"] = node_type
    with pytest.raises(CredentialMaterialRefused):
        require_retained_argument_credentials("upsert_node", node, env_ref_names=frozenset())
    with pytest.raises(CredentialMaterialRefused):
        require_retained_graph_credentials({"nodes": [node]}, env_ref_names=frozenset())


def test_actual_splice_projection_keeps_its_explicit_transform_shape():
    node = _mapper_node()
    node.pop("node_type")
    require_retained_argument_credentials("splice_transform", {"node": node}, env_ref_names=frozenset())
    with pytest.raises(CredentialMaterialRefused):
        require_retained_argument_credentials("upsert_node", node, env_ref_names=frozenset())


def test_failed_source_field_mapper_intent_does_not_gain_transform_projection():
    node = _mapper_node()
    value = _arg_error_input("upsert_node", node)
    assert prepared_p4_members(decode_prepared_p4(encode_prepared_p4(value))).invocations[0].status is ComposerToolStatus.ARG_ERROR
    unsafe = dict(node, node_type="gate")
    with pytest.raises(CredentialMaterialRefused):
        _arg_error_input("upsert_node", node, raw_arguments=canonical_json(unsafe))


@pytest.mark.parametrize("error_code", [None, "ordinary_rejection"])
def test_private_rejection_error_code_is_preserved_and_credential_checked(error_code):
    value, *_ = prepared_input()
    rejections = json.loads(value.rejection_records_json)
    rejections[0]["error_code"] = error_code
    clean = value.model_copy(update={"rejection_records_json": canonical_json(rejections)})
    assert prepared_p4_members(decode_prepared_p4(encode_prepared_p4(clean))).rejection_records[0].error_code == error_code
    rejections[0]["error_code"] = "postgresql://user:password@db.example/data"  # secret-scan: allow-this-line
    dirty = clean.model_copy(update={"rejection_records_json": canonical_json(rejections)})
    with pytest.raises(CredentialMaterialRefused):
        prepared_p4_members(dirty)
    with pytest.raises(CredentialMaterialRefused):
        encode_prepared_p4(dirty)
    with pytest.raises(CredentialMaterialRefused):
        decode_prepared_p4(canonical_json(dirty.model_dump(mode="json")))


@pytest.mark.parametrize("mutation", ["missing-data", "null-data", "failed-data", "missing-both"])
def test_selected_discovery_envelope_is_admitted_before_p4_retention(mutation):
    result = schema_result()
    value, selected, display, _ = actual_result_input(
        "get_plugin_schema", {"plugin_name": "retained_schema", "plugin_type": "transform"}, result
    )
    wire = json.loads(selected)
    positive = _replace_selected_result(value, wire)
    restored = prepared_p4_members(decode_prepared_p4(encode_prepared_p4(positive)))
    assert restored.invocations[0].result_canonical == selected
    assert restored.redacted_rows[0].content == display
    if mutation == "missing-data":
        wire.pop("data")
    elif mutation == "null-data":
        wire["data"] = None
    elif mutation == "failed-data":
        wire["success"] = False
    else:
        wire.pop("data")
        wire.pop("success")
    dirty = _replace_selected_result(value, wire)
    expected = AuditIntegrityError if mutation == "missing-both" else FrameworkBugError
    match = "boolean success" if mutation == "missing-both" else "discovery response"
    with pytest.raises(expected, match=match):
        require_retained_result_credentials("get_plugin_schema", wire, env_ref_names=frozenset())
    with pytest.raises(expected, match=match):
        encode_prepared_p4(dirty)
    with pytest.raises(expected, match=match):
        decode_prepared_p4(canonical_json(dirty.model_dump(mode="json")))


def test_real_discovery_business_refusal_and_redacted_summary_remain_distinct():
    result = ToolResult(False, _empty_state(), ValidationSummary(False, ()), ())
    wire = admit_discovery_result("get_plugin_schema", result).to_dict()
    assert "data" not in wire
    value, selected, _, _ = actual_result_input("get_plugin_schema", {}, result)
    assert prepared_p4_members(decode_prepared_p4(encode_prepared_p4(value))).invocations[0].result_canonical == selected
    summary = {"success": True, "data": "<schema summary>"}
    require_retained_result_credentials("get_plugin_schema", summary, env_ref_names=frozenset(), display=True)
    with pytest.raises(AuditIntegrityError, match="actual producer model"):
        require_retained_result_credentials("get_plugin_schema", summary, env_ref_names=frozenset())
    with pytest.raises(CredentialMaterialRefused):
        require_retained_result_credentials(
            "get_plugin_schema",
            {"success": True, "data": "postgresql://user:password@db.example/data"},  # secret-scan: allow-this-line
            env_ref_names=frozenset(),
            display=True,
        )


def test_actual_arg_error_payload_is_not_promoted_to_discovery_success():
    value = _arg_error_input("get_plugin_schema", {"plugin_type": "transform"})
    restored = prepared_p4_members(decode_prepared_p4(encode_prepared_p4(value)))
    invocation = restored.invocations[0]
    assert invocation.status is ComposerToolStatus.ARG_ERROR
    assert json.loads(invocation.result_canonical) == {"status": "ARG_ERROR", "error": "Argument was refused"}
    # The same bare mapping has no success provenance outside that real status.
    with pytest.raises(AuditIntegrityError, match="boolean success"):
        require_retained_result_credentials("get_plugin_schema", json.loads(invocation.result_canonical), env_ref_names=frozenset())
    unsafe = {"success": False, "data": {"password": "synthetic-private-material"}}
    dirty = _replace_selected_result(value, unsafe)
    with pytest.raises(CredentialMaterialRefused):
        encode_prepared_p4(dirty)


@pytest.mark.parametrize(
    "success,data",
    [
        pytest.param(True, None, id="successful-no-data"),
        pytest.param(False, {}, id="failed-data"),
    ],
)
def test_real_discovery_and_b1_share_the_same_success_data_owner(success, data):
    good = schema_result()
    encoded = encode_tool_result_value("get_plugin_schema", good)
    assert (
        admit_discovery_result("get_plugin_schema", decode_tool_result_value(encoded)).to_dict()
        == admit_discovery_result("get_plugin_schema", good).to_dict()
    )
    bad_result = replace(good, success=success, data=data)
    with pytest.raises(FrameworkBugError, match="discovery response"):
        admit_discovery_result("get_plugin_schema", bad_result)
    dirty = encoded.model_copy(update={"success": success, "data_json": None if data is None else canonical_json(data)})
    with pytest.raises(FrameworkBugError, match="discovery response"):
        decode_tool_result_value(dirty)
