"""Actual coupled B2 data owners; no provider, SQL, or restored authority."""

import json
from dataclasses import replace

import pytest

from elspeth.contracts.composer_audit import PrivateToolAuthoring, ToolArgumentErrorCategory
from elspeth.contracts.composer_mutation import ComposerMutationCeiling, ComposerMutationRevocation
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _AdmittedToolFunction, _ToolOutcome
from elspeth.web.composer.continuation_batch_data import decode_coupled_batch_data, encode_coupled_batch_data
from elspeth.web.composer.continuation_tool_batch import RetainedBatchFailure
from elspeth.web.composer.state import CompositionState, ValidationSummary
from elspeth.web.composer.tool_batch import BatchAccumulator, ContinuationBatchState, ContinuationCallState
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.execution.schemas import ValidationCheck, ValidationReadiness, ValidationResult


def actual_cut():
    state = CompositionState.from_dict(
        {"sources": {}, "nodes": [], "edges": [], "outputs": [], "metadata": {"name": "retained", "description": "café"}, "version": 3}
    )
    validation = ValidationSummary(True, ())
    check = ValidationCheck(name="graph_structure", passed=True, detail="actual retained check", affected_nodes=(), outcome_code=None)
    runtime = ValidationResult(
        is_valid=True,
        checks=[check, check],
        errors=[],
        warnings=[],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=False, blockers=[]),
    )
    # Repeated nominal model is allowed by the actual constructor and must not
    # be silently expanded into two independent mutable check owners.
    assert runtime.checks[0] is runtime.checks[1]
    acc = BatchAccumulator(
        state,
        validation,
        runtime,
        2,
        ComposerMutationRevocation(ComposerMutationCeiling("session", "logical-request", "auto_commit"), "explicit_approve"),
    )
    calls = (
        _AdmittedToolCall("call-1", _AdmittedToolFunction("get_pipeline_state", '{"component":"summary"}')),
        _AdmittedToolCall("call-2", _AdmittedToolFunction("get_pipeline_state", '{"component":"summary"}')),
        _AdmittedToolCall("call-3", _AdmittedToolFunction("get_pipeline_state", '{"component":"summary"}')),
    )
    # Failed selected discovery has no data, but keeps its real validation,
    # pre-state identity and private ToolResult flags.
    result = ToolResult(
        False,
        state,
        validation,
        (),
        prior_validation=validation,
        runtime_preflight=runtime,
        post_call_hints=("first", "first", "last"),
        _validation_snapshot_hash="a" * 64,
        _state_validation_withheld=True,
    )
    outcome = _ToolOutcome(calls[0], result, None, None, None, 3, 3, True, False)
    second = _ToolOutcome(calls[1], result, None, None, None, 3, 3, None, None)
    batch = ContinuationBatchState(
        state,
        validation,
        runtime,
        4,
        entry_state=state,
        phase="dispatch_ready",
        next_tool_index=2,
        assistant_message_index=7,
        provider_model_version="retained-model",
        turn_has_discovery=True,
        all_cache_hits=False,
        tool_outcomes=[outcome, second],
        pre_state_id="state-3",
        proposals_this_turn=1,
        mutation_success_observed=True,
        stop_issued_batch=True,
        retained_failure=RetainedBatchFailure(kind="advisor_timeout", error_class="TimeoutError"),
        advisor_compose_timeout="pre_call",
    )
    shared = {"component": "summary", "nested": ["first", "first", {"label": "café"}]}
    batch.decoded_args_by_call_id = {"call-2": shared, "call-1": shared}
    batch.private_authorings_by_call = {
        call.id: PrivateToolAuthoring(call.id, call.function.name, call.function.arguments, None, call.function.arguments)
        for call in calls[:2]
    }
    resource = ("connection", "retained")
    promise = frozenset({resource})
    batch.refused_resources = {resource}
    batch.promises_by_call = {"call-2": promise, "call-1": promise}
    return batch, acc, calls


def encode(batch, acc, calls):
    return encode_coupled_batch_data(batch=batch, accumulator=acc, calls=calls)


def test_coupled_cut_retains_revocation_independent_counts_and_actual_aliases():
    batch, acc, calls = actual_cut()
    encoded = encode(batch, acc, calls)
    restored = decode_coupled_batch_data(encoded)
    live, retained_acc = restored.batch, restored.accumulator
    assert retained_acc.authority_revocation == acc.authority_revocation
    assert retained_acc.authority_revocation is not acc.authority_revocation
    assert retained_acc.advisor_calls_used == 2 and live.advisor_calls_used == 4
    assert retained_acc.state is live.state is live.entry_state
    assert live.last_validation is retained_acc.last_validation
    first, second = live.tool_outcomes
    assert first.call is restored.calls[0] and second.call is restored.calls[1]
    assert first.response is second.response
    assert type(first.response) is ToolResult
    assert first.response.updated_state is live.state
    assert first.response.validation is first.response.prior_validation is live.last_validation
    assert first.response.runtime_preflight is live.last_runtime_preflight is retained_acc.last_runtime_preflight
    assert first.response._state_validation_withheld is True
    assert first.response._validation_snapshot_hash == "a" * 64
    assert first.response.post_call_hints == ("first", "first", "last")
    assert (first.strict_sent, first.wire_conformant, second.strict_sent, second.wire_conformant) == (True, False, None, None)
    assert live.retained_failure == batch.retained_failure
    assert live.advisor_compose_timeout == "pre_call" and live.stop_issued_batch is True
    assert live.assistant_message_index == 7 and live.pre_state_id == "state-3"
    assert live.proposals_this_turn == 1 and live.mutation_success_observed is True
    assert encode(live, retained_acc, restored.calls) == encoded


def test_mutable_argument_and_preflight_aliases_remain_consequential():
    batch, acc, calls = actual_cut()
    restored = decode_coupled_batch_data(encode(batch, acc, calls))
    live = restored.batch
    assert tuple(live.decoded_args_by_call_id) == ("call-2", "call-1")
    assert live.decoded_args_by_call_id["call-2"] is live.decoded_args_by_call_id["call-1"]
    live.decoded_args_by_call_id["call-2"]["nested"].append("later")
    assert live.decoded_args_by_call_id["call-1"]["nested"][-1] == "later"
    assert batch.decoded_args_by_call_id["call-1"]["nested"][-1] == {"label": "café"}
    runtime = live.last_runtime_preflight
    assert runtime is restored.accumulator.last_runtime_preflight
    assert runtime.checks[0] is runtime.checks[1]
    runtime.checks[0].detail = "retained mutation"
    assert restored.accumulator.last_runtime_preflight.checks[1].detail == "retained mutation"
    assert acc.last_runtime_preflight.checks[1].detail == "actual retained check"
    assert tuple(live.promises_by_call) == ("call-2", "call-1")
    assert live.promises_by_call["call-2"] is live.promises_by_call["call-1"]


def test_equal_distinct_state_and_validation_roots_are_not_merged():
    batch, acc, calls = actual_cut()
    acc.state = CompositionState.from_dict(batch.state.to_dict())
    acc.last_validation = ValidationSummary(True, ())
    assert acc.state == batch.state and acc.state is not batch.state
    restored = decode_coupled_batch_data(encode(batch, acc, calls))
    assert restored.accumulator.state == restored.batch.state
    assert restored.accumulator.state is not restored.batch.state
    assert restored.accumulator.last_validation == restored.batch.last_validation
    assert restored.accumulator.last_validation is not restored.batch.last_validation


def test_empty_initialized_cut_is_real_and_keeps_accumulator_revocation():
    _batch, acc, _calls = actual_cut()
    empty = ContinuationBatchState(
        acc.state,
        acc.last_validation,
        acc.last_runtime_preflight,
        2,
        entry_state=acc.state,
        phase="dispatch_ready",
        assistant_message_index=0,
    )
    restored = decode_coupled_batch_data(encode(empty, acc, ()))
    assert restored.calls == () and restored.batch.tool_outcomes == []
    assert restored.accumulator.authority_revocation == acc.authority_revocation


@pytest.mark.parametrize("field", ["plugin_crash", "plugin_crash_cause", "advisor_failure"])
def test_live_exceptions_are_refused_without_silent_failure_descriptors(field):
    batch, acc, calls = actual_cut()
    batch = replace(batch, **{field: RuntimeError("original live owner")})
    with pytest.raises(AuditIntegrityError, match="live exception"):
        encode(batch, acc, calls)


def test_actual_current_call_is_refused_without_recreating_process_local_audit():
    batch, acc, calls = actual_cut()
    batch.current_call = ContinuationCallState(2, calls[2], batch.state, 3)
    with pytest.raises(AuditIntegrityError, match="current call/timing"):
        encode(batch, acc, calls)


@pytest.mark.parametrize("field", ["p4_input_json", "p4_receipt_json"])
def test_p4_inputs_and_committed_receipts_require_their_real_other_owner(field):
    batch, acc, calls = actual_cut()
    batch = replace(batch, **{field: "{}"})
    with pytest.raises(AuditIntegrityError, match="P4 input or committed witness"):
        encode(batch, acc, calls)


def test_duplicate_call_ids_refuse_before_dictionary_normalization():
    batch, acc, calls = actual_cut()
    with pytest.raises(AuditIntegrityError, match="duplicate"):
        encode(batch, acc, (*calls, calls[0]))


def test_equal_copied_outcome_call_is_not_the_admitted_call_owner():
    batch, acc, calls = actual_cut()
    batch.tool_outcomes[0] = replace(batch.tool_outcomes[0], call=replace(calls[0]))
    with pytest.raises(AuditIntegrityError, match="actual admitted call identity"):
        encode(batch, acc, calls)


def test_shared_constructor_frozen_response_mapping_refuses_alias_loss():
    batch, acc, _calls = actual_cut()
    advisor = _AdmittedToolCall("advisor", _AdmittedToolFunction("request_advisor_hint", "{}"))
    outcome = _ToolOutcome(advisor, {"status": "disabled"}, None, None, None, 3, 3)
    # The graph also names the frozen response through a mutable argument tree.
    # _ToolOutcome's real constructor detaches that proxy. Refuse the cut.
    batch.tool_outcomes = [outcome]
    batch.next_tool_index = 1
    batch.decoded_args_by_call_id = {"advisor": {"retained": outcome.response}}
    batch.private_authorings_by_call = {"advisor": PrivateToolAuthoring("advisor", "request_advisor_hint", "{}", None, "{}")}
    batch.promises_by_call = {}
    with pytest.raises(AuditIntegrityError, match="alias topology"):
        encode(batch, acc, (advisor,))


@pytest.mark.parametrize("change", ["unreachable", "unknown_member", "cycle", "scalar_container"])
def test_complete_graph_rejects_lossy_or_forged_structure(change):
    batch, acc, calls = actual_cut()
    value = json.loads(encode(batch, acc, calls))
    if change == "unreachable":
        value["nodes"].append({"kind": "tuple", "value": []})
    elif change == "unknown_member":
        value["nodes"][0]["value"].append(["unknown", {"scalar": None}])
    elif change == "cycle":
        value["nodes"][0]["value"][0][1] = {"ref": 0}
    else:
        value["nodes"][0]["value"][0][1] = {"scalar": {"forged": "state"}}
    with pytest.raises(AuditIntegrityError):
        decode_coupled_batch_data(canonical_json(value))


def test_raw_call_credentials_refuse_even_when_not_yet_executed():
    batch, acc, calls = actual_cut()
    bad = _AdmittedToolCall("call-3", _AdmittedToolFunction("get_pipeline_state", '{"api_key":"opaque-private-value"}'))
    with pytest.raises(CredentialMaterialRefused):
        encode(batch, acc, (*calls[:2], bad))


def test_argument_error_category_and_absent_response_remain_nominal():
    batch, acc, calls = actual_cut()
    batch.tool_outcomes[1] = _ToolOutcome(
        calls[1], None, "ValueError", ToolArgumentErrorCategory.CANONICALIZATION, "invalid canonical input", 3, 3
    )
    restored = decode_coupled_batch_data(encode(batch, acc, calls))
    outcome = restored.batch.tool_outcomes[1]
    assert outcome.response is None
    assert outcome.error_category is ToolArgumentErrorCategory.CANONICALIZATION
    assert outcome.error_class == "ValueError" and outcome.error_message == "invalid canonical input"


def test_real_private_chunk_binding_and_wrong_codec_refuse():
    from elspeth.web.composer.continuation_batch_data import decode_coupled_batch_chunk, encode_coupled_batch_chunk

    batch, acc, calls = actual_cut()
    chunk = encode_coupled_batch_chunk(batch=batch, accumulator=acc, calls=calls)
    restored = decode_coupled_batch_chunk(chunk)
    assert restored.accumulator.authority_revocation == acc.authority_revocation
    with pytest.raises(AuditIntegrityError, match="exact selected codec"):
        decode_coupled_batch_chunk(replace(chunk, reference=chunk.reference.model_copy(update={"codec": "composer.state.v1"})))
    altered = json.loads(chunk.canonical)
    altered["schema_version"] = "composer.coupled-batch-data.v2"
    with pytest.raises(AuditIntegrityError, match="bytes differ"):
        decode_coupled_batch_chunk(replace(chunk, canonical=canonical_json(altered)))


def test_distinct_mutable_preflight_owners_keep_shared_child_list():
    batch, acc, calls = actual_cut()
    acc.last_runtime_preflight = batch.last_runtime_preflight.model_copy(update={"is_valid": False})
    assert acc.last_runtime_preflight is not batch.last_runtime_preflight
    assert acc.last_runtime_preflight.checks is batch.last_runtime_preflight.checks
    restored = decode_coupled_batch_data(encode(batch, acc, calls))
    left, right = restored.batch.last_runtime_preflight, restored.accumulator.last_runtime_preflight
    assert left is not right and left.is_valid is True and right.is_valid is False
    assert left.checks is right.checks
    left.checks.append(ValidationCheck(name="graph_structure", passed=False, detail="later", affected_nodes=(), outcome_code=None))
    assert len(right.checks) == 3
    assert len(batch.last_runtime_preflight.checks) == 2


def test_missing_accumulator_revocation_member_cannot_fill_a_default():
    batch, acc, calls = actual_cut()
    value = json.loads(encode(batch, acc, calls))
    node = value["nodes"][value["roots"][1]["ref"]]
    assert node["kind"] == "BatchAccumulator"
    node["value"] = [pair for pair in node["value"] if pair[0] != "authority_revocation"]
    with pytest.raises(AuditIntegrityError, match="complete ordered nominal members"):
        decode_coupled_batch_data(canonical_json(value))


def test_runtime_constructor_must_not_restore_a_dict_as_its_nominal_check():
    from elspeth.web.composer.continuation_batch_data import _construct, _members

    batch, acc, calls = actual_cut()
    runtime = batch.last_runtime_preflight
    kind, members = _members(runtime)
    assert kind == "ValidationResult"
    assert type(runtime.checks[0]) is ValidationCheck
    restored_runtime = _construct(kind, members)
    assert type(restored_runtime) is ValidationResult
    assert restored_runtime.checks is runtime.checks
    assert restored_runtime.checks[0] is runtime.checks[0]

    # Exercise the real member-to-nominal boundary before downstream export can
    # reject the corrupted object for an unrelated serialization reason.
    check_members = _members(runtime.checks[0])[1]
    forged_members = {**members, "checks": [check_members, check_members]}
    with pytest.raises(AuditIntegrityError, match="constructor changed nominal types"):
        _construct(kind, forged_members)

    value = json.loads(encode(batch, acc, calls))
    node = next(node for node in value["nodes"] if node["kind"] == "ValidationCheck")
    node["kind"] = "dict"
    with pytest.raises(AuditIntegrityError, match="constructor changed nominal types"):
        decode_coupled_batch_data(canonical_json(value))


def test_explicit_environment_inventory_is_required_for_retained_raw_reference():
    batch, acc, calls = actual_cut()
    pending = _AdmittedToolCall("call-3", _AdmittedToolFunction("get_pipeline_state", '{"api_key":"${RETAINED_KEY}"}'))
    selected = (*calls[:2], pending)
    with pytest.raises(CredentialMaterialRefused):
        encode(batch, acc, selected)
    encoded = encode_coupled_batch_data(batch=batch, accumulator=acc, calls=selected, env_ref_names=frozenset({"RETAINED_KEY"}))
    restored = decode_coupled_batch_data(encoded, env_ref_names=frozenset({"RETAINED_KEY"}))
    assert restored.calls[2].function.arguments == pending.function.arguments
    with pytest.raises(CredentialMaterialRefused):
        decode_coupled_batch_data(encoded)


_METADATA_FIELDS = ("call_id", "tool_name", "resource_kind", "resource_value")
_METADATA_ROUTES = ("data_encode", "data_decode", "chunk_encode", "chunk_decode")


def metadata_cut(field, value):
    batch, acc, calls = actual_cut()
    pending = calls[2]
    if field == "call_id":
        pending = replace(pending, id=value)
    elif field == "tool_name":
        # No completed result or decoded-argument owner masks this metadata
        # channel: preserve a real unexecuted malformed raw call.
        pending = replace(pending, function=replace(pending.function, name=value, arguments="retained malformed arguments"))
    else:
        resource = (value, "retained-resource") if field == "resource_kind" else ("connection", value)
        promise = frozenset({resource})
        batch.promises_by_call = {"call-2": promise, "call-1": promise}
        assert resource not in batch.refused_resources
    return batch, acc, (*calls[:2], pending)


@pytest.mark.parametrize("field", _METADATA_FIELDS)
def test_safe_metadata_and_promises_keep_values_aliases_and_revocation_on_both_routes(field):
    from elspeth.web.composer.continuation_batch_data import decode_coupled_batch_chunk, encode_coupled_batch_chunk

    safe = "retained-metadata-label"
    batch, acc, calls = metadata_cut(field, safe)
    encoded = encode(batch, acc, calls)
    chunk = encode_coupled_batch_chunk(batch=batch, accumulator=acc, calls=calls)
    assert chunk.canonical == encoded
    for restored in (decode_coupled_batch_data(encoded), decode_coupled_batch_chunk(chunk)):
        assert encode(restored.batch, restored.accumulator, restored.calls) == encoded
        assert restored.accumulator.authority_revocation == acc.authority_revocation
        assert restored.batch.state is restored.accumulator.state is restored.batch.entry_state
        assert restored.batch.tool_outcomes[0].call is restored.calls[0]
        assert restored.batch.tool_outcomes[0].response is restored.batch.tool_outcomes[1].response
        assert restored.batch.promises_by_call["call-1"] is restored.batch.promises_by_call["call-2"]
        if field == "call_id":
            assert restored.calls[2].id == safe
        elif field == "tool_name":
            assert restored.calls[2].function.name == safe
            assert restored.calls[2].function.arguments == "retained malformed arguments"
        else:
            position = 0 if field == "resource_kind" else 1
            assert next(iter(restored.batch.promises_by_call["call-1"]))[position] == safe


@pytest.mark.parametrize("route", _METADATA_ROUTES)
@pytest.mark.parametrize("field", _METADATA_FIELDS)
@pytest.mark.parametrize("marker", ("https://synthetic:password@example.test", "sk-" + "a" * 24))
def test_call_metadata_and_promised_resource_credentials_refuse_at_every_public_boundary(route, field, marker):
    from hashlib import sha256

    from elspeth.web.composer.continuation import PrivateChunkRef
    from elspeth.web.composer.continuation_batch_data import decode_coupled_batch_chunk, encode_coupled_batch_chunk
    from elspeth.web.composer.continuation_codec import PrivateChunk, verify_private_chunk
    from elspeth.web.credential_guard import require_no_credential_material

    # Control the real original detector before attributing refusal to a new
    # source boundary. These are synthetic public fixtures, never credentials.
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material(marker, surface="batch metadata control")
    if route in {"data_encode", "chunk_encode"}:
        batch, acc, calls = metadata_cut(field, marker)
        with pytest.raises(CredentialMaterialRefused):
            if route == "data_encode":
                encode_coupled_batch_data(batch=batch, accumulator=acc, calls=calls)
            else:
                encode_coupled_batch_chunk(batch=batch, accumulator=acc, calls=calls)
        return

    safe = "retained-metadata-label"
    batch, acc, calls = metadata_cut(field, safe)
    normal = encode(batch, acc, calls)
    assert normal.count(canonical_json(safe)) == 1
    # One scalar changes. Reference IDs, call/outcome cohort, alias partition,
    # complete members and canonical format remain intact.
    altered = normal.replace(canonical_json(safe), canonical_json(marker))
    assert canonical_json(json.loads(altered)) == altered
    if route == "data_decode":
        with pytest.raises(CredentialMaterialRefused):
            decode_coupled_batch_data(altered)
    else:
        raw = altered.encode("utf-8")
        # This is untrusted retained input, deliberately constructed without
        # the encoder's admission. The actual nominal reference and its hash
        # are valid: an earlier digest/setup refusal must not mask this test.
        chunk = PrivateChunk(
            PrivateChunkRef(codec="composer.coupled-batch-data.v1", digest=sha256(raw).hexdigest(), byte_length=len(raw)), altered
        )
        verify_private_chunk(chunk.reference, chunk.canonical)
        with pytest.raises(CredentialMaterialRefused):
            decode_coupled_batch_chunk(chunk)
