"""One actual paid call extends recorder-bound journal and joint evidence DATA."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_evidence_application_data as application_data
from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot, encode_recorder_snapshot
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_evidence import admit_turn_evidence, decode_turn_evidence, encode_turn_evidence
from elspeth.web.composer.continuation_message_shelf import encode_message_shelf
from elspeth.web.sessions import continuation_checkpoint_early_return as early_return
from elspeth.web.sessions.continuation_checkpoint_early_owed_overflow import complete_early_checkpoint_or_overflow
from elspeth.web.sessions.continuation_checkpoint_early_return import _expected_shelf
from elspeth.web.sessions.continuation_selection import load_private_chunks
from elspeth.web.sessions.locking import locked_session_transaction
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_checkpoint_early_owed_overflow import _original_h
from tests.unit.web.sessions.test_continuation_primary_result import publish_actual

operation_store = test_composer_operation_authority.operation_store


def _observe_graph_append(actual_encode, before_graph, call, encoded_graphs):
    def encode(graph, *, env_ref_names, recorder_source):
        assert graph.selected is before_graph.selected
        assert graph.effects is before_graph.effects and graph.cuts is before_graph.cuts
        assert graph.recorder.llm_calls[-1] is call
        assert all(old is new for old, new in zip(before_graph.recorder.llm_calls, graph.recorder.llm_calls[:-1], strict=True))
        encoded_graphs.append(graph)
        return actual_encode(graph, env_ref_names=env_ref_names, recorder_source=recorder_source)

    return encode


@pytest.mark.asyncio
async def test_actual_paid_call_rebinds_graph_recorder_and_transcript_without_replacing_history(operation_store, tmp_path, monkeypatch):
    kwargs, _producers, _recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch, post_p4=True)
    running = kwargs["inventory"].running
    try:
        assert result is not None and len(sdk) == 1
        call = result.outcome.audit_call
        assert call is not None and result.buffer.llm_calls[-1] is call
        selected, names = kwargs["selected"], kwargs["inventory"].names
        frame = selected.stack.frames[0]
        old_journal = decode_turn_evidence(
            next(chunk for chunk in selected.chunks if chunk.reference == frame.evidence),
            env_ref_names=names,
            source_chunks=selected.chunks,
        )
        old_joint = kwargs["prior_joint_snapshot"]
        assert old_journal.snapshot.application_entry is not None and old_journal.graph.cuts
        assert old_joint.snapshot.joint_graph_json is not None and not old_joint.graph.cuts

        for evidence in (old_journal, old_joint):
            before_graph = evidence.graph
            assert before_graph is not None
            original_source = evidence.source_chunk
            before = decode_recorder_snapshot(evidence.recorder_chunk)
            extended = replace(before, llm_calls=(*before.llm_calls, call))
            # This is a transcript DATA comparand, not an SDK/publication source.
            shelf = _expected_shelf(evidence.snapshot.transcript, "Checkpoint transcript DATA accompanies the actual paid call.")
            assert (
                encode_message_shelf(shelf, env_ref_names=names).reference
                != encode_message_shelf(evidence.snapshot.transcript, env_ref_names=names).reference
            )
            actual_encode = application_data.encode_application_graph
            encoded_graphs = []

            with monkeypatch.context() as patch:
                patch.setattr(
                    application_data, "encode_application_graph", _observe_graph_append(actual_encode, before_graph, call, encoded_graphs)
                )
                after = application_data.append_call_to_evidence(
                    evidence,
                    call=call,
                    recorder=extended,
                    transcript=shelf,
                    env_ref_names=names,
                    mandatory_retention=True,
                )
            assert len(encoded_graphs) == 1
            assert after.snapshot.transcript == shelf
            assert after.recorder_chunk == encode_recorder_snapshot(extended)
            assert after.snapshot.recorder == after.recorder_chunk.reference != evidence.snapshot.recorder
            assert after.snapshot.effect_locators == evidence.snapshot.effect_locators
            assert after.snapshot.historical_application_count == evidence.snapshot.historical_application_count
            assert after.sources.chunks == evidence.sources.chunks
            if evidence.snapshot.application_entry is not None:
                assert after.snapshot.application_entry.applications == evidence.snapshot.application_entry.applications
                assert after.snapshot.application_entry.graph_json != evidence.snapshot.application_entry.graph_json
                assert after.snapshot.joint_graph_json is None
            else:
                assert after.snapshot.application_entry is None
                assert after.snapshot.joint_graph_json != evidence.snapshot.joint_graph_json
            admitted_again = decode_turn_evidence(after.source_chunk, env_ref_names=names, source_chunks=after.dependency_chunks)
            assert encode_turn_evidence(admitted_again, env_ref_names=names, mandatory_retention=True) == after.source_chunk
            assert len(admitted_again.graph.cuts) == len(before_graph.cuts)
            assert encode_turn_evidence(evidence, env_ref_names=names) == original_source

            # Reproduce exactly the old broken refresh: the real new recorder
            # cannot admit graph text still bound to its real previous source.
            stale = evidence.snapshot.model_copy(update={"recorder": after.snapshot.recorder, "transcript": shelf})
            with pytest.raises(AuditIntegrityError, match="application graph changed its actual original recorder source"):
                admit_turn_evidence(stale, env_ref_names=names, source_chunks=after.dependency_chunks, mandatory_retention=True)
            copied_call = replace(call)
            assert copied_call == call and copied_call is not call
            with pytest.raises(AuditIntegrityError, match="exact original recorder prefix"):
                application_data.append_call_to_evidence(
                    evidence, call=copied_call, recorder=extended, transcript=shelf, env_ref_names=names, mandatory_retention=True
                )
            legacy = application_data.append_primary_call_to_evidence(evidence, call=call, recorder=extended, env_ref_names=names)
            direct = application_data.append_call_to_evidence(evidence, call=call, recorder=extended, env_ref_names=names)
            assert legacy.source_chunk == direct.source_chunk
            assert legacy.snapshot.transcript == evidence.snapshot.transcript
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_early_child_refreshes_complete_applied_batch_closure(operation_store, tmp_path, monkeypatch):
    async with _original_h(operation_store, tmp_path, monkeypatch) as (service, _source, prepared, owed, returned, projected, sdk):
        names = prepared.env_ref_names
        sid, operation = str(prepared.running.claim.session_id), prepared.running.claim.operation_id
        with locked_session_transaction(operation_store[0], sid) as conn:
            original = early_return.read_early_complete_return_on_connection(
                conn, session_id=sid, operation_id=operation, selected=owed.owed_selected, names=names
            )
            root = decode_continuation_snapshot(original.selected.payload_json)
            _before_compose, before_batch = root.frames
            old_applied = early_return._held_chunk(conn, original, before_batch.accumulator)
            prior_chunks = load_private_chunks(conn, session_id=sid, operation_id=operation, stack=root)
            expected_compose, expected_batch, expected_chunks = early_return._expected_child_frames(
                conn, original, retry_pending=projected.retry_pending
            )
        assert projected.captured.stack.frames == (expected_compose, expected_batch)
        applied = next(chunk for chunk in expected_chunks if chunk.reference == expected_batch.accumulator)
        assert applied.reference.codec == "composer.applied-p4-batch.v2"
        assert expected_batch.accumulator != before_batch.accumulator
        assert expected_batch.model_copy(update={"accumulator": before_batch.accumulator}) == before_batch
        assert applied in projected.captured.chunks
        refreshed = decode_turn_evidence(
            next(chunk for chunk in expected_chunks if chunk.reference == expected_compose.evidence),
            env_ref_names=names,
            source_chunks=(*prior_chunks, *expected_chunks),
        )
        assert applied.canonical == refreshed.snapshot.application_entry.graph_json
        application_data.require_selected_applied_batch_chunk(applied, evidence=refreshed, env_ref_names=names)
        with pytest.raises(AuditIntegrityError, match="selected applied batch changed its complete shared evidence graph"):
            application_data.require_selected_applied_batch_chunk(old_applied, evidence=refreshed, env_ref_names=names)

        actual_read = early_return.read_original_early_child_source_on_connection
        cold = []

        def read(conn, **kwargs):
            result = actual_read(conn, **kwargs)
            cold.append(result)
            return result

        monkeypatch.setattr(early_return, "read_original_early_child_source_on_connection", read)
        completed = await complete_early_checkpoint_or_overflow(
            service=service, prepared=prepared, owed=owed, returned=returned, projected=projected
        )
        assert completed.advanced.completed.cursor > original.selected.cursor and sdk == [1]
        assert cold
        for child in cold:
            assert child.owed.selected == original.selected
            assert decode_continuation_snapshot(child.selected.payload_json).frames == (expected_compose, expected_batch)
            with locked_session_transaction(operation_store[0], sid) as conn:
                for chunk in expected_chunks:
                    assert early_return._held_chunk(conn, original, chunk.reference) == chunk
