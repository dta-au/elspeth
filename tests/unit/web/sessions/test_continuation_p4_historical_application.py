"""Actual historical writer/read/projection boundaries, with retained live owners."""

import asyncio
import threading
from dataclasses import replace

import pytest
from sqlalchemy import update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_p4_application as application
from elspeth.web.composer.audit import BufferingRecorder, PrivatePlannerCandidate, begin_dispatch, finish_success
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from elspeth.web.composer.continuation_owed_p4 import materialize_owed_p4_env_inventory
from elspeth.web.composer.continuation_p4_application import apply_selected_p4_to_live_evidence, selected_p4_batch_data
from elspeth.web.composer.continuation_prepared_p4 import prepared_p4_members
from elspeth.web.composer.protocol import RetainedOperationContext
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.sessions.models import chat_messages_table
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.composer.test_audit import _make_planner_attempt
from tests.unit.web.composer.test_continuation_physical_outcomes import actual_record
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_owed_p4_read import committed_case
from tests.unit.web.sessions.test_continuation_p4_owner import recovered, rows

operation_store = test_composer_operation_authority.operation_store


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
    evidence = TurnEvidence(
        session_id=selected.stack.logical.session_id,
        session_operation_context=running.session_operation_context,
        request_id=selected.stack.logical.original_user_message_id,
        request="Preserve café and exact context",
        initial_state_id=data.batch.pre_state_id,
        initial_state=data.batch.entry_state,
        actor_user_id="local",
        retained_operation_context=RetainedOperationContext(running.claim.session_id, "local", "unknown"),
    )
    evidence.transcript.observe_messages([{"role": "user", "content": "retained current request text"}])
    return data, recorder, drained, evidence


async def prepared_application(store, *, kind="graph", monkeypatch=None):
    _engine, _repository, authority, sessions, _sid = store
    running, prepared, commit, audit, selected = await committed_case(
        store,
        graph=kind in ("graph", "revoked"),
        crash_pending=kind in ("crash", "unavailable"),
        unavailable=kind == "unavailable",
        revoked=kind == "revoked",
        monkeypatch=monkeypatch,
    )
    work = inventory_work(running)
    inventory = await materialize_owed_p4_env_inventory(
        advisor=inventory_advisor(sessions), authority=authority, selected=selected, required_work=work, effect_key=commit.effect_key
    )
    data, recorder, drained, evidence = live_projection_owners(running, selected, prepared, inventory.names)
    arguments = {
        "authority": authority,
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
async def test_actual_historical_projection_preserves_graph_history_channels_and_retries_once(operation_store, monkeypatch, kind):
    engine, repository, authority, _sessions, _sid = operation_store
    running, commit, audit, args = await prepared_application(operation_store, kind=kind, monkeypatch=monkeypatch)
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
async def test_actual_application_refuses_changed_owned_input_without_partial_projection(operation_store, change):
    _engine, repository, authority, _sessions, _sid = operation_store
    running, commit, _audit, args = await prepared_application(operation_store)
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
            changed = replace(link, event=link.event.with_display_result("different visible history"))
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
async def test_verification_failure_retains_committed_identity_and_exact_live_comparison_cause(operation_store, monkeypatch):
    _engine, repository, _authority, _sessions, _sid = operation_store
    running, _commit, _audit, args = await prepared_application(operation_store)
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
async def test_retry_rechecks_real_rows_before_returning_live_cached_projection(operation_store):
    engine, repository, _authority, _sessions, sid = operation_store
    running, _commit, _audit, args = await prepared_application(operation_store)
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
async def test_started_read_cancellation_keeps_applied_result_and_original_cancellation(operation_store, monkeypatch):
    _engine, repository, authority, _sessions, _sid = operation_store
    running, _commit, _audit, args = await prepared_application(operation_store)
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
async def test_admission_cancellation_has_no_projection_and_preserves_same_original(operation_store, monkeypatch):
    from elspeth.web import async_workers

    _engine, repository, _authority, _sessions, _sid = operation_store
    running, _commit, _audit, args = await prepared_application(operation_store)
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
async def test_original_current_writer_checkpoint_witness_contract_remains_separate(operation_store):
    _engine, repository, _authority, _sessions, _sid = operation_store
    running, _commit, audit, args = await prepared_application(operation_store)
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
