"""Pure DATA oracles supplied by real original P4 writer/read owner fixtures.

The setup uses existing SQLite owners; none of these tests grants current
application authority from a decoded value or implements an atomic successor.
"""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_p4_application as live_application
from elspeth.web.composer.continuation_application_graph import decode_application_graph, encode_application_graph
from elspeth.web.composer.continuation_codec import encode_private_chunk
from elspeth.web.composer.continuation_evidence import decode_turn_evidence, encode_turn_evidence
from elspeth.web.composer.continuation_evidence_application_data import (
    build_portable_p4_application,
    require_portable_p4_application,
    retain_portable_application_on_live_evidence,
)
from elspeth.web.composer.continuation_evidence_application_values import P4EffectProducerData
from elspeth.web.composer.continuation_evidence_graph import _restore_current_evidence, capture_turn_evidence
from elspeth.web.composer.state import CompositionState
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_p4_historical_application import prepared_application
from tests.unit.web.sessions.test_continuation_p4_owner import recovered

operation_store = test_composer_operation_authority.operation_store


async def portable_input(store, *, kind="graph", monkeypatch=None, share_authoring=True):
    running, commit, audit, args = await prepared_application(store, kind=kind, monkeypatch=monkeypatch)
    selected, data, evidence, recorder = args["selected"], args["data"], args["evidence"], args["recorder"]
    if share_authoring:
        # Actual nominal objects, not equal-value reconstruction. The original
        # independent batch codec has the same bytes; this new bridge retains
        # whether the producer shared its authoring with the recorder.
        for item in recorder.snapshot_for_continuation(drained=args["drained"]).private_authorings:
            if item.tool_call_id in data.batch.private_authorings_by_call:
                data.batch.private_authorings_by_call[item.tool_call_id] = item
    messages = [{"role": "user", "content": "retained current request text"}]
    evidence.transcript.observe_messages(messages)
    snapshot = capture_turn_evidence(
        evidence,
        messages,
        logical=selected.stack.logical,
        recorder=recorder,
        drained=args["drained"],
        env_ref_names=args["inventory"].names,
        data=data,
    )
    frame = selected.stack.frames[-1]
    response = next(item for item in selected.chunks if item.reference == frame.response)
    pending = next(item for item in selected.chunks if item.reference == frame.accumulator)
    result_ref = next(item.result for item in selected.stack.pending_owner_results if item.effect_key == commit.effect_key)
    result_chunk = next(item for item in selected.chunks if item.reference == result_ref)
    result = recovered(store[2], running, commit)
    producer = P4EffectProducerData(
        running.claim.segment_sequence,
        running.claim.ownership_generation,
        running.session_operation_context.fence.operation_id,
        running.session_operation_context.fence.operation_epoch,
    )
    values = {
        "logical": selected.stack.logical,
        "checkpoint_id": selected.checkpoint_id,
        "cursor": selected.stack.cursor,
        "response_chunk": response,
        "pending_batch_chunk": pending,
        "evidence_snapshot": snapshot,
        "result_chunk": result_chunk,
        "result": result,
        "producer": producer,
        "env_ref_names": args["inventory"].names,
    }
    return running, args, values, audit, messages


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("graph", "nongraph", "revoked", "crash", "unavailable"))
async def test_complete_portable_projection_retains_real_outcome_all_channels_and_shared_graph(operation_store, monkeypatch, kind):
    running, args, values, audit, _messages = await portable_input(operation_store, kind=kind, monkeypatch=monkeypatch)
    try:
        before = args["evidence"].effects
        bundle = build_portable_p4_application(**values)
        assert args["evidence"].effects is before and args["evidence"]._historical_p4_applications == {}
        assert bundle.batch_after.batch.phase == "complete" and bundle.batch_after.batch.p4 is None
        assert bundle.batch_after.batch.p4_receipt_json == values["result_chunk"].canonical
        entry = bundle.entry.applications[-1]
        assert entry.result == values["result_chunk"].reference
        assert entry.creator_operation_id == values["producer"].creator_operation_id
        if kind == "graph":
            assert entry.creator_operation_id == audit.checkpoint.creator_operation_id
        assert entry.prepared_input_json == values["result"].receipt.prepared_p4_json
        require_portable_p4_application(
            bundle.entry,
            result_chunk=values["result_chunk"],
            result=values["result"],
            producer=values["producer"],
            env_ref_names=values["env_ref_names"],
        )
        decoded = decode_turn_evidence(bundle.evidence_chunk, env_ref_names=values["env_ref_names"])
        restored = _restore_current_evidence(decoded, context=running.session_operation_context, env_ref_names=values["env_ref_names"])
        assert restored.batch is not None and restored.applications == bundle.entry
        assert restored.evidence._historical_p4_applications == {}
        assert restored.evidence._portable_p4_graph.selected is restored.batch
        graph = restored.evidence._portable_p4_graph
        assert restored.evidence.effects is graph.effects
        cut = graph.cuts[-1]
        assert graph.effects is cut.effects_after
        assert cut.effects_before.initial_state is cut.data.batch.entry_state
        assert graph.effects.initial_state is cut.effects_before.initial_state
        assert restored.batch.accumulator is cut.data.accumulator
        assert restored.batch.calls is cut.data.calls
        assert restored.batch.batch.tool_outcomes is cut.data.batch.tool_outcomes
        call_id = restored.batch.calls[0].id
        authoring = restored.batch.batch.private_authorings_by_call[call_id]
        assert graph.recorder.private_authorings[0] is authoring
        assert graph.recorder.invocations[1].private_authoring is authoring
        assert restored.recorder.visible_tool_events[0][0] is restored.recorder.invocations[1]
        assert len(graph.recorder.llm_calls) == len(graph.recorder.planner_attempts) == len(graph.recorder.private_candidates) == 1
        assert graph.recorder.drained.llm_calls == graph.recorder.drained.planner_attempts == graph.recorder.drained.private_candidates == 0
        if kind == "unavailable":
            assert cut.recorder_after is cut.recorder_before and graph.effects is cut.effects_before
            assert graph.recorder.drained.invocations == 1
        else:
            assert graph.recorder.drained.invocations == graph.recorder.drained.withheld_replies == 2
            assert graph.recorder.drained.visible_tool_events == graph.recorder.drained.private_authorings == 1
        assert encode_application_graph(graph, env_ref_names=values["env_ref_names"]) == bundle.entry.graph_json
        assert encode_turn_evidence(decoded, env_ref_names=values["env_ref_names"]) == bundle.evidence_chunk
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_joint_capture_retains_distinct_equal_state_and_authoring_objects(operation_store):
    running, args, values, _audit, messages = await portable_input(operation_store, share_authoring=False)
    try:
        original = args["evidence"].effects.initial_state
        distinct = CompositionState.from_dict(original.to_dict())
        assert distinct == original and distinct is not original
        args["evidence"].effects = replace(args["evidence"].effects, latest_verified_state=distinct)
        values["evidence_snapshot"] = capture_turn_evidence(
            args["evidence"],
            messages,
            logical=values["logical"],
            recorder=args["recorder"],
            drained=args["drained"],
            env_ref_names=values["env_ref_names"],
            data=args["data"],
        )
        bundle = build_portable_p4_application(**values)
        graph = decode_application_graph(bundle.entry.graph_json, env_ref_names=values["env_ref_names"])
        before = graph.cuts[0].effects_before
        assert before.initial_state == before.latest_verified_state and before.initial_state is not before.latest_verified_state
        authoring = graph.selected.batch.private_authorings_by_call[graph.selected.calls[0].id]
        assert authoring == graph.recorder.private_authorings[0] and authoring is not graph.recorder.private_authorings[0]
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ("producer", "result", "input", "drain", "failure", "alias", "accumulator", "missing_joint", "duplicate")
)
async def test_portable_admission_refuses_one_changed_fact_after_complete_positive(operation_store, change):
    running, _args, values, _audit, _messages = await portable_input(operation_store)
    try:
        bundle = build_portable_p4_application(**values)
        require_portable_p4_application(
            bundle.entry,
            result_chunk=values["result_chunk"],
            result=values["result"],
            producer=values["producer"],
            env_ref_names=values["env_ref_names"],
        )
        check = {
            "entry": bundle.entry,
            "result_chunk": values["result_chunk"],
            "result": values["result"],
            "producer": values["producer"],
            "env_ref_names": values["env_ref_names"],
        }
        if change == "producer":
            check["producer"] = replace(values["producer"], creator_generation=values["producer"].creator_generation + 1)
        elif change == "result":
            check["result_chunk"] = encode_private_chunk(codec="composer.owner-result.v1", canonical="{}")
        elif change == "input":
            record = bundle.entry.applications[0].model_copy(update={"prepared_input_json": "{}"})
            check["entry"] = bundle.entry.model_copy(update={"applications": (record,)})
        elif change in ("drain", "alias", "accumulator"):
            graph = decode_application_graph(bundle.entry.graph_json, env_ref_names=values["env_ref_names"])
            cut = graph.cuts[0]
            if change == "drain":
                changed = replace(cut.recorder_after, drained=replace(cut.recorder_after.drained, invocations=1))
                cut = replace(cut, recorder_after=changed)
                graph = replace(graph, recorder=changed, cuts=(cut,))
            elif change == "accumulator":
                graph = replace(graph, selected=replace(graph.selected, accumulator=replace(graph.selected.accumulator)))
            else:
                equal = CompositionState.from_dict(cut.effects_after.initial_state.to_dict())
                changed = replace(cut.effects_after, initial_state=equal)
                cut = replace(cut, effects_after=changed)
                graph = replace(graph, effects=changed, cuts=(cut,))
            check["entry"] = bundle.entry.model_copy(
                update={"graph_json": encode_application_graph(graph, env_ref_names=values["env_ref_names"])}
            )
        elif change == "failure":
            record = bundle.entry.applications[0].model_copy(update={"verification": "authored_state_mismatch"})
            check["entry"] = bundle.entry.model_copy(update={"applications": (record,)})
        elif change == "missing_joint":
            values["evidence_snapshot"] = values["evidence_snapshot"].model_copy(update={"joint_graph_json": None})
            with pytest.raises(AuditIntegrityError, match="producer-captured"):
                build_portable_p4_application(**values)
            return
        else:
            check["entry"] = bundle.entry.model_copy(update={"applications": bundle.entry.applications * 2})
        with pytest.raises((AuditIntegrityError, ValueError)):
            require_portable_p4_application(**check)
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_known_verification_failure_data_preserves_original_live_exception_and_cause(operation_store, monkeypatch):
    running, args, values, _audit, messages = await portable_input(operation_store)
    try:
        original = ValueError("controlled original comparison failure")

        def refuse_comparison(before, after):
            raise original

        monkeypatch.setattr(live_application, "_changed_fields", refuse_comparison)
        bundle = build_portable_p4_application(**values)
        assert bundle.entry.applications[-1].verification == "checkpoint_decode_or_compare_failed"
        assert bundle.evidence_after.effects.verification == "unavailable"
        assert len(bundle.evidence_after.effects.committed_revision_ids) == 1
        applied = await live_application.apply_selected_p4_to_live_evidence(**args)
        assert applied.verification_failure.__cause__ is original
        actual_exception = applied.verification_failure
        after_data = retain_portable_application_on_live_evidence(
            owner=args["evidence"], recorder=args["recorder"], application=applied, bundle=bundle, env_ref_names=values["env_ref_names"]
        )
        assert applied.verification_failure is actual_exception and actual_exception.__cause__ is original
        retained = capture_turn_evidence(
            args["evidence"],
            messages,
            logical=values["logical"],
            recorder=args["recorder"],
            drained=applied.recorder_after.drained,
            env_ref_names=values["env_ref_names"],
            data=after_data,
        )
        assert retained.application_entry.applications == bundle.entry.applications
        restored = _restore_current_evidence(retained, context=running.session_operation_context, env_ref_names=values["env_ref_names"])
        assert restored.evidence._historical_p4_applications == {}
        assert restored.batch.batch.p4 is None and restored.batch.batch.p4_verification_failed
        assert args["evidence"]._historical_p4_applications[applied.effect_key] is applied
        wrong = bundle.entry.applications[-1].model_copy(update={"verification": "authored_state_mismatch"})
        changed = bundle.entry.model_copy(update={"applications": (wrong,)})
        with pytest.raises(AuditIntegrityError, match="actual historical projection"):
            require_portable_p4_application(
                changed,
                result_chunk=values["result_chunk"],
                result=values["result"],
                producer=values["producer"],
                env_ref_names=values["env_ref_names"],
            )
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_unjournaled_warm_application_is_not_silently_cleared(operation_store):
    running, args, values, _audit, messages = await portable_input(operation_store, kind="nongraph")
    try:
        applied = await live_application.apply_selected_p4_to_live_evidence(**args)
        with pytest.raises(AuditIntegrityError, match="warm applications"):
            capture_turn_evidence(
                args["evidence"],
                messages,
                logical=values["logical"],
                recorder=args["recorder"],
                drained=applied.recorder_after.drained,
                env_ref_names=values["env_ref_names"],
            )
        assert args["evidence"]._historical_p4_applications[applied.effect_key] is applied
        assert args["evidence"].effects is applied.effects_after
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_selected_chunk_and_new_metadata_domains_are_admitted_without_authority(operation_store):
    from elspeth.web.composer.continuation_evidence_application_data import require_selected_applied_batch_chunk
    from elspeth.web.credential_guard import CredentialMaterialRefused

    running, _args, values, _audit, _messages = await portable_input(operation_store)
    try:
        bundle = build_portable_p4_application(**values)
        require_selected_applied_batch_chunk(bundle.batch_chunk, evidence=bundle.evidence_after, env_ref_names=values["env_ref_names"])
        changed = bundle.entry.applications[-1].model_copy(
            update={"checkpoint_id": "postgresql://user:private@host/database"}  # secret-scan: allow-this-line
        )
        with pytest.raises(CredentialMaterialRefused):
            require_portable_p4_application(
                bundle.entry.model_copy(update={"applications": (changed,)}),
                result_chunk=values["result_chunk"],
                result=values["result"],
                producer=values["producer"],
                env_ref_names=values["env_ref_names"],
            )
        with pytest.raises(AuditIntegrityError, match="another actual codec"):
            require_selected_applied_batch_chunk(
                values["pending_batch_chunk"], evidence=bundle.evidence_after, env_ref_names=values["env_ref_names"]
            )
    finally:
        operation_store[1].release(running.session_operation_context)
