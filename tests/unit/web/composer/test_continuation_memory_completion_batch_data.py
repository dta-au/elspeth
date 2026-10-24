"""Real completed memory-result graph before application; no current grants."""

import json
from dataclasses import replace

import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.catalog.schemas import PluginSchemaInfo
from elspeth.web.composer import audit as audit_owner
from elspeth.web.composer.audit import finish_success
from elspeth.web.composer.continuation_batch_data import (
    decode_memory_completion_batch_data,
    encode_coupled_batch_data,
    encode_memory_completion_batch_data,
    encode_prepared_batch_data,
    encode_restored_memory_completion_batch_data,
    require_selected_memory_completion,
)
from elspeth.web.composer.continuation_tool_receipts import capture_tool_receipt, encode_retained_tool_receipt
from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult, admit_discovery_result
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolResult, _serialize_full_pipeline_state
from elspeth.web.composer.tools._generation_schema_response import (
    PLUGIN_SCHEMA_RESPONSE_CONTRACT,
    AdmittedPluginSchemaResponse,
    PluginSchemaSnapshot,
)
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer.test_continuation_prepared_batch_data import prepared_cut, schema_cut


def completed_cut(monkeypatch, *, kind="ordinary", changed=False):
    if kind == "schema":
        cut = prepared_cut(monkeypatch, tool="get_plugin_schema", arguments={"plugin_type": "transform", "name": "example"})
    elif kind in {"failed_discovery", "pipeline"}:
        cut = prepared_cut(monkeypatch)
    else:
        cut = prepared_cut(monkeypatch, tool="set_metadata", arguments={"patch": {"name": "after"}})
    batch, acc, calls, dialect = cut
    call = batch.current_call
    # The real begin_dispatch has already recorded its original tick. Give the
    # actual finisher/capture a later monotonic sample; no timer stand-in owner.
    monkeypatch.setattr(audit_owner.time, "monotonic_ns", lambda: 8_000_000)
    state = (
        CompositionState.from_dict({**call.pre_state.to_dict(), "version": 4, "metadata": {"name": "after", "description": "changed"}})
        if changed
        else call.pre_state
    )
    result = ToolResult(
        kind != "failed_discovery",
        state,
        batch.last_validation,
        (),
        prior_validation=batch.last_validation,
        runtime_preflight=batch.last_runtime_preflight,
        post_call_hints=("first", "first", "last"),
    )
    if kind == "schema":
        _schema_batch, _schema_acc, _schema_calls, schema = schema_cut()
        result = admit_discovery_result(call.tool_name, replace(result, data=schema))
    elif kind == "failed_discovery":
        result = admit_discovery_result(call.tool_name, result)
    elif kind == "pipeline":
        result = admit_discovery_result(
            call.tool_name, replace(result, data=_serialize_full_pipeline_state(state, requested_component=None))
        )
    invocation = finish_success(call.audit, result_payload=result.to_dict(), version_after=state.version)
    invocation = replace(invocation, private_authoring=batch.private_authorings_by_call[call.tool_call.id])
    receipt = capture_tool_receipt(effect_key=f"memory:{call.tool_call.id}", invocation=invocation, result=result, pre_state=call.pre_state)
    call.memory_result = result
    call.owner_tool_receipt_json = encode_retained_tool_receipt(receipt)
    call.phase = "application_pending"
    return batch, acc, calls, dialect, invocation


def capture(cut):
    batch, acc, calls, dialect, _invocation = cut
    return encode_memory_completion_batch_data(batch=batch, accumulator=acc, calls=calls, dialect=dialect)


@pytest.mark.parametrize("changed", [False, True])
def test_completed_memory_cut_keeps_all_actual_roots_receipt_and_completed_timing(monkeypatch, changed):
    cut = completed_cut(monkeypatch, changed=changed)
    batch, acc, _calls, dialect, invocation = cut
    live_audit = batch.current_call.audit
    encoded = capture(cut)
    restored = decode_memory_completion_batch_data(encoded)
    call = restored.batch.current_call
    assert call.audit is None and batch.current_call.audit is live_audit
    assert live_audit.started_ns == 1_000_000
    assert call.phase == "application_pending" and restored.batch.next_tool_index == 2
    assert len(restored.batch.tool_outcomes) == 2
    assert call.tool_call is restored.calls[2]
    assert call.pre_state is restored.batch.state is restored.accumulator.state is restored.batch.entry_state
    assert call.arguments is call.audit_arguments is restored.batch.decoded_args_by_call_id[call.tool_call.id]
    assert (call.memory_result.updated_state is call.pre_state) is (not changed)
    assert call.memory_result.validation is call.memory_result.prior_validation is restored.batch.last_validation
    assert call.memory_result.runtime_preflight is restored.batch.last_runtime_preflight is restored.accumulator.last_runtime_preflight
    assert call.memory_result.post_call_hints == ("first", "first", "last")
    assert restored.batch.tool_outcomes[0].response is restored.batch.tool_outcomes[1].response
    assert restored.accumulator.authority_revocation == acc.authority_revocation
    assert (restored.accumulator.advisor_calls_used, restored.batch.advisor_calls_used) == (2, 4)
    assert call.owner_tool_receipt_json == batch.current_call.owner_tool_receipt_json
    assert encode_restored_memory_completion_batch_data(restored) == encoded
    selected = require_selected_memory_completion(
        restored, selected_call=call.tool_call, selected_pre_state=call.pre_state, current_dialect=dialect, env_ref_names=frozenset()
    )
    assert selected == invocation
    assert selected.latency_ms == invocation.latency_ms == 7
    assert selected.started_at == live_audit.started_at and selected.finished_at == invocation.finished_at
    assert call.audit is None  # No timer or current authority is manufactured.


def test_actual_admitted_schema_wrapper_snapshot_and_mutable_result_aliases_survive(monkeypatch):
    cut = completed_cut(monkeypatch, kind="schema")
    original = cut[0].current_call.memory_result
    assert type(original) is AdmittedDiscoveryResult and type(original.result.data) is PluginSchemaInfo
    restored = decode_memory_completion_batch_data(capture(cut))
    value = restored.batch.current_call.memory_result
    assert type(value) is AdmittedDiscoveryResult
    assert type(value.response) is AdmittedPluginSchemaResponse
    assert type(value.response.snapshot) is PluginSchemaSnapshot
    assert value.contract is value.response.contract is PLUGIN_SCHEMA_RESPONSE_CONTRACT
    assert value.response.snapshot is not original.response.snapshot
    assert value.to_dict() == original.to_dict()
    assert value.response.readmit(value.contract).to_wire() == original.response.to_wire()
    assert type(value.result.data) is PluginSchemaInfo
    left = value.result.data.json_schema["properties"]["items"]["default"]
    right = value.result.data.knob_schema["fields"][0]["default"]
    assert left is right
    assert value.result.updated_state is restored.batch.state
    before = value.response.to_wire()
    left.append("consequential")
    assert right[-1] == "consequential"
    assert value.response.to_wire() == before  # Original admitted snapshot is separately owned.
    assert original.result.data.json_schema["properties"]["items"]["default"] == ["ordinary", "ordinary"]
    with pytest.raises(AuditIntegrityError):
        encode_restored_memory_completion_batch_data(restored)


def test_failed_admitted_discovery_preserves_no_payload_and_complete_nominal_result(monkeypatch):
    cut = completed_cut(monkeypatch, kind="failed_discovery")
    restored = decode_memory_completion_batch_data(capture(cut))
    value = restored.batch.current_call.memory_result
    assert type(value) is AdmittedDiscoveryResult and value.response is None
    assert value.result.success is False and value.result.data is None
    assert value.result.updated_state is restored.batch.state
    call = restored.batch.current_call
    assert (
        require_selected_memory_completion(
            restored,
            selected_call=call.tool_call,
            selected_pre_state=call.pre_state,
            current_dialect=restored.dialect,
            env_ref_names=frozenset(),
        )
        == cut[4]
    )


@pytest.mark.parametrize(
    "changed", ["effect", "receipt_result", "audit_actor", "authoring", "receipt_authoring", "phase", "owner_result", "advisor", "prefix"]
)
def test_memory_cut_refuses_independent_actual_binding_or_phase_changes(monkeypatch, changed):
    cut = completed_cut(monkeypatch)
    assert decode_memory_completion_batch_data(capture(cut)).batch.current_call.phase == "application_pending"
    batch = cut[0]
    call = batch.current_call
    if changed == "effect":
        receipt = json.loads(call.owner_tool_receipt_json)
        receipt["effect_key"] = "memory:another-call"
        call.owner_tool_receipt_json = canonical_json(receipt)
    elif changed == "receipt_result":
        call.memory_result = replace(call.memory_result, success=False)
    elif changed == "audit_actor":
        call.audit = replace(call.audit, actor="another-current-actor")
    elif changed == "authoring":
        original = batch.private_authorings_by_call[call.tool_call.id]
        # The actual authoring owner permits absent private semantic text; the
        # prepared join still requires the retained semantic/canonical facts.
        batch.private_authorings_by_call[call.tool_call.id] = replace(original, arguments_canonical=None)
    elif changed == "receipt_authoring":
        receipt = json.loads(call.owner_tool_receipt_json)
        invocation = json.loads(receipt["invocation_json"])
        invocation["private_authoring"]["arguments_canonical"] = None
        receipt["invocation_json"] = canonical_json(invocation)
        call.owner_tool_receipt_json = canonical_json(receipt)
    elif changed == "phase":
        call.phase = "applied"
    elif changed == "owner_result":
        call.owner_result_json = "{}"
    elif changed == "advisor":
        call.advisor_invocation_json = "{}"
    else:
        batch.next_tool_index += 1
    with pytest.raises(AuditIntegrityError):
        capture(cut)


@pytest.mark.parametrize("changed", ["call", "pre_state", "dialect"])
def test_memory_application_selection_requires_exact_retained_roots(monkeypatch, changed):
    restored = decode_memory_completion_batch_data(capture(completed_cut(monkeypatch)))
    call = restored.batch.current_call
    selected = replace(call.tool_call) if changed == "call" else call.tool_call
    state = CompositionState.from_dict(call.pre_state.to_dict()) if changed == "pre_state" else call.pre_state
    dialect = ToolContractDialect.OPENAI_STRICT if changed == "dialect" else restored.dialect
    with pytest.raises(AuditIntegrityError, match="exact selected DATA roots"):
        require_selected_memory_completion(
            restored, selected_call=selected, selected_pre_state=state, current_dialect=dialect, env_ref_names=frozenset()
        )


def test_other_real_successful_wrapper_is_refused_before_any_application(monkeypatch):
    cut = completed_cut(monkeypatch, kind="pipeline")
    assert cut[0].current_call.memory_result.response.readmit(cut[0].current_call.memory_result.contract).to_wire()
    with pytest.raises(AuditIntegrityError, match="own complete graph owner"):
        capture(cut)
    assert cut[0].next_tool_index == 2 and len(cut[0].tool_outcomes) == 2


def test_existing_prepared_and_quiescent_entrypoints_still_refuse_completed_cuts(monkeypatch):
    cut = completed_cut(monkeypatch)
    batch, acc, calls, dialect, _invocation = cut
    with pytest.raises(AuditIntegrityError):
        encode_prepared_batch_data(batch=batch, accumulator=acc, calls=calls, dialect=dialect)
    with pytest.raises(AuditIntegrityError):
        encode_coupled_batch_data(batch=batch, accumulator=acc, calls=calls)


def test_decoder_rechecks_original_actor_and_complete_graph(monkeypatch):
    normal = capture(completed_cut(monkeypatch))
    payload = json.loads(normal)
    audit_node = next(node for node in payload["nodes"] if node["kind"] == "portable_dispatch_audit")
    audit = json.loads(audit_node["value"])
    audit["actor"] = "another-current-actor"
    audit_node["value"] = canonical_json(audit)
    with pytest.raises(AuditIntegrityError, match="retained audit binding"):
        decode_memory_completion_batch_data(canonical_json(payload))


def test_schema_mutation_credentials_are_not_waived_by_admitted_snapshot(monkeypatch):
    cut = completed_cut(monkeypatch, kind="schema")
    assert decode_memory_completion_batch_data(capture(cut)).batch.current_call.memory_result.result.success
    cut[0].current_call.memory_result.result.data.json_schema["properties"]["api_key"]["default"] = "opaque-private-value"
    with pytest.raises((CredentialMaterialRefused, AuditIntegrityError)):
        capture(cut)


def test_schema_snapshot_constructor_normalization_is_refused_not_silently_repartitioned(monkeypatch):
    normal = capture(completed_cut(monkeypatch, kind="schema"))
    assert encode_restored_memory_completion_batch_data(decode_memory_completion_batch_data(normal)) == normal
    payload = json.loads(normal)
    snapshot = next(node for node in payload["nodes"] if node["kind"] == "JSONSchemaSnapshot")
    keywords_ref = dict(snapshot["value"])["keywords"]
    keywords = payload["nodes"][keywords_ref["ref"]]
    assert keywords["kind"] == "mapping"
    keywords["kind"] = "dict"
    with pytest.raises(AuditIntegrityError, match="alias partition"):
        decode_memory_completion_batch_data(canonical_json(payload))
