"""Actual prepared/quiescent graph identities; DATA alone grants no current owner."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_application_graph import ApplicationGraph, decode_application_graph, encode_application_graph
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from elspeth.web.composer.continuation_batch_data import RestoredBatchData, RestoredPreparedBatchData, decode_prepared_batch_data
from elspeth.web.composer.continuation_evidence import decode_turn_evidence, encode_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import _restore_current_evidence, capture_turn_evidence
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.turn_effects import authored_state_hash
from tests.unit.web.composer.test_continuation_batch_data import actual_cut
from tests.unit.web.composer.test_continuation_evidence_graph import rich_evidence
from tests.unit.web.composer.test_continuation_prepared_batch_data import capture, prepared_cut
from tests.unit.web.composer.test_turn_effects import CONTEXT
from tests.unit.web.coordination import test_composer_operation_authority

operation_store = test_composer_operation_authority.operation_store


def active_graph(monkeypatch, *, shared=True):
    evidence, messages, recorder, logical, _policy = rich_evidence()
    with monkeypatch.context() as clock:
        original = prepared_cut(clock, tool="read_attachment", arguments={"blob_id": "00000000-0000-0000-0000-000000000123"})
        encoded = capture(original)
    retained = decode_prepared_batch_data(encoded)
    batch = retained.batch
    initial = batch.state if shared else CompositionState.from_dict(batch.state.to_dict())
    evidence.effects = replace(
        evidence.effects,
        initial_state=initial,
        latest_verified_state=initial,
        initial_authored_state_hash=authored_state_hash(initial),
        latest_authored_state_hash=authored_state_hash(initial),
    )
    recorder.record_private_tool_authoring(batch.private_authorings_by_call[batch.current_call.tool_call.id])
    return evidence, messages, recorder, logical, retained


@pytest.mark.parametrize("shared", (False, True))
def test_complete_active_snapshot_preserves_cross_owner_state_and_authoring_partition(monkeypatch, shared):
    owner, messages, recorder, logical, prepared = active_graph(monkeypatch, shared=shared)
    before = capture_turn_evidence(
        owner,
        messages,
        logical=logical,
        recorder=recorder,
        drained=RecorderDrainCursors(llm_calls=1),
        env_ref_names=frozenset(),
        data=prepared,
    )
    encoded = encode_turn_evidence(before)
    after = decode_turn_evidence(encoded, env_ref_names=frozenset())
    restored = _restore_current_evidence(after, context=CONTEXT)
    assert restored.batch is not None and restored.selected_audit == prepared.audit
    assert restored.selected_dialect is prepared.dialect
    batch = restored.batch.batch
    assert batch.current_call.audit is None
    assert batch.current_call.pre_state is batch.state is restored.batch.accumulator.state
    assert (restored.evidence.effects.initial_state is batch.state) is shared
    assert restored.evidence.effects.initial_state == batch.state
    assert batch.tool_outcomes[0].response is batch.tool_outcomes[1].response
    assert restored.batch.accumulator.authority_revocation == prepared.accumulator.authority_revocation
    assert (
        restored.recorder.snapshot_for_continuation(drained=restored.drained).private_authorings[-1]
        is batch.private_authorings_by_call[batch.current_call.tool_call.id]
    )
    recaptured = capture_turn_evidence(
        restored.evidence,
        restored.messages,
        logical=logical,
        recorder=restored.recorder,
        drained=restored.drained,
        env_ref_names=frozenset(),
        data=RestoredPreparedBatchData(
            batch, restored.batch.accumulator, restored.batch.calls, restored.selected_audit, restored.selected_dialect
        ),
    )
    assert encode_turn_evidence(recaptured) == encoded
    assert restored.evidence._portable_p4_entry is None


def test_quiescent_graph_retains_full_counters_and_aliases_without_invented_audit():
    evidence, _messages, recorder, _logical, _policy = rich_evidence()
    batch, acc, calls = actual_cut()
    graph = ApplicationGraph(
        RestoredBatchData(batch, acc, calls), evidence.effects, recorder.snapshot_for_continuation(drained=RecorderDrainCursors()), ()
    )
    encoded = encode_application_graph(graph, env_ref_names=frozenset())
    restored = decode_application_graph(encoded, env_ref_names=frozenset())
    assert restored.selected_audit is None and restored.selected_dialect is None
    assert restored.selected.batch.next_tool_index == batch.next_tool_index
    assert restored.selected.batch.tool_outcomes[0].response is restored.selected.batch.tool_outcomes[1].response
    assert restored.selected.accumulator.authority_revocation == acc.authority_revocation
    assert encode_application_graph(restored, env_ref_names=frozenset()) == encoded


@pytest.mark.parametrize("change", ("audit", "dialect", "actor", "call_alias", "live_exception"))
def test_active_graph_refuses_independent_broken_owner_after_positive(monkeypatch, change):
    evidence, _messages, recorder, _logical, data = active_graph(monkeypatch)
    graph = ApplicationGraph(
        RestoredBatchData(data.batch, data.accumulator, data.calls),
        evidence.effects,
        recorder.snapshot_for_continuation(drained=RecorderDrainCursors()),
        (),
        data.audit,
        data.dialect,
    )
    assert (
        decode_application_graph(encode_application_graph(graph, env_ref_names=frozenset()), env_ref_names=frozenset()).selected_audit
        == data.audit
    )
    if change == "audit":
        graph = replace(graph, selected_audit=None)
    elif change == "dialect":
        graph = replace(graph, selected_dialect=None)
    elif change == "actor":
        # A live timer retains its exact actor binding independently of the DATA.
        from elspeth.web.composer.continuation_batch_data import readmit_prepared_batch_audit

        call = data.batch.current_call
        call.audit = readmit_prepared_batch_audit(
            data,
            selected_call=call.tool_call,
            selected_pre_state=call.pre_state,
            current_dialect=data.dialect,
            actor=data.audit.actor,
            strict_sent=call.wire.strict_sent,
            wire_conformant=call.wire.wire_conformant,
        )
        graph = replace(graph, selected_audit=data.audit.model_copy(update={"actor": "other-actor"}))
    elif change == "call_alias":
        data.batch.current_call.pre_state = CompositionState.from_dict(data.batch.state.to_dict())
    else:
        data.batch.advisor_failure = RuntimeError("original exception cannot be made portable")
    with pytest.raises(AuditIntegrityError):
        encode_application_graph(graph, env_ref_names=frozenset())


@pytest.mark.asyncio
async def test_active_graph_keeps_actual_prior_p4_cuts_and_recorder_prefix(operation_store, monkeypatch):
    from elspeth.web.composer.continuation_audit_buffer import encode_recorder_snapshot
    from elspeth.web.composer.continuation_evidence_application_data import (
        build_portable_p4_application,
        require_evidence_application_links,
        require_portable_p4_application,
    )
    from tests.unit.web.composer.test_portable_evidence_application import portable_input

    running, _args, values, _audit, _messages = await portable_input(operation_store)
    try:
        bundle = build_portable_p4_application(**values)
        previous = require_evidence_application_links(bundle.evidence_after, env_ref_names=values["env_ref_names"])
        assert previous is not None and previous.cuts
        _owner, _messages, _recorder, _logical, prepared = active_graph(monkeypatch)
        changed = replace(
            previous,
            selected=RestoredBatchData(prepared.batch, prepared.accumulator, prepared.calls),
            selected_audit=prepared.audit,
            selected_dialect=prepared.dialect,
        )
        encoded = encode_application_graph(changed, env_ref_names=values["env_ref_names"])
        restored = decode_application_graph(encoded, env_ref_names=values["env_ref_names"])
        entry = bundle.entry.model_copy(update={"graph_json": encoded})
        require_portable_p4_application(
            entry,
            result_chunk=values["result_chunk"],
            result=values["result"],
            producer=values["producer"],
            env_ref_names=values["env_ref_names"],
        )
        assert entry.applications == bundle.entry.applications
        assert restored.effects is restored.cuts[-1].effects_after
        assert restored.selected.batch.current_call.audit is None
        assert restored.selected_audit == prepared.audit
        for before, after in zip(previous.cuts, restored.cuts, strict=True):
            assert encode_recorder_snapshot(after.recorder_before) == encode_recorder_snapshot(before.recorder_before)
            assert encode_recorder_snapshot(after.recorder_after) == encode_recorder_snapshot(before.recorder_after)
            assert after.data.batch.p4_input_json == before.data.batch.p4_input_json
        assert encode_application_graph(restored, env_ref_names=values["env_ref_names"]) == encoded
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_selected_prepared_and_history_wrappers_bind_complete_graph(operation_store, monkeypatch):
    from elspeth.web.composer.continuation_attachment_result import apply_attachment_history, run_attachment_result
    from elspeth.web.composer.continuation_selected_batch_graph import require_selected_active_batch_graph
    from tests.unit.web.sessions.test_continuation_attachment_result import refreshed, result_case, without_service

    kwargs, _path, _actors, prepared_json = await result_case(operation_store, monkeypatch)
    data = decode_prepared_batch_data(prepared_json, env_ref_names=kwargs["inventory"].names)
    evidence, messages, recorder, _logical, _policy = rich_evidence()
    # This test exercises only the pure selected DATA equality. The fixture's
    # opaque unrelated Compose inputs do not assert current evidence admission.
    snapshot = capture_turn_evidence(
        evidence,
        messages,
        logical=_logical,
        recorder=recorder,
        drained=RecorderDrainCursors(),
        env_ref_names=kwargs["inventory"].names,
        data=data,
    )
    require_selected_active_batch_graph(kwargs["selected"], snapshot, env_ref_names=kwargs["inventory"].names)
    await run_attachment_result(**kwargs)
    kwargs = await refreshed(kwargs)
    await apply_attachment_history(**without_service(kwargs))
    kwargs = await refreshed(kwargs)
    require_selected_active_batch_graph(kwargs["selected"], snapshot, env_ref_names=kwargs["inventory"].names)
    changed = replace(data, batch=replace(data.batch, advisor_calls_used=data.batch.advisor_calls_used + 1))
    other = capture_turn_evidence(
        evidence,
        messages,
        logical=_logical,
        recorder=recorder,
        drained=RecorderDrainCursors(),
        env_ref_names=kwargs["inventory"].names,
        data=changed,
    )
    with pytest.raises(AuditIntegrityError, match="complete shared evidence graph"):
        require_selected_active_batch_graph(kwargs["selected"], other, env_ref_names=kwargs["inventory"].names)
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_quiescent_selected_graph_requires_actual_coupled_outer_codec(operation_store, monkeypatch):
    from elspeth.web.composer.continuation import ToolBatchFrame
    from elspeth.web.composer.continuation_batch_data import encode_coupled_batch_data
    from elspeth.web.composer.continuation_codec import encode_continuation_response, encode_private_chunk
    from elspeth.web.composer.continuation_selected_batch_graph import require_selected_active_batch_graph
    from elspeth.web.composer.continuation_selection import SelectedContinuationState
    from elspeth.web.sessions.continuation_selection import required_private_chunks
    from tests.unit.web.composer.test_continuation_message_family_builders import completion_with_native
    from tests.unit.web.sessions.test_continuation_attachment_result import result_case

    kwargs, _path, _actors, _prepared = await result_case(operation_store, monkeypatch)
    try:
        selected = kwargs["selected"]
        batch, acc, calls = actual_cut()
        data = RestoredBatchData(batch, acc, calls)
        owner, messages, recorder, _logical, _policy = rich_evidence()
        snapshot = capture_turn_evidence(
            owner, messages, logical=_logical, recorder=recorder, drained=RecorderDrainCursors(), env_ref_names=frozenset(), data=data
        )
        payload = encode_coupled_batch_data(batch=batch, accumulator=acc, calls=calls)
        response = encode_continuation_response(completion_with_native(calls=calls))

        def selected_with(codec):
            accumulator = encode_private_chunk(codec=codec, canonical=payload)
            compose = selected.stack.frames[0].model_copy(update={"pending_response": response.reference})
            frame = ToolBatchFrame(
                phase="tool_ready",
                response=response.reference,
                accumulator=accumulator.reference,
                next_tool_index=batch.next_tool_index,
                settled_effect_ids=(),
            )
            root = selected.stack.model_copy(update={"frames": (compose, frame)})
            available = {chunk.reference: chunk for chunk in (*selected.chunks, response, accumulator)}
            # This bounded fixture has no nested End/Advisor frame; the actual
            # SelectedContinuationState still checks its complete closure.
            return SelectedContinuationState(
                selected.checkpoint_id, root, tuple(available[reference] for reference in required_private_chunks(root))
            )

        normal = selected_with("composer.coupled-batch-data.v1")
        require_selected_active_batch_graph(normal, snapshot, env_ref_names=frozenset())
        with pytest.raises(AuditIntegrityError, match="actual selected coupled codec"):
            require_selected_active_batch_graph(selected_with("composer.tool-batch.v1"), snapshot, env_ref_names=frozenset())
    finally:
        operation_store[1].release(kwargs["inventory"].running.session_operation_context)
