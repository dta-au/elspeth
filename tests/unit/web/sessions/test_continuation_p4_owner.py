"""Actual P4 transaction/result owners; the selected batch is a storage fixture.

No fixture claims a provider call happened. These tests start at the explicit
P4 input seam and use real session admission, lease, SQL, complete snapshots,
actual nominal results/audit, and the production writer/receipt reader.
"""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from elspeth.contracts.composer_audit import PrivateToolAuthoring
from elspeth.contracts.composer_mutation import ComposerMutationRevocation
from elspeth.contracts.errors import AuditIntegrityError, FailedTurnMetadata
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.token_usage import TokenUsage
from elspeth.web.composer._compose_loop_carriers import (
    _AdmittedAssistantMessage,
    _AdmittedLLMCompletion,
    _AdmittedLLMProviderMetadata,
    _AdmittedToolBatch,
    _AdmittedToolCall,
    _AdmittedToolFunction,
    _PersistOutcome,
    _ToolOutcome,
)
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory, AdvisorTranscriptOwner
from elspeth.web.composer.audit import begin_dispatch, finish_plugin_crash, finish_success
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.continuation import ToolBatchFrame
from elspeth.web.composer.continuation_batch_data import encode_coupled_batch_chunk
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_response, encode_continuation_snapshot
from elspeth.web.composer.continuation_p4_batch import decode_p4_pending_batch_chunk, encode_p4_pending_batch_chunk
from elspeth.web.composer.continuation_p4_outcome import capture_p4_outcome, decode_p4_outcome, encode_p4_outcome, require_same_p4_outcome
from elspeth.web.composer.continuation_prepared_p4 import capture_prepared_p4, encode_prepared_p4, prepared_p4_members
from elspeth.web.composer.continuation_tool_batch import RetainedBatchFailure
from elspeth.web.composer.diagnostic_evidence import build_private_tool_evidence, diagnostic_evidence_identity
from elspeth.web.composer.state import CompositionState, ValidationSummary
from elspeth.web.composer.tool_batch import BatchAccumulator, ContinuationBatchState
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.turn_audit import _state_payload_for_compose_turn
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.composer.withheld_replies import WithheldReply
from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions._persist_payload import AuditOutcome, RedactedToolRow, RejectionRecord
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    append_complete_snapshot,
    private_chunk_key,
    put_chunk,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import (
    chat_messages_table,
    composer_continuation_effects_table,
    composer_operation_segments_table,
    composition_rejection_events_table,
    composition_states_table,
    session_operation_fences_table,
    sessions_table,
)
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_cold_restore import admit_continuation, start_continuation

operation_store = test_composer_operation_authority.operation_store


def completed_cut(
    running,
    stack,
    *,
    graph=False,
    crash_pending=False,
    initial_state=None,
    call_id="owned-p4-call",
    current_state_id=None,
    prior_withheld_replies=(),
):
    """Actual completed owners, retaining state/result aliases and source audit."""
    context = running.session_operation_context
    before = (
        initial_state
        if initial_state is not None
        else CompositionState.from_dict(
            {"version": 1, "sources": {}, "nodes": [], "edges": [], "outputs": [], "metadata": {"name": "before", "description": ""}}
        )
    )
    after = (
        CompositionState.from_dict(
            {"version": 2, "sources": {}, "nodes": [], "edges": [], "outputs": [], "metadata": {"name": "after", "description": ""}}
        )
        if graph
        else before
    )
    validation = ValidationSummary(False, ())
    arguments = {"patch": {"name": "after"}}
    raw = canonical_json(arguments)
    call = _AdmittedToolCall(call_id, _AdmittedToolFunction("set_metadata", raw))
    result = ToolResult(graph, after, validation, (), prior_validation=validation)
    outcome = _ToolOutcome(call, result, None, None, None, before.version, after.version, True, True)
    authoring = PrivateToolAuthoring(call.id, call.function.name, raw, None, raw)
    audit = finish_success(
        begin_dispatch(
            call.id, call.function.name, arguments, version_before=before.version, actor="composer", strict_sent=True, wire_conformant=True
        ),
        result_payload=result.to_dict(),
        version_after=after.version,
    )
    if crash_pending:
        assert not graph
        original = RuntimeError("original plugin error")
        audit = finish_plugin_crash(
            begin_dispatch(
                call.id,
                call.function.name,
                arguments,
                version_before=before.version,
                actor="composer",
                strict_sent=True,
                wire_conformant=True,
            ),
            exc=original,
        )
        outcome = _ToolOutcome(call, None, "RuntimeError", None, "RuntimeError", before.version, before.version, True, True)
    audit = replace(audit, private_authoring=authoring)
    display = canonical_json({"success": False, "error_class": "RuntimeError"}) if crash_pending else canonical_json(result.to_dict())
    visible = VisibleToolEvent.capture(
        context=context,
        request_id=stack.logical.original_user_message_id,
        tool_call_id=call.id,
        tool_name=call.function.name,
        received_arguments=raw,
        effective_arguments=raw,
        selected_result=display,
        display_arguments=raw,
        display_result=display,
    )
    row = RedactedToolRow(call.id, display, _state_payload_for_compose_turn(result) if graph else None, visible)
    rows = (row,)
    refusal = canonical_json({"success": False, "error": "automatic mutation refused"})
    revoked = (
        (replace(row, content=refusal, composition_state_payload=None, visible_event=visible.with_authority_revocation(refusal)),)
        if graph
        else rows
    )
    evidence = build_private_tool_evidence(
        session_id=context.fence.session_id,
        operation_id=context.fence.operation_id,
        operation_epoch=context.fence.operation_epoch,
        tool_call_id=call.id,
        tool_name=call.function.name,
        status=audit.status.value,
        arguments_canonical=raw,
        result_canonical=audit.result_canonical,
        raw_arguments=raw,
    )
    prepared = capture_prepared_p4(
        context=context,
        assistant_content="retained prose",
        raw_content="original prose",
        redacted_calls=(
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": raw},
                "strict_sent": True,
                "wire_conformant": True,
            },
        ),
        redacted_rows=rows,
        revoked_rows=revoked,
        mutation_ceiling=stack.mutation_ceiling.materialize(),
        authority_revocation=None,
        private_tool_evidence=(evidence,),
        private_tool_failures=(
            ()
            if audit.private_failure is None
            else (
                build_private_tool_evidence(
                    session_id=context.fence.session_id,
                    operation_id=context.fence.operation_id,
                    operation_epoch=context.fence.operation_epoch,
                    tool_call_id=call.id,
                    tool_name="composer_failure",
                    status="tool_failure",
                    arguments_canonical=None,
                    result_canonical=audit.private_failure.canonical,
                    evidence_id=diagnostic_evidence_identity("tool-failure", call.id),
                ),
            )
        ),
        rejection_records=() if graph else (RejectionRecord(call.id, call.function.name, "policy_refused", "retained refusal", display),),
        withheld_replies=(*prior_withheld_replies, WithheldReply("planner_prose_unadmitted", "unpublished words")),
        invocations=(audit,),
        authorings=(authoring,),
        parent_composition_state_id=current_state_id,
        expected_current_state_id=current_state_id,
        assistant_row_uses_current_dispatch=True,
        crash_pending=crash_pending,
    )
    acc = BatchAccumulator(before, validation, None, 0)
    batch = ContinuationBatchState(
        after,
        validation,
        None,
        0,
        entry_state=before,
        pre_state_id=current_state_id,
        phase="dispatch_ready",
        next_tool_index=1,
        assistant_message_index=0,
        provider_model_version="owned-model",
        turn_has_mutation=graph,
        all_cache_hits=False,
        tool_outcomes=[outcome],
        decoded_args_by_call_id={call.id: arguments},
        private_authorings_by_call={call.id: authoring},
        mutation_success_observed=graph,
        retained_failure=RetainedBatchFailure(kind="plugin_crash", error_class="RuntimeError") if crash_pending else None,
    )
    completion = _AdmittedLLMCompletion(
        _AdmittedAssistantMessage("retained prose"),
        _AdmittedToolBatch((call,), frozenset({call.id})),
        _AdmittedLLMProviderMetadata(
            "owned-model",
            "tool_calls",
            TokenUsage(prompt_tokens=10, completion_tokens=5),
            None,
            "not_available",
            "provider-request",
            None,
            None,
            None,
            "test",
        ),
    )
    return batch, acc, completion, prepared


def selected_p4(store, *, graph=False, crash_pending=False):
    engine, repository, authority, _service, sid = store
    _record, _seed, _action = admit_continuation(store)
    running = start_continuation(repository, authority)
    head = authority.load_continuation_snapshot(running)
    stack = decode_continuation_snapshot(head.payload_json)
    batch, acc, response, prepared = completed_cut(running, stack, graph=graph, crash_pending=crash_pending)
    batch_chunk = encode_coupled_batch_chunk(batch=batch, accumulator=acc, calls=response.tool_batch.calls)
    response_chunk = encode_continuation_response(response)
    # Direct storage setup is restricted to the selected input boundary. All
    # SQL preparation, mutation, publication, root CAS and recovery below use
    # the actual production owners, with no guard patched out.
    with locked_session_transaction(engine, str(sid)) as conn:
        for chunk in (batch_chunk, response_chunk):
            put_chunk(
                conn,
                session_id=str(sid),
                operation_id=running.claim.operation_id,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode(),
                codec=chunk.reference.codec,
            )
        selected = stack.model_copy(
            update={
                "frames": (
                    *stack.frames,
                    ToolBatchFrame(
                        phase="tool_ready",
                        response=response_chunk.reference,
                        accumulator=batch_chunk.reference,
                        next_tool_index=1,
                        settled_effect_ids=(),
                    ),
                )
            }
        )
        head = append_complete_snapshot(
            conn,
            session_id=str(sid),
            operation_id=running.claim.operation_id,
            expected_checkpoint_id=head.checkpoint_id,
            expected_cursor=head.cursor,
            snapshot_json=encode_continuation_snapshot(selected),
            event_batch=(ContinuationEvent("tool_prepared", canonical_json({"fixture": "selected P4 input"})),),
            handoff=False,
        )
    pending = replace(batch, phase="p4_pending", p4_input_json=encode_prepared_p4(prepared))
    chunk = encode_p4_pending_batch_chunk(batch=pending, accumulator=acc, calls=response.tool_batch.calls)
    restored = decode_p4_pending_batch_chunk(chunk)
    assert restored.batch.entry_state is restored.accumulator.state
    if crash_pending:
        assert restored.batch.tool_outcomes[0].response is None
        assert restored.batch.retained_failure.kind == "plugin_crash"
        assert restored.batch.plugin_crash is None and restored.batch.plugin_crash_cause is None
    else:
        assert restored.batch.tool_outcomes[0].response.updated_state is restored.batch.state
    assert restored.batch.tool_outcomes[0].call is restored.calls[0]
    assert (restored.batch.state is restored.accumulator.state) is (not graph)
    commit = authority.prepare_continuation_p4(
        running, expected_checkpoint_id=head.checkpoint_id, expected_cursor=head.cursor, pending_batch=chunk
    )
    return running, prepared, commit


def persist(service, running, prepared, commit):
    members = prepared_p4_members(prepared)
    return service.persist_compose_turn(
        session_id=str(running.claim.session_id),
        writer_principal="compose_loop",
        session_operation_context=running.session_operation_context,
        assistant_content=prepared.assistant_content,
        raw_content=prepared.raw_content,
        redacted_assistant_tool_calls=members.redacted_calls,
        redacted_tool_rows=members.redacted_rows,
        revoked_tool_rows=members.revoked_rows,
        mutation_ceiling=members.mutation_ceiling,
        authority_revocation=members.authority_revocation,
        private_tool_evidence=members.private_tool_evidence,
        private_tool_failures=members.private_tool_failures,
        rejection_records=members.rejection_records,
        parent_composition_state_id=prepared.parent_composition_state_id,
        expected_current_state_id=prepared.expected_current_state_id,
        plugin_crash_pending=prepared.crash_pending,
        continuation=commit,
        continuation_prepared=prepared,
    )


def recovered(authority, running, commit):
    head = authority.load_continuation_snapshot(running)
    return authority.read_continuation_p4_result(
        running, expected_checkpoint_id=head.checkpoint_id, expected_cursor=head.cursor, effect_key=commit.effect_key
    )


def rows(engine):
    with engine.connect() as conn:
        return tuple(
            tuple(conn.execute(select(table).order_by(table.c.id)).all())
            for table in (chat_messages_table, composition_states_table, composition_rejection_events_table)
        )


@pytest.mark.parametrize("graph", [False, True])
def test_actual_p4_commits_rows_complete_receipt_and_root_together(operation_store, graph):
    engine, repository, authority, service, sid = operation_store
    running, prepared, commit = selected_p4(operation_store, graph=graph)
    try:
        audit = persist(service, running, prepared, commit)
        assert type(audit) is AuditOutcome
        result = recovered(authority, running, commit)
        assert str(result.receipt.assistant_id) == audit.assistant_id
        assert result.outcome.published_assistant_content == "retained prose"
        assert result.receipt.creator_operation_id == running.session_operation_context.fence.operation_id
        assert result.receipt.invocation_ids == ("owned-p4-call",)
        assert len(result.receipt.withheld_hashes) == 1
        with engine.connect() as conn:
            stored = conn.execute(
                select(chat_messages_table).where(chat_messages_table.c.session_id == str(sid)).order_by(chat_messages_table.c.sequence_no)
            ).all()
        assert tuple(str(item) for item in result.receipt.chat_message_ids) == tuple(
            row.id for row in stored if row.writer_principal == "compose_loop"
        )
        assert [row.role for row in stored if row.writer_principal == "compose_loop"] == ["assistant", "tool", "audit", "audit", "audit"]
        assert stored[-1].content == "unpublished words"
        assert audit.checkpoint_disposition == ("committed" if graph else "empty")
        if graph:
            assert audit.checkpoint is not None
            assert result.historical_effect is not None
            assert result.receipt.inserted_state_ids == audit.checkpoint.inserted_state_ids
            assert len(result.receipt.state_row_hashes) == 1
            assert result.outcome.checkpoint.composer_meta["content_publication"]
        else:
            assert audit.checkpoint is None and result.historical_effect is None
            assert len(result.receipt.rejection_ids) == 1
        before = rows(engine)
        assert recovered(authority, running, commit) == result
        assert rows(engine) == before
        with pytest.raises(ContinuationCASConflict):
            persist(service, running, prepared, commit)
        assert rows(engine) == before
    finally:
        repository.release(running.session_operation_context)


def test_actual_revoked_publication_preserves_refusal_not_candidate_graph(operation_store):
    engine, repository, authority, service, sid = operation_store
    running, prepared, commit = selected_p4(operation_store, graph=True)
    try:
        with engine.begin() as conn:
            conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(trust_mode="explicit_approve"))
        audit = persist(service, running, prepared, commit)
        result = recovered(authority, running, commit)
        assert audit.authority_revocation == ComposerMutationRevocation(prepared_p4_members(prepared).mutation_ceiling, "explicit_approve")
        assert audit.published_assistant_content == ""
        assert audit.published_tool_rows == prepared_p4_members(prepared).revoked_rows
        assert audit.checkpoint is None and result.historical_effect is None
        assert result.outcome.authority_revocation_json is not None
        assert rows(engine)[1] == ()
        # The invocation ran and stays recorded, but a later Advisor must never
        # see its result as the turn's outcome once P4 refused the graph.
        [invocation] = prepared_p4_members(prepared).invocations
        success, _envelope = redacted_tool_invocation_content_and_envelope(invocation)
        assert json.loads(success)["success"] is True
        user = asyncio.run(
            service.add_message(
                sid, "user", "Continue", writer_principal="route_user_message", session_operation_context=running.session_operation_context
            )
        )
        history = AdvisorTranscriptHistory.from_records(
            session_id=str(sid),
            actor_user_id="alice",
            current_request_id=str(user.id),
            request="Continue",
            records=asyncio.run(service.get_messages(sid, limit=None)),
        )
        events = json.loads(AdvisorTranscriptOwner(request="Continue", history=history).snapshot().provider_json)["events"]
        [replayed] = [event for event in events if "legacy_invocations" in event]
        assert [item["tool_call_id"] for item in replayed["legacy_invocations"]] == [invocation.tool_call_id]
        assert replayed["legacy_invocations"][0]["result_canonical"] is None
        assert not any(event.get("content") == success for event in events)
        assert "content" not in replayed and "content_ref" not in replayed
        assert replayed["result_withheld"] == "authority_revoked"
    finally:
        repository.release(running.session_operation_context)


def test_result_publication_failure_rolls_back_business_rows_and_private_head(operation_store, monkeypatch):
    from elspeth.web.sessions import continuation_p4

    engine, repository, authority, service, _sid = operation_store
    running, prepared, commit = selected_p4(operation_store, graph=True)
    original = continuation_p4.retain_p4_result_on_connection
    failure = AuditIntegrityError("failure after actual result/root writes")
    head, before = authority.load_continuation_snapshot(running), rows(engine)

    def after_actual(*args, **kwargs):
        original(*args, **kwargs)
        raise failure

    try:
        with monkeypatch.context() as patch:
            patch.setattr(continuation_p4, "retain_p4_result_on_connection", after_actual)
            with pytest.raises(AuditIntegrityError) as caught:
                persist(service, running, prepared, commit)
            assert caught.value is failure
        assert authority.load_continuation_snapshot(running) == head
        assert rows(engine) == before
        with engine.connect() as conn:
            effect = conn.execute(
                select(composer_continuation_effects_table).where(composer_continuation_effects_table.c.effect_key == commit.effect_key)
            ).one()
        assert effect.disposition == "prepared" and effect.result_chunk_key is None
        assert persist(service, running, prepared, commit).checkpoint is not None
    finally:
        repository.release(running.session_operation_context)


def test_unavailable_p4_retains_rollback_facts_without_checkpoint_witness(operation_store, monkeypatch):
    engine, repository, authority, service, _sid = operation_store
    running, prepared, commit = selected_p4(operation_store, crash_pending=True)
    before = rows(engine)
    failure = OperationalError("INSERT controlled unavailable", {}, RuntimeError("storage unavailable"))

    def unavailable(*args, **kwargs):
        raise failure

    try:
        with monkeypatch.context() as patch:
            patch.setattr(service, "_insert_chat_message", unavailable)
            audit = persist(service, running, prepared, commit)
        assert audit.unwind_audit_failed and audit.checkpoint_disposition == "unavailable"
        assert audit.checkpoint is None and audit.assistant_id is None
        assert audit.continuation_retention_failure is None
        assert rows(engine) == before
        result = recovered(authority, running, commit)
        assert result.receipt.unwind_audit_failed
        assert result.receipt.chat_message_ids == result.receipt.inserted_state_ids == ()
        assert result.historical_effect is None
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.parametrize("member", ["content", "raw_content", "is_valid", "composer_meta", "provenance", "rejection_message"])
def test_recovery_binds_complete_real_rows_not_only_authored_graph(operation_store, member):
    engine, repository, authority, service, _sid = operation_store
    graph = member not in ("rejection_message",)
    running, prepared, commit = selected_p4(operation_store, graph=graph)
    try:
        audit = persist(service, running, prepared, commit)
        good = recovered(authority, running, commit)
        if member == "content":
            before = rows(engine)
            with pytest.raises(IntegrityError, match=r"chat_messages\.content is append-only"), engine.begin() as conn:
                conn.execute(update(chat_messages_table).where(chat_messages_table.c.id == audit.assistant_id).values(content="changed"))
            assert rows(engine) == before
            assert recovered(authority, running, commit).receipt == good.receipt
            return
        with engine.begin() as conn:
            if member == "raw_content":
                conn.execute(update(chat_messages_table).where(chat_messages_table.c.id == audit.assistant_id).values({member: "changed"}))
            elif member == "rejection_message":
                conn.execute(
                    update(composition_rejection_events_table)
                    .where(composition_rejection_events_table.c.id == good.receipt.rejection_ids[0])
                    .values(message="changed")
                )
            else:
                values = (
                    {"is_valid": True}
                    if member == "is_valid"
                    else {"composer_meta": {"changed": True}}
                    if member == "composer_meta"
                    else {"provenance": "session_seed"}
                )
                conn.execute(update(composition_states_table).where(composition_states_table.c.id == audit.current_state_id).values(values))
        with pytest.raises(AuditIntegrityError):
            recovered(authority, running, commit)
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.parametrize("expired", ["lease", "segment"])
def test_owed_p4_keeps_live_lease_requirement_but_drains_expired_segment(operation_store, expired):
    engine, repository, _authority, service, sid = operation_store
    running, prepared, commit = selected_p4(operation_store)
    try:
        with engine.begin() as conn:
            measured_now = database_now(conn)
            earlier = measured_now - timedelta(seconds=1)
            if expired == "lease":
                conn.execute(
                    update(session_operation_fences_table)
                    .where(session_operation_fences_table.c.session_id == str(sid))
                    .values(lease_expires_at=earlier)
                )
            else:
                conn.execute(
                    update(composer_operation_segments_table)
                    .where(composer_operation_segments_table.c.session_id == str(sid))
                    .values(
                        started_at=measured_now - timedelta(seconds=3),
                        cooperative_at=measured_now - timedelta(seconds=2),
                        deadline_at=earlier,
                    )
                )
        before = rows(engine)
        if expired == "lease":
            with pytest.raises(ComposerOperationFenceLost):
                persist(service, running, prepared, commit)
            assert rows(engine) == before
        else:
            assert persist(service, running, prepared, commit).assistant_id is not None
    finally:
        if expired == "lease":
            with pytest.raises(SessionOperationFenceLost) as cleanup:
                repository.release(running.session_operation_context)
            assert cleanup.value.reason is FenceLossReason.LEASE_EXPIRED
        else:
            repository.release(running.session_operation_context)


def test_historical_projection_never_reconstructs_live_writer_or_exception(operation_store):
    _engine, repository, _authority, service, _sid = operation_store
    running, prepared, commit = selected_p4(operation_store, graph=True)
    try:
        audit = persist(service, running, prepared, commit)
        failure = AuditIntegrityError("actual postcommit verification failure")
        cause = ValueError("original cause")
        failure.__cause__ = cause
        live = _PersistOutcome(
            current_state_id=audit.current_state_id,
            persisted_assistant_message_id=audit.assistant_id,
            persisted_assistant_content=audit.published_assistant_content,
            persisted_tool_call_turn=True,
            persisted_assistant_matches_current_dispatch=True,
            unwind_audit_failed=False,
            failed_turn=FailedTurnMetadata(audit.assistant_id, 1, 1),
            redacted_assistant_tool_calls=prepared_p4_members(prepared).redacted_calls,
            redacted_tool_rows=audit.published_tool_rows,
            audit_outcome=audit,
            effect_verification_failure=failure,
            authority_revocation=audit.authority_revocation,
        )
        retained = decode_p4_outcome(encode_p4_outcome(capture_p4_outcome(prepared, live)))
        assert retained.effect_verification_failed
        assert require_same_p4_outcome(retained, actual=live) is live
        assert live.audit_outcome is audit and live.audit_outcome.checkpoint is audit.checkpoint
        assert live.effect_verification_failure is failure and failure.__cause__ is cause
        assert "original cause" not in encode_p4_outcome(retained)
        with pytest.raises(AuditIntegrityError, match="actual current owner"):
            require_same_p4_outcome(retained, actual=replace(live, effect_verification_failure=None))
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.parametrize("changed", ["input", "creator", "cursor", "claim", "prepared_content"])
def test_writer_refuses_substituted_selected_owner_before_any_rows(operation_store, changed):
    engine, repository, authority, service, _sid = operation_store
    running, prepared, commit = selected_p4(operation_store)
    head, before = authority.load_continuation_snapshot(running), rows(engine)
    try:
        if changed == "input":
            commit = replace(commit, input_sha256="b" * 64)
        elif changed == "creator":
            prepared = prepared.model_copy(update={"creator_operation_epoch": prepared.creator_operation_epoch + 1})
        elif changed == "cursor":
            commit = replace(commit, expected_cursor=commit.expected_cursor + 1)
        elif changed == "claim":
            commit = replace(commit, running=replace(running, claim=replace(running.claim, claim_token="different-claim")))
        else:
            prepared = prepared.model_copy(update={"assistant_content": "different selected prose"})
        with pytest.raises((AuditIntegrityError, ContinuationCASConflict)):
            persist(service, running, prepared, commit)
        assert rows(engine) == before
        assert authority.load_continuation_snapshot(running) == head
    finally:
        repository.release(running.session_operation_context)


def test_unavailable_receipt_failure_keeps_original_secondary_cause(operation_store, monkeypatch):
    from elspeth.web.sessions import continuation_p4

    engine, repository, authority, service, _sid = operation_store
    running, prepared, commit = selected_p4(operation_store, crash_pending=True)
    first = OperationalError("controlled unavailable", {}, RuntimeError("first storage error"))
    second = AuditIntegrityError("controlled result retention failure")
    before, head = rows(engine), authority.load_continuation_snapshot(running)

    def unavailable(*args, **kwargs):
        raise first

    def failed_retention(*args, **kwargs):
        raise second

    try:
        with monkeypatch.context() as patch:
            patch.setattr(service, "_insert_chat_message", unavailable)
            patch.setattr(continuation_p4, "retain_p4_result_on_connection", failed_retention)
            audit = persist(service, running, prepared, commit)
        assert audit.unwind_audit_failed and audit.checkpoint is None
        assert type(audit.continuation_retention_failure) is AuditIntegrityError
        assert audit.continuation_retention_failure.__cause__ is second
        assert rows(engine) == before and authority.load_continuation_snapshot(running) == head
    finally:
        repository.release(running.session_operation_context)


def test_finish_once_returns_actual_committed_outcome_before_deferred_cancellation(operation_store, monkeypatch):
    import asyncio
    from threading import Event

    from elspeth.web.required_sql_outcomes import RequiredSQLReturned
    from elspeth.web.sessions import continuation_p4
    from tests.unit.web.sessions.test_common_mutation_authority_workers import coordinator_for

    _engine, repository, authority, service, _sid = operation_store
    running, prepared, commit = selected_p4(operation_store, graph=True)
    coordinator, ticket = coordinator_for(running)
    entered, release = Event(), Event()
    original = continuation_p4.retain_p4_result_on_connection
    observed = []

    def hold_actual(*args, **kwargs):
        receipt = original(*args, **kwargs)
        observed.append(kwargs["outcome"])
        entered.set()
        assert release.wait(10), "test did not release the actual P4 transaction"
        return receipt

    async def drive():
        members = prepared_p4_members(prepared)
        task = asyncio.create_task(
            service.persist_continuation_compose_turn(
                session_id=str(running.claim.session_id),
                writer_principal="compose_loop",
                session_operation_context=running.session_operation_context,
                assistant_content=prepared.assistant_content,
                raw_content=prepared.raw_content,
                redacted_assistant_tool_calls=members.redacted_calls,
                redacted_tool_rows=members.redacted_rows,
                revoked_tool_rows=members.revoked_rows,
                mutation_ceiling=members.mutation_ceiling,
                authority_revocation=members.authority_revocation,
                private_tool_evidence=members.private_tool_evidence,
                private_tool_failures=members.private_tool_failures,
                rejection_records=members.rejection_records,
                parent_composition_state_id=None,
                expected_current_state_id=None,
                plugin_crash_pending=False,
                continuation=commit,
                continuation_prepared=prepared,
                required_work=ticket,
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            task.cancel("after actual SQL, before commit handoff")
            await asyncio.sleep(0)
            release.set()
            return await task
        finally:
            release.set()
            if not task.done():
                await task

    try:
        with monkeypatch.context() as patch:
            patch.setattr(continuation_p4, "retain_p4_result_on_connection", hold_actual)
            result = asyncio.run(drive())
        assert type(result) is RequiredSQLReturned
        assert result.value is observed[0] and result.value.checkpoint is not None
        assert len(result.deferred_cancellations) == 1
        assert result.deferred_cancellations[0].args == ("after actual SQL, before commit handoff",)
        assert ticket.complete
        coordinator.assert_completed()
        assert str(recovered(authority, running, commit).receipt.assistant_id) == result.value.assistant_id
    finally:
        release.set()
        repository.release(running.session_operation_context)


@pytest.mark.parametrize("changed_member", ["result", "graph"])
def test_pending_data_refuses_unbound_actual_graph_and_source_result(operation_store, changed_member):
    from elspeth.web.composer.continuation_prepared_p4 import decode_prepared_p4
    from elspeth.web.composer.continuation_tool_batch import _read_json_value

    _engine, repository, authority, _service, _sid = operation_store
    _record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    try:
        stack = decode_continuation_snapshot(authority.load_continuation_snapshot(running).payload_json)
        batch, acc, response, prepared = completed_cut(running, stack, graph=True)
        pending = replace(batch, phase="p4_pending", p4_input_json=encode_prepared_p4(prepared))
        normal = encode_p4_pending_batch_chunk(batch=pending, accumulator=acc, calls=response.tool_batch.calls)
        assert decode_p4_pending_batch_chunk(normal).batch.state.to_dict() == batch.state.to_dict()
        if changed_member == "result":
            # A genuine different same-version result with the same call/audit
            # identity reaches the new full result join; no digest mismatch or
            # invalid envelope serves as the negative oracle.
            changed_result = replace(batch.tool_outcomes[0].response, success=False)
            changed = replace(pending, tool_outcomes=[replace(batch.tool_outcomes[0], response=changed_result)])
            with pytest.raises(AuditIntegrityError, match="actual selected result"):
                encode_p4_pending_batch_chunk(batch=changed, accumulator=acc, calls=response.tool_batch.calls)
        else:
            # This second candidate changes only valid payload metadata, retaining
            # source result bytes. StatePayload's own credential guard still passes;
            # the graph/result join, not a hash or constructor failure, must refuse.
            raw_rows = _read_json_value(prepared.redacted_rows_json)
            state = _read_json_value(raw_rows[0]["state_payload_json"])
            state["data"]["metadata_"]["name"] = "unrelated retained graph"
            raw_rows[0]["state_payload_json"] = canonical_json(state)
            altered = prepared.model_copy(update={"redacted_rows_json": canonical_json(raw_rows)})
            decode_prepared_p4(encode_prepared_p4(altered))
            changed = replace(pending, p4_input_json=encode_prepared_p4(altered))
            with pytest.raises(AuditIntegrityError, match="actual dispatched graph"):
                encode_p4_pending_batch_chunk(batch=changed, accumulator=acc, calls=response.tool_batch.calls)
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.parametrize("crash_pending", [False, True])
def test_pending_data_binds_exact_primary_failure_disposition(operation_store, crash_pending):
    _engine, repository, authority, _service, _sid = operation_store
    _record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    try:
        stack = decode_continuation_snapshot(authority.load_continuation_snapshot(running).payload_json)
        batch, acc, response, prepared = completed_cut(running, stack, crash_pending=crash_pending)
        pending = replace(batch, phase="p4_pending", p4_input_json=encode_prepared_p4(prepared))
        normal = encode_p4_pending_batch_chunk(batch=pending, accumulator=acc, calls=response.tool_batch.calls)
        restored = decode_p4_pending_batch_chunk(normal)
        assert (restored.batch.retained_failure is not None) is crash_pending
        altered = prepared.model_copy(update={"crash_pending": not crash_pending})
        changed = replace(pending, p4_input_json=encode_prepared_p4(altered))
        with pytest.raises(AuditIntegrityError, match="actual primary-failure disposition"):
            encode_p4_pending_batch_chunk(batch=changed, accumulator=acc, calls=response.tool_batch.calls)
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.parametrize(("kind", "crash_pending"), [("advisor_failure", True), ("advisor_timeout", False)])
def test_pending_data_preserves_advisor_failure_vs_timeout_disposition(operation_store, kind, crash_pending):
    _engine, repository, authority, _service, _sid = operation_store
    _record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    try:
        stack = decode_continuation_snapshot(authority.load_continuation_snapshot(running).payload_json)
        batch, acc, response, prepared = completed_cut(running, stack)
        batch = replace(batch, retained_failure=RetainedBatchFailure(kind=kind, error_class="RuntimeError"))
        prepared = prepared.model_copy(update={"crash_pending": crash_pending})
        pending = replace(batch, phase="p4_pending", p4_input_json=encode_prepared_p4(prepared))
        normal = encode_p4_pending_batch_chunk(batch=pending, accumulator=acc, calls=response.tool_batch.calls)
        restored = decode_p4_pending_batch_chunk(normal)
        assert restored.batch.retained_failure == batch.retained_failure
        altered = prepared.model_copy(update={"crash_pending": not crash_pending})
        with pytest.raises(AuditIntegrityError, match="actual primary-failure disposition"):
            encode_p4_pending_batch_chunk(
                batch=replace(pending, p4_input_json=encode_prepared_p4(altered)), accumulator=acc, calls=response.tool_batch.calls
            )
    finally:
        repository.release(running.session_operation_context)
