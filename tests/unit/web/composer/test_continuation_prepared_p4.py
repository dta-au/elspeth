"""Pure complete P4 retention, with real evidence owners and no SQL authority."""

import json
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus, PrivateFailureFacts, PrivateToolAuthoring
from elspeth.contracts.composer_mutation import ComposerMutationCeiling, ComposerMutationRevocation
from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.catalog.schemas import PluginSchemaInfo
from elspeth.web.composer.continuation_prepared_p4 import (
    capture_prepared_p4,
    decode_prepared_p4,
    encode_prepared_p4,
    p4_semantic_input,
    prepared_p4_members,
)
from elspeth.web.composer.continuation_result_credentials import require_retained_result_credentials
from elspeth.web.composer.continuation_tool_batch import decode_tool_result_value, encode_tool_result_value
from elspeth.web.composer.diagnostic_evidence import build_private_tool_evidence, diagnostic_evidence_identity
from elspeth.web.composer.discovery_response import admit_discovery_result
from elspeth.web.composer.redaction import redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.source_inspection import facts_to_dict, inspect_blob_content
from elspeth.web.composer.state import ValidationSummary
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.composer.withheld_replies import WithheldReply
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.sessions._persist_payload import RedactedToolRow, RejectionRecord, StatePayload
from elspeth.web.sessions.protocol import CompositionStateData, CompositionValidationError
from tests.unit.web.composer.test_applied_component_echo import _empty_state, _json_output_arguments, _run, _state_with_source

NOW = datetime(2026, 10, 23, 12, 15, tzinfo=UTC)


def prepared_input(*, graph=True, output_url=None, env_ref_names=frozenset(), raw_arguments=None, nodes=()):
    context = SessionOperationContext(SessionOperationFence("session", "creator", "live-fixture-lease", 7), SessionOperationKind.COMPOSE)
    ceiling = ComposerMutationCeiling("session", "logical-request", "auto_commit")
    revocation = ComposerMutationRevocation(ceiling, "explicit_approve")
    calls = []
    rows = []
    invocations = []
    authorings = []
    evidence = []
    failures = []
    for index, tool in enumerate(("set_pipeline", "get_plugin_schema")):
        call_id = f"call-{index}"
        arguments = canonical_json({"reason": "retained authoring"})
        result = canonical_json({"success": index == 0, "detail": "exact result"})
        authoring = PrivateToolAuthoring(
            call_id, tool, arguments, None, raw_arguments if raw_arguments is not None else '{ "reason": "retained authoring" }'
        )
        failure = None if index == 0 else PrivateFailureFacts(canonical_json({"error_class": "ValueError", "detail": "bounded failure"}))
        invocation = ComposerToolInvocation(
            tool_call_id=call_id,
            tool_name=tool,
            arguments_canonical=arguments,
            arguments_hash=sha256(arguments.encode()).hexdigest(),
            result_canonical=result,
            result_hash=sha256(result.encode()).hexdigest(),
            status=ComposerToolStatus.SUCCESS if index == 0 else ComposerToolStatus.PLUGIN_CRASH,
            error_class=None if index == 0 else "ValueError",
            error_message=None if index == 0 else "exact failure",
            version_before=3,
            version_after=4 if index == 0 and graph else 3,
            started_at=NOW,
            finished_at=NOW,
            latency_ms=13,
            actor="composer",
            cache_hit=False,
            strict_sent=True,
            wire_conformant=True,
            private_authoring=authoring,
            private_failure=failure,
        )
        calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": tool, "arguments": arguments},
                "strict_sent": True,
                "wire_conformant": True,
            }
        )
        payload = None
        if index == 0 and graph:
            payload = StatePayload(
                CompositionStateData(
                    sources={},
                    nodes=nodes,
                    edges=(),
                    outputs=()
                    if output_url is None
                    else ({"name": "output", "plugin": "database", "options": {"url": output_url}, "on_write_failure": "discard"},),
                    metadata_={"name": "retained", "description": "café"},
                    is_valid=False,
                    validation_errors=(
                        CompositionValidationError(message="repair needed", error_code="synthetic_code", component="output"),
                    ),
                    composer_meta={"validation_lane": "authoring", "nested": {"ordered": ["b", "a"]}},
                ),
                derived_from_state_id="parent-state",
            )
        visible = VisibleToolEvent.capture(
            context=context,
            request_id="logical-request",
            tool_call_id=call_id,
            tool_name=tool,
            received_arguments=authoring.raw_arguments,
            effective_arguments=arguments,
            selected_result=result,
            display_arguments=arguments,
            display_result=result,
        )
        rows.append(RedactedToolRow(call_id, result, payload, visible))
        invocations.append(invocation)
        authorings.append(authoring)
        evidence.append(
            build_private_tool_evidence(
                session_id="session",
                operation_id="creator",
                operation_epoch=7,
                tool_call_id=call_id,
                tool_name=tool,
                status=invocation.status.value,
                arguments_canonical=arguments,
                result_canonical=result,
                raw_arguments=authoring.raw_arguments,
            )
        )
        if failure is not None:
            failures.append(
                build_private_tool_evidence(
                    session_id="session",
                    operation_id="creator",
                    operation_epoch=7,
                    tool_call_id=call_id,
                    tool_name="composer_failure",
                    status="tool_failure",
                    arguments_canonical=None,
                    result_canonical=failure.canonical,
                    evidence_id=diagnostic_evidence_identity("tool-failure", call_id),
                )
            )
    rows = tuple(rows)
    if graph:
        first = rows[0]
        refused = canonical_json({"success": False, "error": "mutation authority revoked"})
        revoked = (RedactedToolRow(first.tool_call_id, refused, None, first.visible_event.with_display_result(refused)), rows[1])
    else:
        revoked = rows
    value = capture_prepared_p4(
        context=context,
        assistant_content="public prose",
        raw_content="original provider prose",
        redacted_calls=tuple(calls),
        redacted_rows=rows,
        revoked_rows=revoked,
        mutation_ceiling=ceiling,
        authority_revocation=revocation,
        private_tool_evidence=tuple(evidence),
        private_tool_failures=tuple(failures),
        rejection_records=(RejectionRecord("call-1", "get_plugin_schema", "plugin_failure", "exact refusal", rows[1].content),),
        withheld_replies=(WithheldReply("planner_prose_unadmitted", "unpublished model words"),),
        invocations=tuple(invocations),
        authorings=tuple(authorings),
        parent_composition_state_id="parent-state",
        expected_current_state_id="selected-state",
        assistant_row_uses_current_dispatch=False,
        crash_pending=True,
        env_ref_names=env_ref_names,
    )
    return value, rows, revoked, tuple(invocations), tuple(authorings), tuple(evidence), tuple(failures)


def test_complete_p4_preserves_real_rows_refusal_audit_and_private_cohorts():
    value, rows, revoked, invocations, authorings, evidence, failures = prepared_input()
    encoded = encode_prepared_p4(value)
    restored = decode_prepared_p4(encoded)
    members = prepared_p4_members(restored)
    assert restored == value
    assert encode_prepared_p4(restored) == encoded
    assert members.redacted_rows == rows
    assert members.revoked_rows == revoked
    assert members.revoked_rows is not members.redacted_rows
    assert members.invocations == invocations
    assert members.authorings == authorings
    assert members.private_tool_evidence == evidence
    assert members.private_tool_failures == failures
    assert members.authority_revocation == ComposerMutationRevocation(
        ComposerMutationCeiling("session", "logical-request", "auto_commit"), "explicit_approve"
    )
    assert members.redacted_rows[0].composition_state_payload.derived_from_state_id == "parent-state"
    assert members.redacted_rows[0].composition_state_payload.data.composer_meta["nested"]["ordered"] == ("b", "a")
    assert members.withheld_replies == (WithheldReply("planner_prose_unadmitted", "unpublished model words"),)
    assert members.rejection_records[0].planner_payload == rows[1].content
    assert restored.parent_composition_state_id == "parent-state"
    assert restored.expected_current_state_id == "selected-state"
    assert not restored.assistant_row_uses_current_dispatch
    assert restored.crash_pending
    assert "lease" not in encoded
    with pytest.raises(TypeError):
        members.redacted_calls[0]["id"] = "changed"
    with pytest.raises(TypeError):
        members.redacted_rows[0].composition_state_payload.data.composer_meta["nested"]["ordered"] = ()


def test_nongraph_refusal_retains_original_tuple_identity():
    value, *_ = prepared_input(graph=False)
    members = prepared_p4_members(decode_prepared_p4(encode_prepared_p4(value)))
    assert members.revoked_rows is members.redacted_rows
    with pytest.raises(AuditIntegrityError, match="tuple identity"):
        encode_prepared_p4(value.model_copy(update={"revoked_rows_are_original": False}))


@pytest.mark.parametrize("member", ["assistant_content", "raw_content", "parent_composition_state_id", "expected_current_state_id"])
def test_semantic_input_binds_each_consequential_scalar(member):
    value, *_ = prepared_input()
    changed = value.model_copy(update={member: "different retained value"})
    assert p4_semantic_input(changed) != p4_semantic_input(value)


@pytest.mark.parametrize("member", ["assistant_row_uses_current_dispatch", "crash_pending"])
def test_semantic_input_binds_control_flags(member):
    value, *_ = prepared_input()
    assert p4_semantic_input(value.model_copy(update={member: not value.model_dump()[member]})) != p4_semantic_input(value)


@pytest.mark.parametrize("member", ["creator_operation_id", "creator_operation_epoch", "session_id"])
def test_creator_change_cannot_relabel_historical_evidence(member):
    value, *_ = prepared_input()
    replacement = 8 if member == "creator_operation_epoch" else "another-owner"
    with pytest.raises(AuditIntegrityError):
        encode_prepared_p4(value.model_copy(update={member: replacement}))


def test_nested_unknown_and_missing_fields_are_refused_before_normalization():
    value, *_ = prepared_input()
    for mutate in (lambda item: item.update({"unowned": True}), lambda item: item.pop("cache_hit")):
        audit = json.loads(value.source_audit_json)
        mutate(audit["invocations"][0])
        with pytest.raises(AuditIntegrityError, match="lost or normalized"):
            encode_prepared_p4(value.model_copy(update={"source_audit_json": canonical_json(audit)}))
    rows = json.loads(value.redacted_rows_json)
    payload = json.loads(rows[0]["state_payload_json"])
    payload["data"]["unowned"] = "must not disappear"
    rows[0]["state_payload_json"] = canonical_json(payload)
    with pytest.raises(AuditIntegrityError, match="lost or normalized"):
        encode_prepared_p4(value.model_copy(update={"redacted_rows_json": canonical_json(rows)}))


def test_visible_evidence_body_hash_and_recorded_display_link_are_both_required():
    value, *_ = prepared_input()
    rows = json.loads(value.redacted_rows_json)
    envelope = json.loads(rows[0]["visible_event_json"])
    envelope["payload_json"] = canonical_json({"tampered": True})
    rows[0]["visible_event_json"] = canonical_json(envelope)
    with pytest.raises(AuditIntegrityError, match="body binding"):
        encode_prepared_p4(value.model_copy(update={"redacted_rows_json": canonical_json(rows)}))
    rows = json.loads(value.redacted_rows_json)
    rows[0]["content"] = "different display"
    with pytest.raises(AuditIntegrityError, match="recorded display"):
        encode_prepared_p4(value.model_copy(update={"redacted_rows_json": canonical_json(rows)}))


def test_refusal_rows_cannot_retain_graph_authority():
    value, *_ = prepared_input()
    with pytest.raises(AuditIntegrityError, match="kept graph authority"):
        encode_prepared_p4(value.model_copy(update={"revoked_rows_json": value.redacted_rows_json}))


@pytest.mark.parametrize("member", ["private_tool_evidence_json", "private_tool_failures_json", "source_audit_json"])
def test_nonempty_audit_and_private_cohorts_cannot_be_dropped(member):
    value, *_ = prepared_input()
    replacement = canonical_json({"invocations": [], "authorings": []}) if member == "source_audit_json" else "[]"
    with pytest.raises(AuditIntegrityError):
        encode_prepared_p4(value.model_copy(update={member: replacement}))


def test_invocation_hash_and_selected_tool_name_are_rechecked():
    value, *_ = prepared_input()
    for key, replacement in (("arguments_hash", "0" * 64), ("tool_name", "another_tool")):
        audit = json.loads(value.source_audit_json)
        audit["invocations"][0][key] = replacement
        with pytest.raises(AuditIntegrityError):
            encode_prepared_p4(value.model_copy(update={"source_audit_json": canonical_json(audit)}))


def test_call_and_row_cohort_cannot_duplicate_or_drop_members():
    value, *_ = prepared_input()
    calls = json.loads(value.redacted_calls_json)
    for changed in ((calls[0], calls[0]), (calls[0],)):
        with pytest.raises(AuditIntegrityError):
            encode_prepared_p4(value.model_copy(update={"redacted_calls_json": canonical_json(changed)}))


def test_credentials_require_actual_inventory_and_raw_material_remains_refused():
    value, *_ = prepared_input(output_url="${DB_URL}", env_ref_names=frozenset({"DB_URL"}))
    encoded = encode_prepared_p4(value, env_ref_names=frozenset({"DB_URL"}))
    assert decode_prepared_p4(encoded, env_ref_names=frozenset({"DB_URL"})) == value
    with pytest.raises(CredentialMaterialRefused):
        decode_prepared_p4(encoded)
    with pytest.raises(CredentialMaterialRefused):
        prepared_input(
            output_url="postgresql://user:password@db.example/data",  # secret-scan: allow-this-line
            env_ref_names=frozenset({"DB_URL"}),
        )


def test_outer_strict_canonical_shape_and_duplicate_keys_are_required():
    value, *_ = prepared_input()
    encoded = encode_prepared_p4(value)
    with pytest.raises(AuditIntegrityError):
        decode_prepared_p4(encoded + " ")
    with pytest.raises(AuditIntegrityError):
        decode_prepared_p4('{"session_id":"other",' + encoded[1:])
    payload = json.loads(encoded)
    payload["unknown"] = True
    with pytest.raises(AuditIntegrityError):
        decode_prepared_p4(canonical_json(payload))


def test_filtered_private_derivative_does_not_admit_credential_bearing_source():
    # Both diagnostic evidence and visible evidence use their real filtering
    # producers; preserving their filtered outputs cannot excuse this raw wire.
    with pytest.raises(CredentialMaterialRefused):
        prepared_input(raw_arguments=canonical_json({"password": "synthetic-private-material"}))


@pytest.mark.parametrize("member", ["assistant_content", "raw_content", "withheld_replies_json", "source_audit_json", "redacted_rows_json"])
def test_each_retained_text_channel_checks_decoded_credential_fields(member):
    value, *_ = prepared_input()
    unsafe = canonical_json({"password": "synthetic-private-material"})
    if member == "withheld_replies_json":
        payload = json.loads(value.withheld_replies_json)
        payload[0]["content"] = unsafe
        replacement = canonical_json(payload)
    elif member == "source_audit_json":
        payload = json.loads(value.source_audit_json)
        payload["invocations"][0]["error_message"] = unsafe
        replacement = canonical_json(payload)
    elif member == "redacted_rows_json":
        payload = json.loads(value.redacted_rows_json)
        payload[0]["content"] = unsafe
        replacement = canonical_json(payload)
    else:
        replacement = unsafe
    with pytest.raises(CredentialMaterialRefused):
        encode_prepared_p4(value.model_copy(update={member: replacement}))


def test_actual_field_mapper_label_domain_survives_state_payload_admission():
    node = {
        "id": "rename",
        "node_type": "transform",
        "plugin": "field_mapper",
        "input": "rows",
        "on_success": "output",
        "on_error": "discard",
        "options": {"mapping": {"customer_key": "customer_id"}, "schema": {"mode": "observed"}},
    }
    value, *_ = prepared_input(nodes=(node,))
    restored = prepared_p4_members(decode_prepared_p4(encode_prepared_p4(value)))
    assert restored.redacted_rows[0].composition_state_payload.data.nodes[0]["options"]["mapping"]["customer_key"] == "customer_id"
    node["options"]["password"] = "synthetic-private-material"
    with pytest.raises(CredentialMaterialRefused):
        prepared_input(nodes=(node,))


def test_empty_cohort_cannot_falsify_the_original_tuple_alias_fact():
    value, *_ = prepared_input(graph=False)
    empty = value.model_copy(
        update={
            "redacted_calls_json": "[]",
            "redacted_rows_json": "[]",
            "revoked_rows_json": "[]",
            "private_tool_evidence_json": "[]",
            "private_tool_failures_json": "[]",
            "rejection_records_json": "[]",
            "source_audit_json": canonical_json({"invocations": [], "authorings": []}),
        }
    )
    members = prepared_p4_members(decode_prepared_p4(encode_prepared_p4(empty)))
    assert members.redacted_rows is members.revoked_rows
    with pytest.raises(AuditIntegrityError, match="tuple identity"):
        encode_prepared_p4(empty.model_copy(update={"revoked_rows_are_original": False}))


def test_visible_payload_closed_shape_is_required_even_with_a_matching_digest():
    value, *_ = prepared_input(graph=False)
    rows = json.loads(value.redacted_rows_json)
    envelope = json.loads(rows[0]["visible_event_json"])
    payload = json.loads(envelope["payload_json"])
    payload["availability_gaps"] = [3]
    envelope["payload_json"] = canonical_json(payload)
    envelope["payload_hash"] = sha256(envelope["payload_json"].encode()).hexdigest()
    rows[0]["visible_event_json"] = canonical_json(envelope)
    encoded_rows = canonical_json(rows)
    with pytest.raises(AuditIntegrityError, match="content fields"):
        encode_prepared_p4(value.model_copy(update={"redacted_rows_json": encoded_rows, "revoked_rows_json": encoded_rows}))


def actual_result_input(tool, arguments, result, *, env_ref_names=frozenset()):
    """Actual owned result -> selected response -> actual public/private owners."""
    context = SessionOperationContext(SessionOperationFence("session", "creator", "fixture-lease", 7), SessionOperationKind.COMPOSE)
    arguments_json = canonical_json(arguments)
    if tool in {"upsert_node", "set_source", "set_output", "set_metadata"}:
        wire = result.to_dict()
    else:
        wire = admit_discovery_result(tool, result).to_dict()
    selected = canonical_json(wire)
    display = canonical_json(redact_tool_call_response(tool, wire, telemetry=NoopRedactionTelemetry()))
    authoring = PrivateToolAuthoring("actual-call", tool, arguments_json, None, json.dumps(arguments, ensure_ascii=False))
    invocation = ComposerToolInvocation(
        tool_call_id="actual-call",
        tool_name=tool,
        arguments_canonical=arguments_json,
        arguments_hash=sha256(arguments_json.encode()).hexdigest(),
        result_canonical=selected,
        result_hash=sha256(selected.encode()).hexdigest(),
        status=ComposerToolStatus.SUCCESS,
        error_class=None,
        error_message=None,
        version_before=result.updated_state.version,
        version_after=result.updated_state.version,
        started_at=NOW,
        finished_at=NOW,
        latency_ms=11,
        actor="composer",
        strict_sent=True,
        wire_conformant=True,
        private_authoring=authoring,
    )
    event = VisibleToolEvent.capture(
        context=context,
        request_id="actual-request",
        tool_call_id="actual-call",
        tool_name=tool,
        received_arguments=authoring.raw_arguments,
        effective_arguments=arguments_json,
        selected_result=selected,
        display_arguments=arguments_json,
        display_result=display,
    )
    rows = (RedactedToolRow("actual-call", display, None, event),)
    private = build_private_tool_evidence(
        session_id="session",
        operation_id="creator",
        operation_epoch=7,
        tool_call_id="actual-call",
        tool_name=tool,
        status=invocation.status.value,
        arguments_canonical=arguments_json,
        raw_arguments=authoring.raw_arguments,
        result_canonical=selected,
    )
    value = capture_prepared_p4(
        context=context,
        assistant_content="actual retained response",
        raw_content="provider original",
        redacted_calls=(
            {
                "id": "actual-call",
                "type": "function",
                "function": {"name": tool, "arguments": arguments_json},
                "strict_sent": True,
                "wire_conformant": True,
            },
        ),
        redacted_rows=rows,
        revoked_rows=rows,
        mutation_ceiling=None,
        authority_revocation=None,
        private_tool_evidence=(private,),
        private_tool_failures=(),
        rejection_records=(),
        withheld_replies=(),
        invocations=(invocation,),
        authorings=(authoring,),
        parent_composition_state_id=None,
        expected_current_state_id=None,
        assistant_row_uses_current_dispatch=True,
        crash_pending=False,
        env_ref_names=env_ref_names,
    )
    return value, selected, display, result


def schema_result(*, default=None):
    model = PluginSchemaInfo(
        name="retained_schema",
        plugin_type="transform",
        description="literal schema names",
        json_schema={"type": "object", "properties": {"api_key": {"type": "string", "default": default}}},
        knob_schema={"fields": []},
    )
    return ToolResult(True, _empty_state(), ValidationSummary(True, ()), (), data=model)


def test_actual_nonempty_schema_result_restores_b1_nominal_and_complete_p4_channels():
    result = schema_result()
    tool_value = encode_tool_result_value("get_plugin_schema", result)
    restored_result = decode_tool_result_value(tool_value, pre_state=result.updated_state)
    assert type(restored_result.data) is PluginSchemaInfo
    assert restored_result.updated_state is result.updated_state
    assert (
        admit_discovery_result("get_plugin_schema", restored_result).to_dict()
        == admit_discovery_result("get_plugin_schema", result).to_dict()
    )
    value, selected, display, _ = actual_result_input(
        "get_plugin_schema", {"plugin_type": "transform", "plugin_name": "retained_schema"}, result
    )
    restored = prepared_p4_members(decode_prepared_p4(encode_prepared_p4(value)))
    assert restored.invocations[0].result_canonical == selected
    assert restored.redacted_rows[0].content == display
    visible = json.loads(restored.redacted_rows[0].visible_event.payload_json)
    assert json.loads(visible["selected_result"])["data"]["json_schema"]["properties"]["api_key"]["default"] is None
    assert restored.revoked_rows is restored.redacted_rows
    with pytest.raises(CredentialMaterialRefused):
        encode_tool_result_value("get_plugin_schema", schema_result(default="synthetic-opaque-value"))


def test_actual_mutation_echo_and_selected_pipeline_response_keep_field_mapper_labels(tmp_path):
    state = _state_with_source(tmp_path)
    state = _run("set_output", _json_output_arguments("rows_out", tmp_path / "rows.json"), state).updated_state
    node = {
        "id": "rename_customer",
        "node_type": "transform",
        "plugin": "field_mapper",
        "input": "rows",
        "on_success": "rows_out",
        "on_error": "discard",
        "options": {"mapping": {"customer_key": "customer_id"}, "schema": {"mode": "observed"}},
    }
    result = _run("upsert_node", node, state)
    assert result.success and result.applied_component is not None
    value, selected, display, _ = actual_result_input("upsert_node", node, result)
    restored = prepared_p4_members(decode_prepared_p4(encode_prepared_p4(value)))
    assert restored.invocations[0].result_canonical == selected
    assert json.loads(selected)["applied_component"]["nodes"][0]["options"]["mapping"] == {"customer_key": "customer_id"}
    assert restored.redacted_rows[0].content == display
    for component in (None, "rename_customer", "set_pipeline_arguments"):
        args = {} if component is None else {"component": component}
        read = _run("get_pipeline_state", args, result.updated_state)
        assert read.success
        value, selected, _, _ = actual_result_input("get_pipeline_state", args, read)
        assert prepared_p4_members(decode_prepared_p4(encode_prepared_p4(value))).invocations[0].result_canonical == selected
    unsafe = json.loads(selected)
    unsafe["data"]["nodes"][0]["options"]["password"] = "synthetic-private-material"
    with pytest.raises(CredentialMaterialRefused):
        require_retained_result_credentials("get_pipeline_state", unsafe, env_ref_names=frozenset())


def test_raw_customer_blob_body_preserved_and_complete_read_payload_still_scanned():
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
    value, selected, display, _ = actual_result_input("get_blob_content", {"blob_id": "blob"}, result)
    restored = prepared_p4_members(decode_prepared_p4(encode_prepared_p4(value)))
    assert json.loads(restored.invocations[0].result_canonical)["data"]["content"] == content
    assert restored.invocations[0].result_canonical == selected and restored.redacted_rows[0].content == display
    dirty = json.loads(selected)
    dirty["data"]["filename"] = "postgresql://user:password@db.example/data"  # secret-scan: allow-this-line
    with pytest.raises(CredentialMaterialRefused):
        require_retained_result_credentials("get_blob_content", dirty, env_ref_names=frozenset())
    dirty = json.loads(selected)
    content_with_secret = "postgresql://user:password@db.example/data"  # secret-scan: allow-this-line
    dirty["data"].update(content=content_with_secret, total_chars=len(content_with_secret), end_char=len(content_with_secret))
    with pytest.raises(CredentialMaterialRefused):
        require_retained_result_credentials("get_blob_content", dirty, env_ref_names=frozenset())
    dirty = json.loads(selected)
    dirty["data"]["creation_modality"] = "unowned-modality"
    with pytest.raises(FrameworkBugError):
        require_retained_result_credentials("get_blob_content", dirty, env_ref_names=frozenset())


def test_actual_source_inspection_keeps_nonlexical_header_order_and_structural_names():
    facts = inspect_blob_content(content=b"z_name,customer_key,a_name\nZ,C,A\n", filename="customer.csv", mime_type="text/csv")
    result = ToolResult(True, _empty_state(), ValidationSummary(True, ()), (), data=facts_to_dict(facts))
    value, selected, _, _ = actual_result_input("inspect_source", {"blob_id": "blob"}, result)
    restored = prepared_p4_members(decode_prepared_p4(encode_prepared_p4(value)))
    assert restored.invocations[0].result_canonical == selected
    assert json.loads(selected)["data"]["observed_headers"] == ["z_name", "customer_key", "a_name"]
    assert json.loads(selected)["data"]["field_name_mapping"]["customer_key"] == "customer_key"


@pytest.mark.parametrize("tool", ["list_sources", "list_transforms", "list_sinks"])
def test_real_inventory_result_domains_reach_p4_with_complete_defaults_and_labels(tool):
    result = _run(tool, {}, _empty_state())
    assert result.success
    value, selected, _, _ = actual_result_input(tool, {}, result)
    assert prepared_p4_members(decode_prepared_p4(encode_prepared_p4(value))).invocations[0].result_canonical == selected


def test_raw_malformed_arguments_remain_exact_and_not_replaced_with_empty_defaults():
    raw = '{"unfinished":'
    value, *_ = prepared_input(raw_arguments=raw)
    restored = prepared_p4_members(decode_prepared_p4(encode_prepared_p4(value)))
    assert all(item.raw_arguments == raw for item in restored.authorings)


def test_declared_nonheuristic_plugin_secret_slot_rechecks_inventory():
    payload = {
        "success": True,
        "applied_component": {
            "outputs": [
                {"name": "warehouse", "plugin": "database", "options": {"url": "${WAREHOUSE_URL}"}},
            ]
        },
    }
    require_retained_result_credentials("set_output", payload, env_ref_names=frozenset({"WAREHOUSE_URL"}))
    with pytest.raises(CredentialMaterialRefused):
        require_retained_result_credentials("set_output", payload, env_ref_names=frozenset())
    payload["applied_component"]["outputs"][0]["options"]["url"] = "opaque-private-material"
    with pytest.raises(CredentialMaterialRefused):
        require_retained_result_credentials("set_output", payload, env_ref_names=frozenset({"WAREHOUSE_URL"}))


def test_filtered_evidence_must_still_equal_its_complete_actual_source_audit():
    value, *_ = prepared_input()
    envelopes = json.loads(value.private_tool_evidence_json)
    replacement = build_private_tool_evidence(
        session_id="session",
        operation_id="creator",
        operation_epoch=7,
        tool_call_id="call-0",
        tool_name="set_pipeline",
        status=ComposerToolStatus.SUCCESS.value,
        arguments_canonical=canonical_json({"reason": "different retained authoring"}),
        raw_arguments='{ "reason": "retained authoring" }',
        result_canonical=canonical_json({"success": True, "detail": "exact result"}),
    )
    envelopes[0] = replacement.to_envelope()
    with pytest.raises(AuditIntegrityError, match="differs from its actual source audit"):
        encode_prepared_p4(value.model_copy(update={"private_tool_evidence_json": canonical_json(envelopes)}))


def test_rejection_name_and_wire_flags_cannot_cross_the_selected_call():
    value, *_ = prepared_input()
    rejections = json.loads(value.rejection_records_json)
    rejections[0]["tool_name"] = "set_metadata"
    with pytest.raises(AuditIntegrityError, match="rejection names"):
        encode_prepared_p4(value.model_copy(update={"rejection_records_json": canonical_json(rejections)}))
    calls = json.loads(value.redacted_calls_json)
    calls[0]["strict_sent"] = False
    with pytest.raises(AuditIntegrityError, match="wire facts"):
        encode_prepared_p4(value.model_copy(update={"redacted_calls_json": canonical_json(calls)}))
