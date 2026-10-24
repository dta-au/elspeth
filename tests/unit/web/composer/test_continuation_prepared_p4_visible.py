"""Historical visible text and complete selected source have distinct owners."""

import json
from hashlib import sha256

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.catalog.schemas import PluginSchemaInfo
from elspeth.web.composer.continuation_prepared_p4 import (
    decode_prepared_p4,
    encode_prepared_p4,
    p4_semantic_input,
    prepared_p4_members,
)
from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.state import ValidationSummary
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.validation import _redact_sensitive_content
from tests.unit.web.composer.test_applied_component_echo import _empty_state, _run
from tests.unit.web.composer.test_continuation_prepared_p4 import actual_result_input, schema_result
from tests.unit.web.composer.test_continuation_prepared_p4_admission import _replace_selected_result

CREDENTIAL_URI = "postgresql://user:password@db.example/data"  # secret-scan: allow-this-line


def _with_visible_event(value, event):
    rows = json.loads(value.redacted_rows_json)
    rows[0]["visible_event_json"] = canonical_json(event.to_envelope())
    encoded = canonical_json(rows)
    assert value.revoked_rows_are_original
    return value.model_copy(update={"redacted_rows_json": encoded, "revoked_rows_json": encoded})


def _with_actual_selected_wire(value, wire):
    members = prepared_p4_members(value)
    row = members.redacted_rows[0]
    invocation = members.invocations[0]
    authoring = members.authorings[0]
    event = VisibleToolEvent.capture(
        context=SessionOperationContext(
            SessionOperationFence("session", "creator", "fixture-live-lease", 7),
            SessionOperationKind.COMPOSE,
        ),
        request_id="actual-request",
        tool_call_id=row.tool_call_id,
        tool_name=invocation.tool_name,
        received_arguments=authoring.raw_arguments,
        effective_arguments=invocation.arguments_canonical,
        selected_result=wire,
        display_arguments=members.redacted_calls[0]["function"]["arguments"],
        display_result=row.content,
    )
    return _with_visible_event(value, event), event


def _roundtrip(value, event):
    encoded = encode_prepared_p4(value)
    decoded = decode_prepared_p4(encoded)
    assert decoded == value
    assert encode_prepared_p4(decoded) == encoded
    members = prepared_p4_members(decoded)
    assert members.redacted_rows[0].visible_event == event
    assert members.redacted_rows[0].visible_event.payload_json == event.payload_json
    assert members.revoked_rows is members.redacted_rows
    assert json.loads(p4_semantic_input(decoded))["prepared"]["redacted_rows_json"] == value.redacted_rows_json
    assert "fixture-live-lease" not in encoded
    return members


def test_actual_visible_hash_redaction_preserves_full_blob_source_and_display():
    content = "customer_key,original label\nCafé,A\n"
    data = {
        "blob_id": "blob",
        "filename": "customer.json",
        "mime_type": "application/json",
        "content": content,
        "truncated": False,
        "size_bytes": len(content.encode()),
        "content_hash": sha256(content.encode()).hexdigest(),
        "total_chars": len(content),
        "start_char": 0,
        "end_char": len(content),
        "next_offset_chars": None,
        "created_by": "user",
        "creation_modality": "verbatim",
    }
    result = ToolResult(True, _empty_state(), ValidationSummary(True, ()), (), data=data)
    value, source, display, _ = actual_result_input("get_blob_content", {"blob_id": "blob"}, result)
    wire = serialize_admitted_discovery_result(admit_discovery_result("get_blob_content", result))
    assert wire != source and json.loads(wire) == json.loads(source)
    changed, event = _with_actual_selected_wire(value, wire)
    visible = json.loads(event.payload_json)["selected_result"]
    assert visible == _redact_sensitive_content(wire)
    assert json.loads(visible)["data"]["content_hash"] != data["content_hash"]
    assert "<redacted-sensitive:phone>" in json.loads(visible)["data"]["content_hash"]
    restored = _roundtrip(changed, event)
    assert restored.invocations[0].result_canonical == source
    assert json.loads(source)["data"]["content"] == content
    assert restored.redacted_rows[0].content == display
    # Two actual serializations of the same complete source yield different
    # retained display evidence. The semantic input binds those exact bytes.
    assert changed.source_audit_json == value.source_audit_json
    assert changed.private_tool_evidence_json == value.private_tool_evidence_json
    assert p4_semantic_input(changed) != p4_semantic_input(value)


def test_actual_visible_numeric_redaction_can_be_non_json_without_losing_schema_source():
    schema = PluginSchemaInfo(
        name="retained_numeric_schema",
        plugin_type="transform",
        description="a numeric default",
        json_schema={"type": "object", "properties": {"sample_count": {"type": "integer", "default": 2025550123}}},
        knob_schema={"fields": []},
    )
    result = ToolResult(True, _empty_state(), ValidationSummary(True, ()), (), data=schema)
    value, source, display, _ = actual_result_input("get_plugin_schema", {}, result)
    wire = serialize_admitted_discovery_result(admit_discovery_result("get_plugin_schema", result))
    changed, event = _with_actual_selected_wire(value, wire)
    visible = json.loads(event.payload_json)["selected_result"]
    assert visible == _redact_sensitive_content(wire)
    assert "<redacted-sensitive:phone>" in visible
    _, error = parse_json_strict(visible)
    assert error is not None
    restored = _roundtrip(changed, event)
    assert restored.invocations[0].result_canonical == source
    assert json.loads(source)["data"]["json_schema"]["properties"]["sample_count"]["default"] == 2025550123
    assert restored.redacted_rows[0].content == display


def test_actual_list_transforms_visible_projection_retains_complete_original_inventory():
    result = _run("list_transforms", {}, _empty_state())
    assert result.success
    value, source, display, _ = actual_result_input("list_transforms", {}, result)
    wire = serialize_admitted_discovery_result(admit_discovery_result("list_transforms", result))
    changed, event = _with_actual_selected_wire(value, wire)
    assert json.loads(event.payload_json)["selected_result"] == _redact_sensitive_content(wire)
    assert json.loads(wire) == json.loads(source)
    restored = _roundtrip(changed, event)
    assert restored.invocations[0].result_canonical == source
    assert restored.redacted_rows[0].content == display


def test_filtered_visible_derivative_never_admits_credential_bearing_complete_source():
    value, source, _, _ = actual_result_input("get_plugin_schema", {}, schema_result())
    members = prepared_p4_members(value)
    _roundtrip(value, members.redacted_rows[0].visible_event)
    wire = json.loads(source)
    wire["data"]["description"] = CREDENTIAL_URI
    dirty = _replace_selected_result(value, wire)
    rows = json.loads(dirty.redacted_rows_json)
    event = VisibleToolEvent.from_envelope(json.loads(rows[0]["visible_event_json"]))
    visible = json.loads(event.payload_json)["selected_result"]
    assert visible is None or CREDENTIAL_URI not in visible
    assert CREDENTIAL_URI in json.loads(dirty.source_audit_json)["invocations"][0]["result_canonical"]
    # _replace_selected_result rebuilds the actual private producer from the
    # changed original; a matching filtered derivative cannot authorize it.
    with pytest.raises(CredentialMaterialRefused):
        prepared_p4_members(dirty)
    with pytest.raises(CredentialMaterialRefused):
        encode_prepared_p4(dirty)
    with pytest.raises(CredentialMaterialRefused):
        decode_prepared_p4(canonical_json(dirty.model_dump(mode="json", warnings="error")))
    with pytest.raises(CredentialMaterialRefused):
        p4_semantic_input(dirty)


def test_retained_visible_text_has_credential_admission_after_actual_historical_link():
    value, *_ = actual_result_input("get_plugin_schema", {}, schema_result())
    members = prepared_p4_members(value)
    event = members.redacted_rows[0].visible_event
    assert event is not None
    _roundtrip(value, event)
    envelope = event.to_envelope()
    body = json.loads(envelope["payload_json"])
    body["selected_result"] = CREDENTIAL_URI
    envelope["payload_json"] = json.dumps(body, ensure_ascii=False)
    envelope["payload_hash"] = sha256(envelope["payload_json"].encode()).hexdigest()
    historical = VisibleToolEvent.from_envelope(envelope)
    historical.validate_recorded_link(
        session_id="session",
        operation_id="creator",
        tool_call_id=event.tool_call_id,
        tool_name=event.tool_name,
        display_arguments=members.redacted_calls[0]["function"]["arguments"],
        display_result=members.redacted_rows[0].content,
    )
    dirty = _with_visible_event(value, historical)
    assert dirty.source_audit_json == value.source_audit_json
    assert dirty.private_tool_evidence_json == value.private_tool_evidence_json
    with pytest.raises(CredentialMaterialRefused):
        prepared_p4_members(dirty)
    with pytest.raises(CredentialMaterialRefused):
        encode_prepared_p4(dirty)
    with pytest.raises(CredentialMaterialRefused):
        decode_prepared_p4(canonical_json(dirty.model_dump(mode="json", warnings="error")))


@pytest.mark.parametrize("field", ["payload_hash", "display_result_hash"])
def test_visible_body_and_display_link_are_still_required(field):
    value, *_ = actual_result_input("get_plugin_schema", {}, schema_result())
    members = prepared_p4_members(value)
    event = members.redacted_rows[0].visible_event
    assert event is not None
    _roundtrip(value, event)
    rows = json.loads(value.redacted_rows_json)
    envelope = event.to_envelope()
    envelope[field] = "0" * 64
    rows[0]["visible_event_json"] = canonical_json(envelope)
    encoded = canonical_json(rows)
    dirty = value.model_copy(update={"redacted_rows_json": encoded, "revoked_rows_json": encoded})
    with pytest.raises(AuditIntegrityError):
        prepared_p4_members(dirty)
