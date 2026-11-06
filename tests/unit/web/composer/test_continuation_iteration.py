from dataclasses import replace

import pytest

from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
from elspeth.contracts.errors import AuditIntegrityError, FailedTurnMetadata
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _AdmittedToolFunction, _AdvisorReviewState
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.continuation_codec import encode_private_chunk
from elspeth.web.composer.continuation_iteration import (
    capture_continuation_iteration,
    capture_continuation_producer_inputs,
    decode_continuation_iteration,
    encode_continuation_iteration,
    iteration_values,
)
from elspeth.web.composer.schema_disclosure import SchemaDisclosureTracker
from elspeth.web.composer.state import CompositionState, PipelineMetadata, ValidationSummary
from elspeth.web.credential_guard import CredentialMaterialRefused


def state():
    return CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def snapshot():
    actual = state()
    equal_distinct = state()
    call = _AdmittedToolCall(
        id="schema-call", function=_AdmittedToolFunction("get_plugin_schema", ' { "plugin_type": "source", "name": "csv" } ')
    )
    inputs = capture_continuation_producer_inputs(
        ((call, actual), (replace(call, id="again"), actual), (replace(call, id="distinct"), equal_distinct)), env_ref_names=frozenset()
    )
    tracker = AntiAnchorTracker()
    for number in range(3):
        tracker.record_failure("set_pipeline", str(number), category=ToolArgumentErrorCategory.SEMANTIC_RULE)
    disclosure = SchemaDisclosureTracker()
    disclosure.mark_plugin_schema_loaded("session", "source", "csv")
    return capture_continuation_iteration(
        producer_inputs=inputs,
        last_validation=ValidationSummary(is_valid=False, errors=()),
        advisor_review=_AdvisorReviewState(2, ("Keep the selected output",), "evidence", ("set_pipeline", "set_metadata")),
        failed_turn=FailedTurnMetadata("assistant", 3, 2),
        anti_anchor=tracker.snapshot_for_continuation(),
        disclosure=disclosure.snapshot_for_continuation(session_id="session", catalog_hash="catalog", policy_hash="policy"),
    )


def test_complete_iteration_and_actual_state_identity_shelf():
    original = snapshot()
    restored = decode_continuation_iteration(encode_continuation_iteration(original))
    assert restored == original
    values = iteration_values(restored)
    assert values.advisor_review.previous_findings == ("Keep the selected output",)
    assert values.advisor_review.successful_mutating_actions == ("set_pipeline", "set_metadata")
    assert values.advisor_review.completed_passes == 2
    assert values.failed_turn == FailedTurnMetadata("assistant", 3, 2)
    assert values.last_validation == ValidationSummary(is_valid=False, errors=())
    assert restored.producer_inputs.schema_inputs[0].state_index == restored.producer_inputs.schema_inputs[1].state_index
    assert restored.producer_inputs.schema_inputs[2].state_index != restored.producer_inputs.schema_inputs[0].state_index
    assert values.schema_states[0] == values.schema_states[1]
    assert values.schema_states[0] is not values.schema_states[1]
    assert restored.producer_inputs.schema_inputs[0].raw_arguments.startswith(" { ")
    tracker = AntiAnchorTracker.for_continuation(restored.anti_anchor)
    assert tracker.next_hint() is not None
    tracker.consume_fire()
    assert tracker.next_hint() is None
    disclosure = SchemaDisclosureTracker()
    disclosure.restore_session_for_continuation(restored.disclosure, session_id="session", catalog_hash="catalog", policy_hash="policy")
    assert disclosure.schemas_loaded_for_session("session") == frozenset({("source", "csv")})
    with pytest.raises(AuditIntegrityError):
        disclosure.restore_session_for_continuation(restored.disclosure, session_id="other", catalog_hash="catalog", policy_hash="policy")


@pytest.mark.parametrize("change", ["unknown", "missing", "raw", "index", "credential"])
def test_iteration_complete_decode_refuses_changed_owned_member(change):
    original = snapshot()
    assert decode_continuation_iteration(encode_continuation_iteration(original)) == original
    wire = original.model_dump(mode="json", by_alias=True)
    if change == "unknown":
        wire["advisor_review_json"] = wire["advisor_review_json"][:-1] + ',"future":1}'
    elif change == "missing":
        del wire["failed_turn_json"]
    elif change == "raw":
        wire["producer_inputs"]["schema"][0]["raw_arguments"] = "{}"
    elif change == "index":
        wire["producer_inputs"]["schema"][0]["state_index"] = 900
    else:
        wire["advisor_review_json"] = canonical_json(
            {
                "completed_passes": 2,
                "previous_findings": ["sk-" + "a" * 24],
                "previous_evidence_hash": "evidence",
                "successful_mutating_actions": [],
            }
        )
    with pytest.raises((AuditIntegrityError, CredentialMaterialRefused)):
        decode_continuation_iteration(encode_private_chunk(codec="composer.iteration.v1", canonical=canonical_json(wire)))


def test_actual_schema_discovery_observation_requires_selected_complete_producer():
    from elspeth.web.composer.continuation_iteration import capture_admitted_schema_inputs
    from elspeth.web.composer.discovery_response import admit_discovery_result
    from elspeth.web.composer.tools._common import ToolResult
    from tests.unit.web.composer.test_generation_schema_response import schema

    actual = state()
    original = schema()
    call = _AdmittedToolCall(
        id="schema",
        function=_AdmittedToolFunction("get_plugin_schema", canonical_json({"plugin_type": original.plugin_type, "name": original.name})),
    )
    result = ToolResult(
        success=True, updated_state=actual, validation=ValidationSummary(is_valid=True, errors=()), affected_nodes=(), data=original
    )
    admitted = admit_discovery_result("get_plugin_schema", result)
    retained = capture_admitted_schema_inputs(((call, admitted), (replace(call, id="second"), admitted)), env_ref_names=frozenset())
    assert retained.schema_inputs[0].state_index == retained.schema_inputs[1].state_index
    assert retained.states_json == (canonical_json(actual.to_dict()),)
    with pytest.raises(AuditIntegrityError):
        capture_admitted_schema_inputs(
            (
                (
                    replace(call, function=_AdmittedToolFunction("get_plugin_schema", '{"plugin_type":"transform","name":"another"}')),
                    admitted,
                ),
            ),
            env_ref_names=frozenset(),
        )
    with pytest.raises(AuditIntegrityError):
        capture_admitted_schema_inputs(((call, replace(admitted, result=replace(result, success=False))),), env_ref_names=frozenset())
    with pytest.raises(TypeError):
        capture_admitted_schema_inputs(((call, result),), env_ref_names=frozenset())


def test_iteration_retains_original_schema_wire_key_after_nominal_attribute_rename():
    import json

    from elspeth.web.composer.continuation_iteration import ContinuationProducerInputs

    original = snapshot()
    chunk = encode_continuation_iteration(original)
    wire = json.loads(chunk.canonical)
    inputs = wire["producer_inputs"]
    assert set(inputs) == {"states_json", "schema"}
    expected = [
        {
            "arguments_json": item.arguments_json,
            "raw_arguments": item.raw_arguments,
            "state_index": item.state_index,
            "tool_call_id": item.tool_call_id,
        }
        for item in original.producer_inputs.schema_inputs
    ]
    assert inputs["schema"] == expected
    assert inputs["states_json"] == list(original.producer_inputs.states_json)
    admitted = ContinuationProducerInputs.model_validate_json(canonical_json(inputs))
    assert admitted == original.producer_inputs
    assert admitted.model_dump(mode="json", by_alias=True) == inputs
    assert decode_continuation_iteration(chunk) == original
    assert encode_continuation_iteration(decode_continuation_iteration(chunk)).canonical == chunk.canonical
