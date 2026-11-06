"""Ownership-copy controls using actual cold batch/evidence admissions."""

from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType

import pytest

from elspeth.contracts.freeze import FrozenJsonArray
from elspeth.contracts.provider_rejection import SafeRequestRejection
from elspeth.web.catalog.schemas import PluginSchemaInfo
from elspeth.web.composer import continuation_application_graph as graph_codec
from elspeth.web.composer.continuation_application_copy import copy_application_graph
from elspeth.web.composer.continuation_application_graph import ApplicationGraph
from elspeth.web.composer.continuation_batch_data import (
    RestoredBatchData,
    decode_memory_completion_batch_data,
    decode_prepared_batch_data,
    encode_restored_memory_completion_batch_data,
)
from elspeth.web.composer.continuation_evidence_application_data import build_portable_p4_application
from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult
from elspeth.web.composer.state import CompositionState, ValidationSummary
from elspeth.web.composer.tools._generation_schema_response import PLUGIN_SCHEMA_RESPONSE_CONTRACT, AdmittedPluginSchemaResponse
from elspeth.web.execution.schemas import SemanticEdgeContractResponse, ValidationError, ValidationReadinessBlocker, ValidationWarning
from tests.unit.web.composer.test_continuation_active_evidence_graph import _recorder_source_graph
from tests.unit.web.composer.test_continuation_memory_completion_batch_data import capture as capture_memory
from tests.unit.web.composer.test_continuation_memory_completion_batch_data import completed_cut
from tests.unit.web.composer.test_continuation_prepared_batch_data import capture as capture_prepared
from tests.unit.web.composer.test_continuation_prepared_batch_data import prepared_cut
from tests.unit.web.composer.test_portable_evidence_application import portable_input
from tests.unit.web.coordination import test_composer_operation_authority

operation_store = test_composer_operation_authority.operation_store


def _cold_graph(graph, source):
    encoded = graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source)
    cold_source = graph_codec.admit_application_recorder_source(source.chunk, env_ref_names=frozenset())
    admitted = graph_codec.decode_application_graph(encoded, env_ref_names=frozenset(), recorder_source=cold_source)
    return admitted, cold_source, encoded


def _wire(graph, source):
    return graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source)


@pytest.mark.parametrize("repeated", (False, True), ids=("distinct-equal-llm-cells", "shared-llm-cell"))
def test_copy_preserves_recorder_slots_and_immutable_private_payloads(repeated):
    graph, source, encoded = _cold_graph(*_recorder_source_graph(repeated=repeated))
    source_bytes = source.chunk.canonical
    assert graph.recorder.llm_calls[0] == graph.recorder.llm_calls[1]
    assert (graph.recorder.llm_calls[0] is graph.recorder.llm_calls[1]) is repeated
    copied = copy_application_graph(graph)
    left, right = copied.recorder.llm_calls
    assert left == right
    assert (left is right) is repeated
    assert left is graph.recorder.llm_calls[0]
    assert right is graph.recorder.llm_calls[1]
    # Cold graph construction must allocate graph-node identities without
    # reconstructing or mutating the admitted recorder's large immutable cell.
    assert graph.recorder.llm_calls[0] is not source.snapshot.llm_calls[0]
    assert graph.recorder.llm_calls[1] is not source.snapshot.llm_calls[1]
    assert left.private_request == source.snapshot.llm_calls[0].private_request
    assert left.private_failure == source.snapshot.llm_calls[0].private_failure
    assert left.private_diagnostics == source.snapshot.llm_calls[0].private_diagnostics
    assert left.private_request is not None
    assert left.private_request.canonical is not None
    with pytest.raises(FrozenInstanceError):
        left.call_id = "changed"
    with pytest.raises(FrozenInstanceError):
        left.private_request.canonical = "changed"
    assert source.chunk.canonical == source_bytes
    assert _wire(copied, source) == encoded == _wire(graph, source)


@pytest.mark.parametrize("mutate_copy", (True, False), ids=("copy-to-source", "source-to-copy"))
def test_copy_isolates_mutable_batch_accumulator_and_validation_in_both_directions(mutate_copy):
    graph, source, encoded = _cold_graph(*_recorder_source_graph())
    copied = copy_application_graph(graph)
    assert _wire(copied, source) == encoded
    changed, untouched = (copied, graph) if mutate_copy else (graph, copied)
    batch = changed.selected.batch
    accumulator = changed.selected.accumulator
    assert batch is not untouched.selected.batch
    assert accumulator is not untouched.selected.accumulator
    assert batch.state is accumulator.state is untouched.selected.batch.state
    assert batch.last_runtime_preflight is accumulator.last_runtime_preflight
    assert batch.last_runtime_preflight.checks[0] is batch.last_runtime_preflight.checks[1]
    assert batch.tool_outcomes[0].response is batch.tool_outcomes[1].response
    assert batch.tool_outcomes[0].response.runtime_preflight is batch.last_runtime_preflight
    assert batch.tool_outcomes[0].call is changed.selected.calls[0]
    assert batch.tool_outcomes[0].response._validation_snapshot_hash == "a" * 64
    assert batch.tool_outcomes[0].response._state_validation_withheld is True
    batch.decoded_args_by_call_id["call-1"]["nested"].append("changed")
    batch.last_runtime_preflight.checks[0].detail = "changed"
    batch.last_runtime_preflight.readiness.completion_ready = True
    batch.advisor_calls_used += 1
    accumulator.advisor_calls_used += 1
    batch.refused_resources.add(("connection", "new"))
    batch.tool_outcomes.pop()
    assert batch.decoded_args_by_call_id["call-2"]["nested"][-1] == "changed"
    assert accumulator.last_runtime_preflight.checks[1].detail == "changed"
    assert untouched.selected.batch.last_runtime_preflight.checks[0].detail == "actual retained check"
    assert untouched.selected.batch.last_runtime_preflight.readiness.completion_ready is False
    assert untouched.selected.batch.advisor_calls_used == 4
    assert untouched.selected.accumulator.advisor_calls_used == 2
    assert len(untouched.selected.batch.tool_outcomes) == 2
    assert ("connection", "new") not in untouched.selected.batch.refused_resources
    assert _wire(untouched, source) == encoded


def test_copy_preserves_distinct_equal_models_and_states_with_shared_model_children():
    warm, source = _recorder_source_graph()
    batch, accumulator = warm.selected.batch, warm.selected.accumulator
    accumulator.state = CompositionState.from_dict(batch.state.to_dict())
    accumulator.last_validation = ValidationSummary(True, ())
    accumulator.last_runtime_preflight = batch.last_runtime_preflight.model_copy()
    graph, source, encoded = _cold_graph(warm, source)
    original_left, original_right = graph.selected.batch, graph.selected.accumulator
    assert original_left.state == original_right.state and original_left.state is not original_right.state
    assert original_left.last_validation == original_right.last_validation
    assert original_left.last_validation is not original_right.last_validation
    assert original_left.last_runtime_preflight == original_right.last_runtime_preflight
    assert original_left.last_runtime_preflight is not original_right.last_runtime_preflight
    assert original_left.last_runtime_preflight.checks is original_right.last_runtime_preflight.checks
    copied = copy_application_graph(graph)
    left, right = copied.selected.batch, copied.selected.accumulator
    assert left.state == right.state and left.state is not right.state
    assert left.last_validation == right.last_validation and left.last_validation is not right.last_validation
    assert left.last_runtime_preflight == right.last_runtime_preflight
    assert left.last_runtime_preflight is not right.last_runtime_preflight
    assert left.last_runtime_preflight.checks is right.last_runtime_preflight.checks
    assert left.last_runtime_preflight.checks is not graph.selected.batch.last_runtime_preflight.checks
    assert _wire(copied, source) == encoded
    left.last_runtime_preflight.is_valid = False
    assert right.last_runtime_preflight.is_valid is True
    right.last_runtime_preflight.checks[0].detail = "shared inside copy"
    assert left.last_runtime_preflight.checks[1].detail == "shared inside copy"
    assert graph.selected.batch.last_runtime_preflight.checks[0].detail == "actual retained check"


def test_copy_preserves_actually_distinct_equal_authoring_nodes_after_cold_admission():
    warm, _old_source = _recorder_source_graph()
    batch_authoring = warm.selected.batch.private_authorings_by_call["call-1"]
    recorder = replace(warm.recorder, private_authorings=(*warm.recorder.private_authorings, replace(batch_authoring)))
    warm = replace(warm, recorder=recorder)
    source = graph_codec.capture_application_recorder_source(recorder, env_ref_names=frozenset())
    graph, source, encoded = _cold_graph(warm, source)
    # The complete graph codec, not a fixture flag, establishes this input
    # partition before the copy under test sees it.
    original_batch = graph.selected.batch.private_authorings_by_call["call-1"]
    original_recorder = graph.recorder.private_authorings[-1]
    assert original_batch == original_recorder and original_batch is not original_recorder
    copied = copy_application_graph(graph)
    copied_batch = copied.selected.batch.private_authorings_by_call["call-1"]
    copied_recorder = copied.recorder.private_authorings[-1]
    assert copied_batch == copied_recorder and copied_batch is not copied_recorder
    assert copied_batch is original_batch and copied_recorder is original_recorder
    assert _wire(copied, source) == encoded


def test_copy_preserves_proxy_and_frozen_array_kinds_without_sharing_mutable_children():
    warm, source = _recorder_source_graph()
    arguments = warm.selected.batch.decoded_args_by_call_id["call-1"]
    arguments["retained_array"] = FrozenJsonArray((MappingProxyType({"nested": ["original"]}),))
    graph, source, encoded = _cold_graph(warm, source)
    copied = copy_application_graph(graph)
    original = graph.selected.batch.decoded_args_by_call_id["call-1"]["retained_array"]
    retained = copied.selected.batch.decoded_args_by_call_id["call-1"]["retained_array"]
    assert type(retained) is FrozenJsonArray and type(retained[0]) is MappingProxyType
    assert retained is not original and retained[0] is not original[0]
    assert _wire(copied, source) == encoded
    retained[0]["nested"].append("copy")
    original[0]["nested"].append("source")
    assert retained[0]["nested"] == ["original", "copy"]
    assert original[0]["nested"] == ["original", "source"]
    with pytest.raises(TypeError):
        retained[0]["new"] = "refused"


def test_copy_detaches_rich_validation_nodes_without_repartitioning_rejection_evidence():
    warm, source = _recorder_source_graph()
    runtime = warm.selected.batch.last_runtime_preflight
    rejection = SafeRequestRejection(reason_code=None, reason_disposition="unavailable", reason_source="unavailable")
    runtime.checks[0].request_rejection = rejection
    runtime.errors.append(
        ValidationError(component_id="node", component_type="transform", message="original error", suggestion=None, error_code=None)
    )
    runtime.warnings.append(
        ValidationWarning(component_id="node", component_type="transform", message="original warning", suggestion=None, warning_code="note")
    )
    runtime.readiness.blockers.append(
        ValidationReadinessBlocker(
            code="advisor_signoff_blocked",
            component_id=None,
            component_type=None,
            detail="original blocker",
            suggestion=None,
            request_rejection=rejection,
            note=None,
        )
    )
    runtime.semantic_contracts.append(
        SemanticEdgeContractResponse(
            from_id="source",
            to_id="node",
            consumer_plugin="consumer",
            producer_plugin="producer",
            producer_field="value",
            consumer_field="value",
            outcome="unknown",
            requirement_code="required",
        )
    )
    graph, source, encoded = _cold_graph(warm, source)
    copied = copy_application_graph(graph)
    assert _wire(copied, source) == encoded
    cloned = copied.selected.batch.last_runtime_preflight
    original = graph.selected.batch.last_runtime_preflight
    assert cloned.checks[0].request_rejection is cloned.readiness.blockers[0].request_rejection
    assert cloned.checks[0].request_rejection == original.checks[0].request_rejection
    cloned.errors[0].message = "copy error"
    original.warnings[0].message = "source warning"
    cloned.readiness.blockers[0].detail = "copy blocker"
    original.semantic_contracts[0].outcome = "conflict"
    assert original.errors[0].message == "original error"
    assert cloned.warnings[0].message == "original warning"
    assert original.readiness.blockers[0].detail == "original blocker"
    assert cloned.semantic_contracts[0].outcome == "unknown"


@pytest.mark.parametrize("mutate_copy", (True, False), ids=("copy-to-source", "source-to-copy"))
def test_copy_isolates_real_prepared_call_and_preserves_argument_aliases(monkeypatch, mutate_copy):
    warm, source = _recorder_source_graph()
    prepared = decode_prepared_batch_data(capture_prepared(prepared_cut(monkeypatch)))
    warm = replace(
        warm,
        selected=RestoredBatchData(prepared.batch, prepared.accumulator, prepared.calls),
        selected_audit=prepared.audit,
        selected_dialect=prepared.dialect,
    )
    graph, source, encoded = _cold_graph(warm, source)
    copied = copy_application_graph(graph)
    assert copied.selected_audit is graph.selected_audit
    assert _wire(copied, source) == encoded
    changed, untouched = (copied, graph) if mutate_copy else (graph, copied)
    call = changed.selected.batch.current_call
    other = untouched.selected.batch.current_call
    assert call is not None and other is not None and call is not other
    assert call.audit is other.audit is None
    assert call.tool_call is changed.selected.calls[2]
    assert call.arguments is call.audit_arguments is changed.selected.batch.decoded_args_by_call_id[call.tool_call.id]
    original_conformant = other.wire.wire_conformant
    call.arguments["component"] = "nodes"
    call.wire.wire_conformant = not original_conformant
    call.phase = "application_pending"
    assert changed.selected.batch.decoded_args_by_call_id[call.tool_call.id]["component"] == "nodes"
    assert other.arguments["component"] == "summary"
    assert other.wire.wire_conformant is original_conformant
    assert other.phase == "prepared"
    assert _wire(untouched, source) == encoded


def test_copy_keeps_admitted_discovery_contracts_and_detaches_mutable_schema_results(monkeypatch):
    # Completed memory DATA has its own real codec. Application wire currently
    # excludes this phase; the wrapper below exercises the copier's supported
    # nested DTO domain without asserting application or execution admission.
    base, _source = _recorder_source_graph()
    encoded = capture_memory(completed_cut(monkeypatch, kind="schema"))
    admitted = decode_memory_completion_batch_data(encoded)
    graph = ApplicationGraph(
        RestoredBatchData(admitted.batch, admitted.accumulator, admitted.calls),
        base.effects,
        base.recorder,
        (),
        admitted.audit,
        admitted.dialect,
    )
    copied = copy_application_graph(graph)
    cloned_memory = replace(admitted, batch=copied.selected.batch, accumulator=copied.selected.accumulator, calls=copied.selected.calls)
    assert encode_restored_memory_completion_batch_data(cloned_memory) == encoded
    original = graph.selected.batch.current_call.memory_result
    cloned = copied.selected.batch.current_call.memory_result
    assert type(cloned) is AdmittedDiscoveryResult
    assert type(cloned.response) is AdmittedPluginSchemaResponse
    assert cloned.contract is cloned.response.contract is original.contract is PLUGIN_SCHEMA_RESPONSE_CONTRACT
    assert cloned is not original and cloned.result is not original.result
    assert cloned.response is not original.response and cloned.response.snapshot is not original.response.snapshot
    assert type(cloned.result.data) is PluginSchemaInfo
    assert cloned.to_dict() == original.to_dict()
    assert cloned.response.readmit(cloned.contract).to_wire() == original.response.to_wire()
    assert cloned.result.updated_state is copied.selected.batch.state
    assert cloned.result.runtime_preflight is copied.selected.batch.last_runtime_preflight
    left = cloned.result.data.json_schema["properties"]["items"]["default"]
    right = cloned.result.data.knob_schema["fields"][0]["default"]
    assert left is right
    original_default = original.result.data.json_schema["properties"]["items"]["default"]
    assert left is not original_default
    snapshot_wire = cloned.response.to_wire()
    left.append("copy")
    original_default.append("source")
    cloned.result.data.description = "copy description"
    assert right == ["ordinary", "ordinary", "copy"]
    assert original_default == ["ordinary", "ordinary", "source"]
    assert original.result.data.description == "Retained schema"
    assert cloned.response.to_wire() == original.response.to_wire() == snapshot_wire
    assert cloned.result.data.__pydantic_fields_set__ is not original.result.data.__pydantic_fields_set__
    original_fields = set(original.result.data.__pydantic_fields_set__)
    cloned.result.data.__pydantic_fields_set__.discard("description")
    assert original.result.data.__pydantic_fields_set__ == original_fields


@pytest.mark.asyncio
@pytest.mark.parametrize("share_authoring", (True, False), ids=("with-authoring-rebind", "without-authoring-rebind"))
async def test_copy_preserves_genuine_portable_crosscut_and_recorder_aliases(
    operation_store, tmp_path, monkeypatch, share_authoring, record_property
):
    async with portable_input(operation_store, tmp_path, monkeypatch, share_authoring=share_authoring) as (
        _running,
        _args,
        values,
        _audit,
        _messages,
    ):
        bundle = build_portable_p4_application(**values)
        names = values["env_ref_names"]
        source = graph_codec.admit_application_recorder_source(bundle.evidence_after.recorder_chunk, env_ref_names=names)
        graph = graph_codec.decode_application_graph(bundle.entry.graph_json, env_ref_names=names, recorder_source=source)
        call_id = graph.selected.calls[0].id
        original_authoring = graph.selected.batch.private_authorings_by_call[call_id]
        original_recorder_authoring = graph.recorder.private_authorings[0]
        assert original_authoring == original_recorder_authoring
        original_shared = original_authoring is original_recorder_authoring
        record_property("cold_input_authoring_shared", original_shared)
        copied = copy_application_graph(graph)
        assert graph_codec.encode_application_graph(copied, env_ref_names=names, recorder_source=source) == bundle.entry.graph_json
        cut = copied.cuts[-1]
        assert copied.effects is cut.effects_after
        assert copied.selected.accumulator is cut.data.accumulator
        assert copied.selected.calls is cut.data.calls
        assert copied.selected.batch.tool_outcomes is cut.data.batch.tool_outcomes
        assert copied.selected.batch.tool_outcomes is not graph.selected.batch.tool_outcomes
        assert cut.effects_before.initial_state is cut.data.batch.entry_state
        assert copied.effects.initial_state is cut.effects_before.initial_state
        assert copied.selected.calls[0].id == call_id
        authoring = copied.selected.batch.private_authorings_by_call[call_id]
        assert (authoring is copied.recorder.private_authorings[0]) is original_shared
        assert authoring == copied.recorder.private_authorings[0]
        assert copied.recorder.invocations[1].private_authoring is copied.recorder.private_authorings[0]
        assert authoring is graph.selected.batch.private_authorings_by_call[call_id]
        assert copied.recorder.invocations[1] is graph.recorder.invocations[1]
        assert copied.recorder.llm_calls[0] is graph.recorder.llm_calls[0]
        assert copied.recorder.private_candidates[0] is graph.recorder.private_candidates[0]
        assert copied.recorder.visible_tool_events[0].invocation_index == graph.recorder.visible_tool_events[0].invocation_index
        assert copied.recorder.visible_tool_events[0].event is graph.recorder.visible_tool_events[0].event
        original_count = len(graph.selected.batch.tool_outcomes)
        assert original_count > 0
        cut.data.batch.tool_outcomes.clear()
        assert copied.selected.batch.tool_outcomes == []
        assert len(graph.selected.batch.tool_outcomes) == original_count
        assert graph_codec.encode_application_graph(graph, env_ref_names=names, recorder_source=source) == bundle.entry.graph_json
