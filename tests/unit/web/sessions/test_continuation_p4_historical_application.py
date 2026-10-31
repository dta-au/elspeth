"""Actual historical writer/read/projection boundaries, with retained live owners."""

import asyncio
import threading
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from sqlalchemy import update
from sqlalchemy.exc import OperationalError

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_p4_application as application
from elspeth.web.composer.audit import BufferingRecorder, PrivatePlannerCandidate, begin_dispatch, finish_success
from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from elspeth.web.composer.continuation_batch_data import decode_coupled_batch_chunk, encode_coupled_batch_chunk
from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_evidence import decode_turn_evidence
from elspeth.web.composer.continuation_evidence_application_values import P4EffectProducerData
from elspeth.web.composer.continuation_evidence_graph import _begin_current_evidence
from elspeth.web.composer.continuation_owed_p4 import materialize_owed_p4_env_inventory
from elspeth.web.composer.continuation_p4_application import apply_selected_p4_to_live_evidence, selected_p4_batch_data
from elspeth.web.composer.continuation_p4_batch import decode_p4_pending_batch_chunk
from elspeth.web.composer.continuation_prepared_p4 import decode_prepared_p4, encode_prepared_p4, prepared_p4_members
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.sessions.continuation_p4 import read_p4_transaction_outcome_on_connection
from elspeth.web.sessions.continuation_p4_application_store import StoredP4Application
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table, sessions_table
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.composer.test_audit import _make_planner_attempt
from tests.unit.web.composer.test_continuation_physical_outcomes import actual_record
from tests.unit.web.composer.test_service_failure_dispatch import _issued_review_case, _refuse_service
from tests.unit.web.composer.test_service_failure_publication import _exception_nodes, _finish_issued_batch
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case
from tests.unit.web.sessions.test_continuation_owed_evidence import committed_evidence_case, persist_retained_library_cut
from tests.unit.web.sessions.test_continuation_p4_owner import recovered, rows
from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input
from tests.unit.web.sessions.test_continuation_terminal_p4 import _close_released_lease

operation_store = test_composer_operation_authority.operation_store


def portable_historical_data_owners(running, admitted, prepared, names):
    """Independent historical DATA augmentation, never live/SQL publication.

    The original captured graph and receipt are native anchors. The nominal
    prefix/suffix records below are the old seven-channel codec subject; their
    drained prefix is not installed on a live runtime or claimed as SQL rows.
    """
    graph = admitted.take_graph()
    assert graph is not None and not graph.cuts
    data = graph.selected
    assert data.batch.phase == "p4_pending" and data.batch.service_failure is None
    evidence, _initial, _latest = _begin_current_evidence(admitted.snapshot, context=running.session_operation_context)
    evidence.effects = graph.effects
    assert evidence.effects.initial_state is data.batch.entry_state
    members = prepared_p4_members(prepared, env_ref_names=names)
    recorder = BufferingRecorder()
    physical, _metadata = actual_record()
    recorder.restore_received_llm_call(physical)
    recorder.record_planner_attempt(_make_planner_attempt(1, 1))
    recorder.record_private_candidate(PrivatePlannerCandidate("retained-candidate", "not-dispatched", "list_sources", "{}", "{}", "{}"))
    recorder.record(
        finish_success(
            begin_dispatch("prior-call", "set_metadata", {}, version_before=1, actor="composer", strict_sent=True, wire_conformant=True),
            result_payload={"success": False},
            version_after=1,
        )
    )
    recorder.record_withheld_reply("planner_prose_unadmitted", "prior withheld")
    drained = RecorderDrainCursors(invocations=1, withheld_replies=1)
    for authoring in members.authorings:
        recorder.record_private_tool_authoring(authoring)
    for invocation in members.invocations:
        recorder.record(invocation)
    for invocation, row in zip(recorder.invocations[drained.invocations :], members.redacted_rows, strict=True):
        if row.visible_event is not None:
            recorder.record_visible_tool_event(invocation, row.visible_event)
    for reply in members.withheld_replies:
        recorder.record_withheld_reply(reply.origin, reply.content)
    recorder.record(
        finish_success(
            begin_dispatch("later-call", "set_metadata", {}, version_before=1, actor="composer", strict_sent=True, wire_conformant=True),
            result_payload={"success": False},
            version_after=1,
        )
    )
    recorder.record_withheld_reply("planner_prose_unadmitted", "later withheld")
    return data, evidence, recorder, drained


@asynccontextmanager
async def genuine_portable_case(store, tmp_path, monkeypatch, *, kind):
    """Actual atomic P4 publication; historical DATA augmentation is separate."""
    assert kind in ("graph", "nongraph", "revoked", "crash", "unavailable")
    coded_crash = kind in ("crash", "unavailable")
    descriptor = (
        (
            "ordinary-refused-output",
            "set_output",
            {
                "sink_name": "output",
                "plugin": "database",
                "options": {"table": "events", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
        )
        if kind == "nongraph"
        else ("original-metadata-change", "set_metadata", {"patch": {"name": "after"}})
    )
    case = (
        _issued_review_case(store, tmp_path, monkeypatch)
        if coded_crash
        else _phase_case(store, tmp_path, monkeypatch, tool_call=descriptor)
    )
    async with case as issued:
        patch = monkeypatch
        if coded_crash:
            owner, running, sdk = issued
            original, cause = RuntimeError("original plugin error"), ValueError("original plugin cause")
            entered = _refuse_service(
                store[3], patch, seam="write", original=original, cause=cause, context=running.session_operation_context
            )
        else:
            owner, running, _primary, sdk = issued
        await _advance(owner)
        data = owner.runtime.graph.batch
        assert data is not None and data.batch.phase == "dispatch_ready" and data.batch.service_failure is None
        assert len(data.batch.tool_outcomes) == 1
        if coded_crash:
            assert len(entered) == 1 and owner.failure_custody.original is original and original.__cause__ is cause
            assert data.batch.retained_failure.kind == "plugin_crash"
            assert owner.failure_custody.invocation.status is ComposerToolStatus.PLUGIN_CRASH
        elif kind == "nongraph":
            outcome = data.batch.tool_outcomes[0]
            assert outcome.response.success is False and outcome.response.updated_state is data.batch.entry_state
            assert outcome.response.to_dict()["validation"]["errors"][0]["error_code"] == "plugin_not_enabled"
        else:
            assert data.batch.state.version == data.batch.entry_state.version + 1
            assert data.batch.state.metadata.name == "after"
        # Observe the actual paid response's unpublished prose before P4 capture.
        # The nominal historical prefix/suffix below remain independent DATA.
        assert owner.primary is not None
        prose = owner.primary.outcome.completion.message.content
        assert type(prose) is str and prose
        assert not owner.runtime.graph.recorder.withheld_replies[owner.runtime.drained.withheld_replies :]
        owner.runtime.graph.recorder.record_withheld_reply("planner_prose_unadmitted", prose)
        if kind == "revoked":
            with store[0].begin() as conn:
                conn.execute(
                    update(sessions_table).where(sessions_table.c.id == str(running.claim.session_id)).values(trust_mode="explicit_approve")
                )
        sql_error = None
        if kind == "unavailable":
            sql_error = OperationalError("controlled unavailable", {}, RuntimeError("storage unavailable"))

            def refuse_insert(conn, **kwargs):
                assert kwargs["session_operation_context"] is running.session_operation_context
                store[3]._assert_session_write_lock_held(conn, str(running.claim.session_id), caller="genuine_portable_case.refuse_insert")
                raise sql_error

            patch.setattr(store[3], "_insert_chat_message", refuse_insert)
        observed = await _finish_issued_batch(owner)
        p4 = owner.p4
        assert p4 is not None and p4.prepared is not None and p4.captured is not None and p4.commit is not None
        assert p4.committed_outcome is not None and type(p4.application) is StoredP4Application, tuple(
            (type(error).__name__, str(error)) for error in (_exception_nodes(observed) if observed is not None else ())
        )
        assert p4.application is p4.committed_outcome.application and p4.application is p4.writer_observation.application_candidate
        assert p4.prepared.crash_pending is coded_crash
        assert tuple(reply.content for reply in prepared_p4_members(p4.prepared, env_ref_names=p4.inventory.names).withheld_replies) == (
            prose,
        )
        audit = p4.committed_outcome.audit_outcome
        if coded_crash:
            assert any(error is original for error in _exception_nodes(observed)) and original.__cause__ is cause
        else:
            assert observed is None
        if kind == "unavailable":
            assert audit.unwind_audit_failed and any(error is sql_error for error in _exception_nodes(observed))
        else:
            assert not audit.unwind_audit_failed
        if kind == "revoked":
            assert audit.authority_revocation is not None and audit.checkpoint is None
        elif kind == "graph":
            assert audit.checkpoint is not None and audit.checkpoint.inserted_state_ids
        assert len(sdk) == 1

        def readback():
            with locked_session_transaction(store[0], str(running.claim.session_id)) as conn:
                return read_p4_transaction_outcome_on_connection(conn, p4.writer_observation, env_ref_names=p4.inventory.names)

        assert await asyncio.to_thread(readback) is audit
        # Publication and complete native readback are finished. The remaining
        # codec work owns only historical DATA, so close the real lease now;
        # an existing renewal loss still fails, and the outer close is idempotent.
        await _close_released_lease(store, owner, running)
        bundle = p4.application.bundle
        (record,) = bundle.entry.applications
        compose, batch = p4.captured.stack.frames[0], p4.captured.stack.frames[-1]
        assert type(compose) is ComposeFrame and type(batch) is ToolBatchFrame
        original_chunk = next(chunk for chunk in p4.captured.chunks if chunk.reference == compose.evidence)
        before = decode_turn_evidence(original_chunk, env_ref_names=p4.inventory.names, source_chunks=p4.captured.chunks)
        assert before.recorder_chunk.reference == compose.audit_buffer
        assert before.snapshot.application_entry is None and before.graph.selected.batch.phase == "p4_pending"
        assert bundle.result.receipt.prepared_p4_json == encode_prepared_p4(p4.prepared, env_ref_names=p4.inventory.names)
        assert record.effect_key == p4.commit.effect_key and record.input_sha256 == p4.commit.input_sha256
        assert record.response == batch.response
        # The captured frame retains the completed coupled cut. The journal
        # retains the P4 wrapper around that cut plus its complete prepared input;
        # their closed codecs and references intentionally differ.
        coupled_chunk = next(chunk for chunk in p4.captured.chunks if chunk.reference == batch.accumulator)
        coupled = decode_coupled_batch_chunk(coupled_chunk, env_ref_names=p4.inventory.names)
        pending_chunk = bundle.evidence_after.sources.chunk(record.pending_batch)
        pending = decode_p4_pending_batch_chunk(pending_chunk, env_ref_names=p4.inventory.names)
        assert pending.batch.p4_input_json == bundle.result.receipt.prepared_p4_json
        assert pending.calls == coupled.calls and pending.accumulator.state == coupled.accumulator.state
        assert pending.batch.entry_state == coupled.batch.entry_state and pending.batch.state == coupled.batch.state
        # This is precisely the existing pending codec's projection. Comparing
        # the complete coupled bytes also preserves every field and alias class.
        assert (
            encode_coupled_batch_chunk(
                batch=replace(pending.batch, phase="dispatch_ready", p4_input_json=None),
                accumulator=pending.accumulator,
                calls=pending.calls,
                env_ref_names=p4.inventory.names,
            )
            == coupled_chunk
        )
        for decoded in (coupled, pending):
            assert all(outcome.call is decoded.calls[index] for index, outcome in enumerate(decoded.batch.tool_outcomes))
        assert (
            record.creator_operation_id
            == bundle.result.receipt.creator_operation_id
            == running.session_operation_context.fence.operation_id
        )
        assert record.creator_operation_epoch == running.session_operation_context.fence.operation_epoch
        assert record.creator_sequence == running.claim.segment_sequence and record.creator_generation == running.claim.ownership_generation
        values = {
            "logical": bundle.entry.logical,
            "checkpoint_id": record.checkpoint_id,
            "cursor": record.cursor,
            "response_chunk": bundle.evidence_after.sources.chunk(record.response),
            "pending_batch_chunk": pending_chunk,
            "evidence_snapshot": before,
            "result_chunk": bundle.evidence_after.sources.chunk(record.result),
            "result": bundle.result,
            "producer": P4EffectProducerData(
                record.creator_sequence, record.creator_generation, record.creator_operation_id, record.creator_operation_epoch
            ),
            "env_ref_names": p4.inventory.names,
        }
        yield running, p4.prepared, p4.inventory, audit, values
        assert owner.p4 is p4 and p4.application.bundle is bundle and original_chunk in p4.captured.chunks


async def _prepared_library_case(store, tmp_path, monkeypatch, *, kind):
    """One real library writer and retained pending result on complete bootstrap."""
    assert kind in ("graph", "nongraph", "revoked", "crash", "unavailable")
    runtime, work, _guard_requests = await initialized_primary_input(store, tmp_path, monkeypatch)
    running = work.running
    assert running is not None
    try:
        current = runtime.source.selected
        compose = current.stack.frames[0]
        assert type(compose) is ComposeFrame
        bootstrap = decode_continuation_bootstrap(next(chunk for chunk in current.chunks if chunk.reference == compose.bootstrap))
        assert bootstrap.logical == current.stack.logical
        current_inventory = await materialize_continuation_env_inventory(
            advisor=inventory_advisor(store[3]), authority=store[2], selected=current, required_work=work
        )
        original_graph = runtime.graph
        received, pending, inventory, before, commit = await committed_evidence_case(
            store,
            graph=kind in ("graph", "revoked"),
            joint=True,
            current=(running, current, current_inventory, original_graph),
            materialization=runtime.materialization,
            crash_pending=kind in ("crash", "unavailable"),
            persist_result=False,
        )
        assert received is running and runtime.graph is original_graph
        assert inventory is None and pending.stack.frames[0].bootstrap == compose.bootstrap
        data = selected_p4_batch_data(pending, env_ref_names=current_inventory.names)
        prepared = decode_prepared_p4(data.batch.p4_input_json, env_ref_names=current_inventory.names)
        if kind == "revoked":
            with store[0].begin() as conn:
                conn.execute(
                    update(sessions_table).where(sessions_table.c.id == str(running.claim.session_id)).values(trust_mode="explicit_approve")
                )
        if kind == "unavailable":
            failure = OperationalError("controlled unavailable", {}, RuntimeError("storage unavailable"))
            reached = []

            def refuse_insert(conn, **kwargs):
                assert kwargs["session_operation_context"] is running.session_operation_context
                store[3]._assert_session_write_lock_held(conn, str(running.claim.session_id), caller="_prepared_library_case.refuse_insert")
                reached.append(failure)
                raise failure

            with monkeypatch.context() as patch:
                patch.setattr(store[3], "_insert_chat_message", refuse_insert)
                audit = await asyncio.to_thread(
                    persist_retained_library_cut, store[3], running, prepared, commit, expected_unavailable=True
                )
            assert reached == [failure] and audit.unwind_audit_failed
        else:
            audit = await asyncio.to_thread(persist_retained_library_cut, store[3], running, prepared, commit)
            assert not audit.unwind_audit_failed
        assert (audit.authority_revocation is not None) is (kind == "revoked")
        selected_work = inventory_work(running)
        selected = await load_selected_continuation(
            authority=store[2], running=running, selected=store[2].load_continuation_snapshot(running), required_work=selected_work
        )
        selected_work.coordinator.assert_completed()
        inventory_owner = inventory_work(running)
        inventory = await materialize_owed_p4_env_inventory(
            advisor=inventory_advisor(store[3]),
            authority=store[2],
            selected=selected,
            required_work=inventory_owner,
            effect_key=commit.effect_key,
        )
        inventory_owner.coordinator.assert_completed()
        assert selected.stack.frames[0].bootstrap == compose.bootstrap
        return running, prepared, commit, audit, selected, before, inventory
    except BaseException:
        store[1].release(running.session_operation_context)
        raise


@asynccontextmanager
async def pending_portable_library_case(store, tmp_path, monkeypatch, *, graph):
    """Existing retained library writer/read seam, not atomic live completion."""
    running, prepared, commit, _audit, selected, before, inventory = await _prepared_library_case(
        store, tmp_path, monkeypatch, kind="graph" if graph else "nongraph"
    )
    try:
        result = recovered(store[2], running, commit)
        assert result.receipt.prepared_p4_json == encode_prepared_p4(prepared, env_ref_names=inventory.names)
        frame = selected.stack.frames[-1]
        retained = next(item for item in selected.stack.pending_owner_results if item.effect_key == commit.effect_key)
        values = {
            "logical": selected.stack.logical,
            "checkpoint_id": selected.checkpoint_id,
            "cursor": selected.stack.cursor,
            "response_chunk": next(chunk for chunk in selected.chunks if chunk.reference == frame.response),
            "pending_batch_chunk": next(chunk for chunk in selected.chunks if chunk.reference == frame.accumulator),
            "evidence_snapshot": before,
            "result_chunk": next(chunk for chunk in selected.chunks if chunk.reference == retained.result),
            "result": result,
            "producer": P4EffectProducerData(
                running.claim.segment_sequence,
                running.claim.ownership_generation,
                running.session_operation_context.fence.operation_id,
                running.session_operation_context.fence.operation_epoch,
            ),
            "env_ref_names": inventory.names,
        }
        yield running, selected, prepared, inventory, values
    finally:
        store[1].release(running.session_operation_context)


def live_projection_owners(running, selected, prepared, names):
    data = selected_p4_batch_data(selected, env_ref_names=names)
    members = prepared_p4_members(prepared, env_ref_names=names)
    recorder = BufferingRecorder()
    physical, _metadata = actual_record()
    recorder.restore_received_llm_call(physical)
    recorder.record_planner_attempt(_make_planner_attempt(1, 1))
    recorder.record_private_candidate(PrivatePlannerCandidate("retained-candidate", "not-dispatched", "list_sources", "{}", "{}", "{}"))
    prefix = finish_success(
        begin_dispatch("prior-call", "set_metadata", {}, version_before=1, actor="composer", strict_sent=True, wire_conformant=True),
        result_payload={"success": False},
        version_after=1,
    )
    recorder.record(prefix)
    recorder.record_withheld_reply("planner_prose_unadmitted", "prior withheld")
    drained = RecorderDrainCursors(invocations=1, withheld_replies=1)
    for authoring in members.authorings:
        recorder.record_private_tool_authoring(authoring)
    for invocation in members.invocations:
        recorder.record(invocation)
    recorded = recorder.invocations[drained.invocations :]
    for invocation, row in zip(recorded, members.redacted_rows, strict=True):
        if row.visible_event is not None:
            recorder.record_visible_tool_event(invocation, row.visible_event)
    for reply in members.withheld_replies:
        recorder.record_withheld_reply(reply.origin, reply.content)
    suffix = finish_success(
        begin_dispatch("later-call", "set_metadata", {}, version_before=1, actor="composer", strict_sent=True, wire_conformant=True),
        result_payload={"success": False},
        version_after=1,
    )
    recorder.record(suffix)
    recorder.record_withheld_reply("planner_prose_unadmitted", "later withheld")
    compose = selected.stack.frames[0]
    assert type(compose) is ComposeFrame
    bootstrap = decode_continuation_bootstrap(next(chunk for chunk in selected.chunks if chunk.reference == compose.bootstrap))
    assert bootstrap.logical == selected.stack.logical
    original = bootstrap_values(bootstrap)
    evidence = TurnEvidence(
        session_id=selected.stack.logical.session_id,
        session_operation_context=running.session_operation_context,
        request_id=selected.stack.logical.original_user_message_id,
        request=original.message,
        initial_state_id=data.batch.pre_state_id,
        initial_state=data.batch.entry_state,
        actor_user_id=original.snapshot.actor_user_id,
        retained_operation_context=original.retained_context,
    )
    evidence.transcript.observe_messages([{"role": "user", "content": "retained current request text"}])
    return data, recorder, drained, evidence


async def prepared_application(store, tmp_path, monkeypatch, *, kind="graph"):
    running, prepared, commit, audit, selected, _before, inventory = await _prepared_library_case(store, tmp_path, monkeypatch, kind=kind)
    work = inventory_work(running)
    data, recorder, drained, evidence = live_projection_owners(running, selected, prepared, inventory.names)
    arguments = {
        "authority": store[2],
        "selected": selected,
        "required_work": work,
        "inventory": inventory,
        "data": data,
        "evidence": evidence,
        "recorder": recorder,
        "drained": drained,
    }
    return running, commit, audit, arguments


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("graph", "nongraph", "revoked", "crash", "unavailable"))
async def test_actual_historical_projection_preserves_graph_history_channels_and_retries_once(operation_store, monkeypatch, kind, tmp_path):
    engine, repository, authority, _sessions, _sid = operation_store
    running, commit, audit, args = await prepared_application(operation_store, tmp_path, monkeypatch, kind=kind)
    try:
        evidence, recorder, data = args["evidence"], args["recorder"], args["data"]
        before = evidence.effects
        transcript_owner = evidence.transcript
        transcript = transcript_owner.snapshot()
        history = evidence._retained_history
        current = (evidence.session_id, evidence.operation_id, evidence.request_id, evidence.request_hash)
        snapshot = recorder.snapshot_for_continuation(drained=args["drained"])
        expected = recovered(authority, running, commit)
        calls = []
        actual_read = authority.read_continuation_owed_p4_result

        def observe(*values, **kwargs):
            result = actual_read(*values, **kwargs)
            calls.append(result)
            return result

        monkeypatch.setattr(authority, "read_continuation_owed_p4_result", observe)
        database = rows(engine)
        applied = await apply_selected_p4_to_live_evidence(**args)
        assert applied.result is calls[0] and applied.result == expected
        assert applied.data is data and applied.data.accumulator is data.accumulator
        assert applied.data.batch.tool_outcomes[0].call is data.calls[0]
        if kind != "crash" and kind != "unavailable":
            assert data.batch.tool_outcomes[0].response.updated_state is data.batch.state
        assert applied.recorder is recorder and applied.effects_before is before
        assert applied.effects_after is evidence.effects
        assert evidence.effects.initial_state is before.initial_state
        assert evidence.transcript is transcript_owner and evidence.transcript.snapshot() == transcript
        assert evidence._retained_history == history
        assert (evidence.session_id, evidence.operation_id, evidence.request_id, evidence.request_hash) == current
        assert applied.result.outcome.published_assistant_content == audit.published_assistant_content
        assert applied.result.outcome.authority_revocation_json == expected.outcome.authority_revocation_json
        assert recorder.snapshot_for_continuation(drained=args["drained"]) == snapshot
        after = applied.recorder_after
        assert after.invocations == snapshot.invocations and after.private_authorings == snapshot.private_authorings
        assert after.visible_tool_events == snapshot.visible_tool_events and after.withheld_replies == snapshot.withheld_replies
        assert after.drained.llm_calls == args["drained"].llm_calls
        assert after.drained.planner_attempts == args["drained"].planner_attempts
        assert after.drained.private_candidates == args["drained"].private_candidates
        assert len(after.llm_calls) == len(after.planner_attempts) == len(after.private_candidates) == 1
        assert after.llm_calls == snapshot.llm_calls and after.planner_attempts == snapshot.planner_attempts
        assert after.private_candidates == snapshot.private_candidates
        if kind == "unavailable":
            assert after is applied.recorder_before and evidence.effects is before
        else:
            assert after.drained.invocations == 2 and after.invocations[2].tool_call_id == "later-call"
            assert after.drained.withheld_replies == 2 and after.withheld_replies[2].content == "later withheld"
            assert after.drained.visible_tool_events == after.drained.private_authorings == 1
        if kind == "graph":
            assert evidence.effects.committed_revision_ids == tuple(str(item) for item in audit.checkpoint.inserted_state_ids)
            assert evidence.effects.saved_revision_count == 1 and len(evidence.effects.batches) == 1
            assert evidence.effects.latest_verified_state.metadata.name == "after"
        else:
            assert evidence.effects is before
        args["drained"] = after.drained
        repeated = await apply_selected_p4_to_live_evidence(**args)
        assert repeated is applied and len(calls) == 2
        assert evidence.effects is applied.effects_after and rows(engine) == database
        assert data.batch.phase == "p4_pending" and data.batch.p4 is None
        assert args["required_work"].coordinator.all_completed
        assert all(not ticket.permits_process_drain for ticket in args["required_work"].coordinator.tickets)
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("invocation", "visible", "cursor", "graph", "context", "known_revision", "selected_root", "request"))
async def test_actual_application_refuses_changed_owned_input_without_partial_projection(operation_store, change, tmp_path, monkeypatch):
    _engine, repository, authority, _sessions, _sid = operation_store
    running, commit, _audit, args = await prepared_application(operation_store, tmp_path, monkeypatch)
    try:
        # First prove the same complete real owners are supported.
        positive_args = dict(args)
        data, recorder, drained, evidence = live_projection_owners(
            running, args["selected"], prepared_p4_from_arguments(args), args["inventory"].names
        )
        positive_args.update(data=data, recorder=recorder, drained=drained, evidence=evidence)
        assert (await apply_selected_p4_to_live_evidence(**positive_args)).verification_failure is None
        if change == "invocation":
            snapshot = args["recorder"].snapshot_for_continuation(drained=args["drained"])
            values = list(snapshot.invocations)
            values[1] = replace(values[1], actor="changed-actor")
            args["recorder"], _ = BufferingRecorder.for_continuation(replace(snapshot, invocations=tuple(values)))
        elif change == "visible":
            snapshot = args["recorder"].snapshot_for_continuation(drained=args["drained"])
            link = snapshot.visible_tool_events[0]
            changed = replace(link, event=link.event.with_authority_revocation("different visible history"))
            args["recorder"], _ = BufferingRecorder.for_continuation(replace(snapshot, visible_tool_events=(changed,)))
        elif change == "cursor":
            args["drained"] = replace(args["drained"], invocations=0)
        elif change == "graph":
            args["data"].accumulator.state = args["data"].batch.state
        elif change == "context":
            args["evidence"].operation_id = "foreign-current-context"
        elif change == "request":
            from elspeth.contracts.hashing import stable_hash

            args["evidence"].request_hash = stable_hash("different original request")
        elif change == "selected_root":
            from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
            from elspeth.web.composer.continuation_selection import SelectedContinuationState

            selected = args["selected"]
            changed = selected.stack.model_copy(
                update={"pending_settlement_ids": (*selected.stack.pending_settlement_ids, "different-pending-settlement")}
            )
            valid = decode_continuation_snapshot(encode_continuation_snapshot(changed))
            args["selected"] = SelectedContinuationState(selected.checkpoint_id, valid, selected.chunks)
        else:
            actual = recovered(authority, running, commit)
            args["evidence"].effects = replace(
                args["evidence"].effects, committed_revision_ids=tuple(str(item) for item in actual.receipt.inserted_state_ids)
            )
        before = args["evidence"].effects
        with pytest.raises(AuditIntegrityError):
            await apply_selected_p4_to_live_evidence(**args)
        assert args["evidence"].effects is before
        assert args["evidence"]._historical_p4_applications == {}
        args["required_work"].coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)


def prepared_p4_from_arguments(args):
    from elspeth.web.composer.continuation_prepared_p4 import decode_prepared_p4

    return decode_prepared_p4(args["data"].batch.p4_input_json, env_ref_names=args["inventory"].names)


@pytest.mark.asyncio
async def test_verification_failure_retains_committed_identity_and_exact_live_comparison_cause(operation_store, monkeypatch, tmp_path):
    _engine, repository, _authority, _sessions, _sid = operation_store
    running, _commit, _audit, args = await prepared_application(operation_store, tmp_path, monkeypatch)
    try:
        original = ValueError("actual comparison failed")

        def refuse_comparison(before, after):
            raise original

        monkeypatch.setattr(application, "_changed_fields", refuse_comparison)
        applied = await apply_selected_p4_to_live_evidence(**args)
        assert applied.verification_failure.__cause__ is original
        assert args["evidence"].effects.verification == "unavailable"
        assert args["evidence"].effects.saved_revision_count == 1
        assert applied.recorder_after.drained.invocations == 2
        args["drained"] = applied.recorder_after.drained
        assert await apply_selected_p4_to_live_evidence(**args) is applied
        assert applied.verification_failure.__cause__ is original
        assert args["evidence"].effects.saved_revision_count == 1
        args["required_work"].coordinator.assert_completed()
        assert any(item.original_root is applied.verification_failure for item in args["required_work"].coordinator.failure_receipts())
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_retry_rechecks_real_rows_before_returning_live_cached_projection(operation_store, tmp_path, monkeypatch):
    engine, repository, _authority, _sessions, sid = operation_store
    running, _commit, _audit, args = await prepared_application(operation_store, tmp_path, monkeypatch)
    try:
        applied = await apply_selected_p4_to_live_evidence(**args)
        args["drained"] = applied.recorder_after.drained
        with engine.begin() as conn:
            conn.execute(
                update(chat_messages_table)
                .where(chat_messages_table.c.session_id == str(sid), chat_messages_table.c.role == "assistant")
                .values(raw_content="tampered original raw")
            )
        with pytest.raises(AuditIntegrityError):
            await apply_selected_p4_to_live_evidence(**args)
        assert args["evidence"].effects is applied.effects_after
        assert args["evidence"].effects.saved_revision_count == 1
        args["required_work"].coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_started_read_cancellation_keeps_applied_result_and_original_cancellation(operation_store, monkeypatch, tmp_path):
    _engine, repository, authority, _sessions, _sid = operation_store
    running, _commit, _audit, args = await prepared_application(operation_store, tmp_path, monkeypatch)
    release = threading.Event()
    try:
        entered = threading.Event()
        actual = authority.read_continuation_owed_p4_result

        def hold(*values, **kwargs):
            entered.set()
            assert release.wait(5)
            return actual(*values, **kwargs)

        monkeypatch.setattr(authority, "read_continuation_owed_p4_result", hold)
        originals = []
        original_finish = application.run_required_sql_finish_once

        async def observe(*values, **kwargs):
            outcome = await original_finish(*values, **kwargs)
            originals.extend(outcome.deferred_cancellations)
            return outcome

        monkeypatch.setattr(application, "run_required_sql_finish_once", observe)
        task = asyncio.create_task(apply_selected_p4_to_live_evidence(**args))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel("historical application after actual SQL start")
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert len(originals) == 1 and caught.value is originals[0]
        assert args["evidence"].effects.saved_revision_count == 1
        applied = next(iter(args["evidence"]._historical_p4_applications.values()))
        args["drained"] = applied.recorder_after.drained
        assert await apply_selected_p4_to_live_evidence(**args) is applied
        args["required_work"].coordinator.assert_completed()
    finally:
        release.set()
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_admission_cancellation_has_no_projection_and_preserves_same_original(operation_store, monkeypatch, tmp_path):
    from elspeth.web import async_workers

    _engine, repository, _authority, _sessions, _sid = operation_store
    running, _commit, _audit, args = await prepared_application(operation_store, tmp_path, monkeypatch)
    try:
        actual = async_workers._acquire_admission
        entered = asyncio.Event()
        originals = []

        async def observe():
            entered.set()
            try:
                await actual()
            except asyncio.CancelledError as original:
                originals.append(original)
                raise

        monkeypatch.setattr(async_workers, "ADMISSION_CAPACITY", 0)
        monkeypatch.setattr(async_workers, "ADMISSION_WAIT_SECONDS", 30.0)
        monkeypatch.setattr(async_workers, "_acquire_admission", observe)
        before = args["evidence"].effects
        task = asyncio.create_task(apply_selected_p4_to_live_evidence(**args))
        try:
            await asyncio.wait_for(entered.wait(), 5)
        finally:
            task.cancel("historical application admission wait")
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert len(originals) == 1 and caught.value is originals[0]
        assert args["evidence"].effects is before
        assert args["evidence"]._historical_p4_applications == {}
        args["required_work"].coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_original_current_writer_checkpoint_witness_contract_remains_separate(operation_store, tmp_path, monkeypatch):
    _engine, repository, _authority, _sessions, _sid = operation_store
    running, _commit, audit, args = await prepared_application(operation_store, tmp_path, monkeypatch)
    try:
        evidence, data = args["evidence"], args["data"]
        evidence.accept_checkpoint(
            audit.checkpoint,
            expected_state=data.batch.state,
            tool_call_ids=tuple(call.id for call in data.calls),
            assistant_message_id=audit.assistant_id,
        )
        assert evidence.effects.saved_revision_count == 1
        assert evidence._historical_p4_applications == {}
        with pytest.raises(AuditIntegrityError, match="cannot be counted twice"):
            evidence.accept_checkpoint(
                audit.checkpoint,
                expected_state=data.batch.state,
                tool_call_ids=tuple(call.id for call in data.calls),
                assistant_message_id=audit.assistant_id,
            )
        assert evidence.effects.saved_revision_count == 1
        assert evidence.effects.verification == "unavailable"
        args["required_work"].coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)
