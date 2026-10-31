"""Real prepared-call DATA and nominal catalogue graph; no execution authority."""

import json
from dataclasses import replace

import pytest

from elspeth.contracts.composer_audit import PrivateToolAuthoring
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.enums import AuditCharacteristic
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, PluginCapability, WebConfigAuthority
from elspeth.web.catalog.schemas import ConfigFieldSummary, PluginSchemaInfo, PluginSecretRequirement, PluginSummary
from elspeth.web.composer import audit as audit_owner
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _AdmittedToolFunction
from elspeth.web.composer.audit import begin_dispatch, finish_success
from elspeth.web.composer.authority_hashing import composer_authority_canonical_json
from elspeth.web.composer.continuation_batch_data import (
    decode_coupled_batch_data,
    decode_prepared_batch_data,
    encode_coupled_batch_data,
    encode_prepared_batch_data,
    encode_restored_prepared_batch_data,
    readmit_prepared_batch_audit,
)
from elspeth.web.composer.discovery_response import admit_discovery_result
from elspeth.web.composer.pipeline_custody import inline_custody_audit_projection
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tool_batch import ContinuationCallState, _CallWireFacts
from elspeth.web.composer.tools.wire_projection import _WIRE_TOOL_DEFS, decode_wire_arguments, encode_semantic_arguments
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer.test_continuation_batch_data import actual_cut


def prepared_cut(monkeypatch, *, tool="get_pipeline_state", arguments=None, dialect=ToolContractDialect.NONE):
    ticks = iter([1_000_000, 4_000_000, 100, 2_000_100])
    monkeypatch.setattr(audit_owner.time, "monotonic_ns", lambda: next(ticks))
    batch, acc, calls = actual_cut()
    semantic = {"component": "summary"} if arguments is None else arguments
    raw = encode_semantic_arguments(tool, dialect, semantic)
    selected = _AdmittedToolCall("call-3", _AdmittedToolFunction(tool, json.dumps(raw)))
    calls = (*calls[:2], selected)
    decoded = decode_wire_arguments(tool, dialect, raw)
    semantic = deep_thaw(decoded.semantic)
    audit_arguments = dict(inline_custody_audit_projection(semantic)) if tool == "set_pipeline" else semantic
    strict = _WIRE_TOOL_DEFS[dialect][tool].strict_capable if dialect is ToolContractDialect.OPENAI_STRICT else None
    wire = _CallWireFacts(strict, decoded.wire_conformant)
    audit = begin_dispatch(
        selected.id,
        tool,
        audit_arguments,
        version_before=batch.state.version,
        actor="composer-web:user:alice",
        strict_sent=wire.strict_sent,
        wire_conformant=wire.wire_conformant,
    )
    private = canonical_json(semantic)
    authority = composer_authority_canonical_json(semantic) if tool == "set_pipeline" else None
    call = ContinuationCallState(
        2,
        selected,
        batch.state,
        batch.state.version,
        phase="prepared",
        tool_name=tool,
        private_raw_arguments=selected.function.arguments,
        private_arguments_canonical=private,
        private_authority_canonical=authority,
        wire=wire,
        arguments=semantic,
        audit_arguments=audit_arguments,
        audit=audit,
        original_creator_id="old-creator",
        original_creator_epoch=4,
    )
    batch.current_call = call
    batch.phase = "call_active"
    batch.stop_issued_batch = False
    batch.decoded_args_by_call_id[selected.id] = audit_arguments
    batch.private_authorings_by_call[selected.id] = PrivateToolAuthoring(selected.id, tool, private, authority, selected.function.arguments)
    batch.promises_by_call[selected.id] = frozenset()
    return batch, acc, calls, dialect


def capture(cut):
    batch, acc, calls, dialect = cut
    return encode_prepared_batch_data(batch=batch, accumulator=acc, calls=calls, dialect=dialect)


@pytest.mark.parametrize("dialect", (ToolContractDialect.NONE, ToolContractDialect.OPENAI_STRICT))
def test_prepared_graph_keeps_real_call_aliases_revocation_and_portable_audit_separate(monkeypatch, dialect):
    cut = prepared_cut(monkeypatch, dialect=dialect)
    batch, acc, _calls, _dialect = cut
    original = batch.current_call.audit
    encoded = capture(cut)
    restored = decode_prepared_batch_data(encoded)
    call = restored.batch.current_call
    assert call.audit is None
    assert restored.audit.elapsed_active_ns == 3_000_000
    assert restored.audit.started_at == original.started_at
    assert call.tool_call is restored.calls[2]
    assert call.pre_state is restored.batch.state is restored.accumulator.state is restored.batch.entry_state
    assert call.arguments is call.audit_arguments is restored.batch.decoded_args_by_call_id[call.tool_call.id]
    assert restored.batch.tool_outcomes[0].response is restored.batch.tool_outcomes[1].response
    assert restored.batch.tool_outcomes[0].response.updated_state is call.pre_state
    assert restored.accumulator.authority_revocation == acc.authority_revocation
    assert restored.accumulator.advisor_calls_used == 2 and restored.batch.advisor_calls_used == 4
    assert call.original_creator_id == "old-creator" and call.original_creator_epoch == 4
    assert encode_restored_prepared_batch_data(restored) == encoded
    rebound = readmit_prepared_batch_audit(
        restored,
        selected_call=restored.calls[2],
        selected_pre_state=call.pre_state,
        current_dialect=dialect,
        actor="composer-web:user:alice",
        strict_sent=call.wire.strict_sent,
        wire_conformant=call.wire.wire_conformant,
    )
    assert rebound.started_ns == 100 - 3_000_000
    assert call.audit is None  # No implicit attachment/current execution grant.
    assert batch.current_call.audit is original and original.started_ns == 1_000_000
    invocation = finish_success(rebound, result_payload={"status": "complete"}, version_after=3)
    assert invocation.latency_ms == 5
    assert invocation.started_at == original.started_at


def test_manual_advisor_prepared_call_uses_actual_wire_owner(monkeypatch):
    cut = prepared_cut(monkeypatch, tool="request_advisor_hint", arguments={"trigger": "manual", "question": "Review this draft"})
    restored = decode_prepared_batch_data(capture(cut))
    call = restored.batch.current_call
    assert call.tool_name == "request_advisor_hint"
    assert call.arguments["question"] == "Review this draft"
    assert call.audit is None and restored.audit.tool_call_id == call.tool_call.id


def test_set_pipeline_keeps_raw_envelope_order_and_separate_custody_projection(monkeypatch):
    arguments = {"sources": {"z_source": {}, "a_source": {}}, "nodes": [], "outputs": []}
    cut = prepared_cut(monkeypatch, tool="set_pipeline", arguments=arguments)
    restored = decode_prepared_batch_data(capture(cut))
    call = restored.batch.current_call
    assert "pipeline" in json.loads(call.private_raw_arguments)
    assert tuple(call.arguments["sources"]) == ("z_source", "a_source")
    assert call.audit_arguments is not call.arguments
    assert call.audit_arguments is restored.batch.decoded_args_by_call_id[call.tool_call.id]
    assert call.private_authority_canonical == composer_authority_canonical_json(call.arguments)
    assert restored.audit.authority_arguments_canonical == composer_authority_canonical_json(call.audit_arguments)


@pytest.mark.parametrize("field", ("memory_result", "owner_result_json", "owner_tool_receipt_json", "completion_json", "effect_key"))
def test_later_owner_fields_are_refused_without_silent_defaulting(monkeypatch, field):
    cut = prepared_cut(monkeypatch)
    batch, _acc, _calls, _dialect = cut
    value = batch.tool_outcomes[0].response if field == "memory_result" else "retained-later-owner"
    batch.current_call = replace(batch.current_call, **{field: value})
    with pytest.raises(AuditIntegrityError, match="later owner result or receipt"):
        capture(cut)


@pytest.mark.parametrize("change", ("raw", "semantic", "pre_state", "argument_alias", "private", "wire", "creator"))
def test_prepared_capture_refuses_each_broken_actual_join(monkeypatch, change):
    cut = prepared_cut(monkeypatch)
    batch, _acc, _calls, _dialect = cut
    call = batch.current_call
    if change == "raw":
        call.private_raw_arguments = '{"component":"nodes"}'
    elif change == "semantic":
        call.arguments["component"] = "nodes"
    elif change == "pre_state":
        call.pre_state = CompositionState.from_dict(call.pre_state.to_dict())
    elif change == "argument_alias":
        call.audit_arguments = dict(call.arguments)
    elif change == "private":
        call.private_arguments_canonical = "{}"
    elif change == "wire":
        call.wire.wire_conformant = not call.wire.wire_conformant
    else:
        call.original_creator_epoch = None
    with pytest.raises(AuditIntegrityError):
        capture(cut)


@pytest.mark.parametrize("change", ("call", "pre_state", "dialect", "actor", "wire_kind"))
def test_explicit_audit_seam_requires_same_selected_roots_and_fresh_binding(monkeypatch, change):
    restored = decode_prepared_batch_data(capture(prepared_cut(monkeypatch)))
    call = restored.batch.current_call
    kwargs = {
        "selected_call": call.tool_call,
        "selected_pre_state": call.pre_state,
        "current_dialect": restored.dialect,
        "actor": "composer-web:user:alice",
        "strict_sent": call.wire.strict_sent,
        "wire_conformant": call.wire.wire_conformant,
    }
    if change == "call":
        kwargs["selected_call"] = replace(call.tool_call)
    elif change == "pre_state":
        kwargs["selected_pre_state"] = CompositionState.from_dict(call.pre_state.to_dict())
    elif change == "dialect":
        kwargs["current_dialect"] = ToolContractDialect.OPENAI_STRICT
    elif change == "wire_kind":
        kwargs["wire_conformant"] = int(call.wire.wire_conformant)
    else:
        kwargs["actor"] = "composer-web:user:bob"
    with pytest.raises(AuditIntegrityError):
        readmit_prepared_batch_audit(restored, **kwargs)
    assert call.audit is None


def test_original_live_exception_and_cause_stay_original_when_capture_refuses(monkeypatch):
    cut = prepared_cut(monkeypatch)
    batch, _acc, _calls, _dialect = cut
    failure, cause = AssertionError("live failure"), ValueError("live cause")
    failure.__cause__ = cause
    batch.advisor_failure = failure
    with pytest.raises(AuditIntegrityError, match="live exception"):
        capture(cut)
    assert batch.advisor_failure is failure and batch.advisor_failure.__cause__ is cause


def schema_cut():
    batch, acc, old_calls = actual_cut()
    shared = ["ordinary", "ordinary"]
    schema = PluginSchemaInfo(
        name="example",
        plugin_type="transform",
        description="Retained schema",
        json_schema={"type": "object", "properties": {"api_key": {"type": "string"}, "items": {"type": "array", "default": shared}}},
        knob_schema={"fields": [{"name": "items", "kind": "text", "required": False, "default": shared}]},
        composer_hints=("retain", "retain"),
        secret_requirements=(PluginSecretRequirement(field="api_key", candidates=("REMOTE_KEY",)),),
        web_config_authority=WebConfigAuthority.USER_CONFIGURABLE_WITH_POLICY,
        policy_capabilities=(CapabilityDeclaration(PluginCapability.LLM),),
        aggregation_output_modes=("transform",),
    )
    assert schema.json_schema["properties"]["items"]["default"] is schema.knob_schema["fields"][0]["default"]
    calls = (
        *tuple(
            _AdmittedToolCall(call.id, _AdmittedToolFunction("get_plugin_schema", '{"plugin_type":"transform","name":"example"}'))
            for call in old_calls[:2]
        ),
        old_calls[2],
    )
    result = replace(batch.tool_outcomes[0].response, success=True, data=schema)
    batch.tool_outcomes = [
        replace(outcome, call=call, response=result) for outcome, call in zip(batch.tool_outcomes, calls[:2], strict=True)
    ]
    batch.private_authorings_by_call = {
        call.id: PrivateToolAuthoring(call.id, call.function.name, call.function.arguments, None, call.function.arguments)
        for call in calls[:2]
    }
    batch.decoded_args_by_call_id = {call.id: {"plugin_type": "transform", "name": "example"} for call in calls[:2]}
    return batch, acc, calls, schema


def test_actual_plugin_schema_info_remains_nominal_and_shared_children_remain_mutable_aliases():
    batch, acc, calls, schema = schema_cut()
    selected = admit_discovery_result("get_plugin_schema", batch.tool_outcomes[0].response)
    encoded = encode_coupled_batch_data(batch=batch, accumulator=acc, calls=calls)
    restored = decode_coupled_batch_data(encoded)
    first, second = restored.batch.tool_outcomes
    assert first.response is second.response
    retained = first.response.data
    assert type(retained) is PluginSchemaInfo
    assert retained is second.response.data and retained is not schema
    assert retained.model_dump(mode="json") == schema.model_dump(mode="json")
    assert canonical_json(admit_discovery_result("get_plugin_schema", first.response).to_dict()) == canonical_json(selected.to_dict())
    left = retained.json_schema["properties"]["items"]["default"]
    right = retained.knob_schema["fields"][0]["default"]
    assert left is right
    left.append("consequential")
    assert right[-1] == "consequential" and schema.json_schema["properties"]["items"]["default"] == ["ordinary", "ordinary"]
    assert retained.secret_requirements[0].field == "api_key"
    assert retained.policy_capabilities[0].capability is PluginCapability.LLM


def test_actual_schema_default_credentials_refuse_without_refusing_property_labels():
    batch, acc, calls, schema = schema_cut()
    schema.json_schema["properties"]["api_key"]["default"] = "opaque-private-value"
    with pytest.raises(CredentialMaterialRefused):
        encode_coupled_batch_data(batch=batch, accumulator=acc, calls=calls)


def test_actual_plugin_summary_children_and_enums_keep_complete_nominal_identity():
    batch, acc, calls = actual_cut()
    field = ConfigFieldSummary(name="path", type="str", required=False, description="Input", default=["first", "first"])
    secret = PluginSecretRequirement(field="api_key", candidates=("REMOTE_KEY",))
    summary = PluginSummary(
        name="csv",
        description="CSV source",
        plugin_type="source",
        config_fields=[field, field],
        usage_when_to_use="Rows",
        usage_when_not_to_use="Other data",
        example_use="Read rows",
        capability_tags=("source",),
        web_config_authority=WebConfigAuthority.USER_CONFIGURABLE,
        policy_capabilities=(CapabilityDeclaration(PluginCapability.LLM),),
        audit_characteristics=(AuditCharacteristic.IO_READ,),
        composer_hints=("first", "first"),
        secret_requirements=(secret, secret),
    )
    selected_calls = (*tuple(_AdmittedToolCall(call.id, _AdmittedToolFunction("list_sources", "{}")) for call in calls[:2]), calls[2])
    result = replace(batch.tool_outcomes[0].response, success=True, data={"available": [summary, summary], "prohibited": []})
    batch.tool_outcomes = [
        replace(outcome, call=call, response=result) for outcome, call in zip(batch.tool_outcomes, selected_calls[:2], strict=True)
    ]
    batch.private_authorings_by_call = {
        call.id: PrivateToolAuthoring(call.id, call.function.name, "{}", None, "{}") for call in selected_calls[:2]
    }
    batch.decoded_args_by_call_id = {call.id: {} for call in selected_calls[:2]}
    restored = decode_coupled_batch_data(encode_coupled_batch_data(batch=batch, accumulator=acc, calls=selected_calls))
    data = restored.batch.tool_outcomes[0].response.data
    first, second = data["available"]
    assert type(first) is PluginSummary and first is second
    assert first.model_dump(mode="json") == summary.model_dump(mode="json")
    assert first.config_fields[0] is first.config_fields[1]
    assert first.secret_requirements[0] is first.secret_requirements[1]
    assert first.audit_characteristics == (AuditCharacteristic.IO_READ,)
    assert first.policy_capabilities[0].capability is PluginCapability.LLM
    first.config_fields[0].default.append("later")
    assert first.config_fields[1].default[-1] == "later" and field.default == ["first", "first"]


def test_strict_promoted_nulls_are_redecoded_without_confusing_raw_and_semantic(monkeypatch):
    cut = prepared_cut(
        monkeypatch, tool="list_models", arguments={"provider": None, "limit": None}, dialect=ToolContractDialect.OPENAI_STRICT
    )
    restored = decode_prepared_batch_data(capture(cut))
    call = restored.batch.current_call
    assert json.loads(call.private_raw_arguments) == {"provider": None, "limit": None}
    assert call.arguments == {} and call.private_arguments_canonical == "{}"
    assert call.arguments is call.audit_arguments


def test_equal_distinct_semantic_audit_maps_are_not_the_actual_prepared_alias(monkeypatch):
    cut = prepared_cut(monkeypatch)
    batch, _acc, _calls, _dialect = cut
    call = batch.current_call
    call.audit_arguments = dict(call.arguments)
    batch.decoded_args_by_call_id[call.tool_call.id] = call.audit_arguments
    with pytest.raises(AuditIntegrityError, match="semantic/audit argument alias"):
        capture(cut)


@pytest.mark.parametrize("change", ("missing_dialect", "missing_member", "wrong_audit_call", "unknown_member"))
def test_prepared_decoder_refuses_complete_graph_and_audit_join_corruption(monkeypatch, change):
    value = json.loads(capture(prepared_cut(monkeypatch)))
    if change == "missing_dialect":
        value.pop("dialect")
    elif change == "wrong_audit_call":
        audit_node = next(node for node in value["nodes"] if node["kind"] == "portable_dispatch_audit")
        audit = json.loads(audit_node["value"])
        audit["tool_call_id"] = "another-call"
        audit_node["value"] = canonical_json(audit)
    else:
        call_node = next(node for node in value["nodes"] if node["kind"] == "ContinuationCallState")
        if change == "missing_member":
            call_node["value"] = [pair for pair in call_node["value"] if pair[0] != "original_creator_epoch"]
        else:
            call_node["value"].append(["invented", {"scalar": None}])
    with pytest.raises(AuditIntegrityError):
        decode_prepared_batch_data(canonical_json(value))


def test_prepared_cut_retains_absent_legacy_creator_pair_without_manufacturing_identity(monkeypatch):
    cut = prepared_cut(monkeypatch)
    cut[0].current_call.original_creator_id = None
    cut[0].current_call.original_creator_epoch = None
    restored = decode_prepared_batch_data(capture(cut))
    assert restored.batch.current_call.original_creator_id is None
    assert restored.batch.current_call.original_creator_epoch is None
    with pytest.raises(AuditIntegrityError, match="current call/timing"):
        encode_coupled_batch_data(batch=restored.batch, accumulator=restored.accumulator, calls=restored.calls)


def test_distinct_actual_schema_models_preserve_shared_top_level_mutable_schema():
    batch, acc, calls, first = schema_cut()
    second = first.model_copy(update={"description": "Second actual model"})
    assert second is not first and second.json_schema is first.json_schema
    assert second.knob_schema is first.knob_schema
    batch.tool_outcomes[1] = replace(batch.tool_outcomes[1], response=replace(batch.tool_outcomes[1].response, data=second))
    encoded = encode_coupled_batch_data(batch=batch, accumulator=acc, calls=calls)
    restored = decode_coupled_batch_data(encoded)
    left = restored.batch.tool_outcomes[0].response.data
    right = restored.batch.tool_outcomes[1].response.data
    assert type(left) is type(right) is PluginSchemaInfo
    assert left is not right and left.description != right.description
    assert left.json_schema is right.json_schema and left.knob_schema is right.knob_schema
    left.json_schema["description"] = "shared mutation"
    assert right.json_schema["description"] == "shared mutation"
    assert "description" not in first.json_schema


def test_schema_credential_admission_also_runs_on_valid_complete_graph_decode():
    batch, acc, calls, _schema = schema_cut()
    value = json.loads(encode_coupled_batch_data(batch=batch, accumulator=acc, calls=calls))
    schema_node = next(node for node in value["nodes"] if node["kind"] == "PluginSchemaInfo")
    members = dict(schema_node["value"])
    schema_object = value["nodes"][members["json_schema"]["ref"]]
    properties_ref = dict(schema_object["value"])["properties"]
    properties = value["nodes"][properties_ref["ref"]]
    api_key_ref = dict(properties["value"])["api_key"]
    api_key = value["nodes"][api_key_ref["ref"]]
    assert api_key["kind"] == "dict"
    api_key["value"].append(["default", {"scalar": "opaque-private-value"}])
    with pytest.raises(CredentialMaterialRefused):
        decode_coupled_batch_data(canonical_json(value))
