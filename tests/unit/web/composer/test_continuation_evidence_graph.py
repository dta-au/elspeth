"""Actual evidence/recorder DATA graph oracles; no SQL authorization claimed here."""

from dataclasses import replace
from uuid import UUID

import pytest

from elspeth.contracts.composer_audit import PrivateToolAuthoring
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence
from elspeth.web.composer.advisor_output import AdvisorAbandonmentResponse
from elspeth.web.composer.audit import BufferingRecorder, begin_dispatch, finish_success
from elspeth.web.composer.continuation import LogicalRequestBinding
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, encode_recorder_snapshot
from elspeth.web.composer.continuation_evidence import decode_turn_evidence, encode_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import _restore_current_evidence, capture_turn_evidence
from elspeth.web.composer.protocol import RetainedOperationContext
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer.test_continuation_physical_outcomes import private_record
from tests.unit.web.composer.test_turn_effects import (
    CONTEXT,
    SID,
    _abandonment_snapshot,
    _delivery_call,
    _page,
    _source_inspection_receipt,
    _state,
)


def rich_evidence():
    request = "Inspect café and retain every original record"
    logical = LogicalRequestBinding(
        session_id=SID,
        operation_id=str(UUID(int=71)),
        original_user_message_id="original",
        request_hash="f" * 64,
        kind="compose_message",
        base_state_id=None,
    )
    evidence = TurnEvidence(
        session_id=SID,
        session_operation_context=CONTEXT,
        request_id="original",
        request=request,
        initial_state_id=None,
        initial_state=_state(),
        actor_user_id="alice",
        retained_operation_context=RetainedOperationContext(UUID(SID), "alice", "unavailable"),
    )
    messages = [{"role": "user", "content": request}]
    evidence.transcript.begin_messages(messages)
    for call_id, produced in (("page", _page(0, 3)), ("source", _source_inspection_receipt()), ("conflict", _page(0, 2))):
        admitted, content = produced
        assert evidence.register_blob_page(
            tool_call_id=call_id, admitted=admitted, selected_content=content, producer_call_id="producer-" + call_id
        )
        messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
        assert evidence.register_quoted_inspection(
            tool_call_id=call_id, original_content=content, quoted_role="user", quoted_content="Exact quote for " + call_id
        )
    evidence.transcript.observe_messages(messages)
    delivered = evidence.prepare_delivery(messages, previous_calls=(), tools=None, scope="primary")
    assert evidence.confirm_delivery(delivered, (_delivery_call(messages, call_id="first"),))
    changed, changed_content = _page(2, 4)
    assert not evidence.register_blob_page(
        tool_call_id="conflict", admitted=changed, selected_content=changed_content, producer_call_id="changed-producer"
    )
    current = evidence.prepare_delivery(messages, previous_calls=(_delivery_call(messages, call_id="first"),), tools=None, scope="primary")
    assert evidence.confirm_delivery(current, (_delivery_call(messages, call_id="primary-current"),))
    repeated = {"role": "user", "content": "Same words, separate identity"}
    messages.extend((repeated, repeated, dict(repeated)))
    evidence.transcript.observe_messages(messages)
    policy = _abandonment_snapshot()
    review = evidence.abandonment_context(
        request=request, current_state=evidence.effects.latest_verified_state, plugin_snapshot=policy, messages=messages
    )
    decision = AdvisorAbandonmentResponse(
        decision="CONTINUE_WITH_LIMITATION", reason="Only selected ranges are read", next_action="Read the remainder", question=None
    )
    evidence.accept_abandonment(review, decision, current_state=evidence.effects.latest_verified_state, plugin_snapshot=policy)
    pending = evidence.prepare_delivery(messages, previous_calls=(_delivery_call(messages, call_id="prior"),), tools=None, scope="planner")
    assert evidence._pending_deliveries["planner"] is pending
    recorder = BufferingRecorder()
    recorder.restore_received_llm_call(private_record())
    authoring = PrivateToolAuthoring("metadata", "set_metadata", '{"name":"café"}', '{"name":"café"}', '{"name":"café"}')
    recorder.record_private_tool_authoring(authoring)
    invocation = finish_success(
        begin_dispatch("metadata", "set_metadata", {"name": "café"}, version_before=1, actor="composer-web:alice"),
        result_payload={"success": True, "detail": "retained"},
        version_after=1,
    )
    recorder.record(invocation)
    invocation = recorder.invocations[0]
    recorder.record_visible_tool_event(
        invocation,
        VisibleToolEvent.capture(
            context=CONTEXT,
            request_id="original",
            tool_call_id="metadata",
            tool_name="set_metadata",
            received_arguments=authoring.raw_arguments,
            effective_arguments=authoring.arguments_canonical,
            selected_result=invocation.result_canonical,
            display_arguments=authoring.arguments_canonical,
            display_result=invocation.result_canonical,
        ),
    )
    recorder.record_withheld_reply("repair_gate_superseded", "Keep the withheld café tail")
    return evidence, messages, recorder, logical, policy


def snapshot(evidence, messages, recorder, logical):
    return capture_turn_evidence(
        evidence, messages, recorder=recorder, logical=logical, drained=RecorderDrainCursors(llm_calls=1), env_ref_names=frozenset()
    )


def test_complete_graph_values_aliases_pending_and_private_recorder_roundtrip():
    evidence, messages, recorder, logical, _policy = rich_evidence()
    original = snapshot(evidence, messages, recorder, logical)
    chunk = encode_turn_evidence(original)
    decoded = decode_turn_evidence(chunk, env_ref_names=frozenset())
    restored = _restore_current_evidence(decoded, context=CONTEXT)
    owner = restored.evidence
    assert owner is not evidence and restored.recorder is not recorder
    assert owner.effects.initial_state is owner.effects.latest_verified_state
    assert owner._pages["page"] is owner._historical_pages["page"]
    assert "conflict" not in owner._pages and "conflict" in owner._conflicting_page_ids
    assert owner._historical_pages["conflict"].tool_call_id == "conflict"
    assert owner._current_pages["primary"][0] is owner._pages["page"]
    quoted = next(item for item in owner._quoted_inspections.values() if item.page.tool_call_id == "page")
    assert quoted.owner is owner and quoted.page is owner._pages["page"]
    pending = owner._pending_deliveries["planner"]
    assert pending.owner is owner and pending.pages[0] is owner._pages["page"]
    assert pending.previous_call_ids == frozenset({"prior"})
    assert owner._abandonment_review is not None
    review, decision = owner._abandonment_review
    limitation = owner._abandonment_limitations[review.binding]
    assert limitation[0] is review and limitation[1] is decision
    assert restored.messages[-3] is restored.messages[-2]
    assert restored.messages[-3] == restored.messages[-1] and restored.messages[-3] is not restored.messages[-1]
    assert owner.transcript._excluded[0] is restored.messages[0]
    assert restored.recorder.visible_tool_events[0][0] is restored.recorder.invocations[0]
    assert restored.recorder._lock is not recorder._lock
    assert (
        encode_recorder_snapshot(restored.recorder.snapshot_for_continuation(drained=restored.drained)).canonical == original.recorder_json
    )
    assert snapshot(owner, restored.messages, restored.recorder, logical) == original
    assert encode_turn_evidence(snapshot(owner, restored.messages, restored.recorder, logical)) == chunk


def test_distinct_equal_initial_latest_are_not_merged_by_json_equality():
    evidence, messages, recorder, logical, _policy = rich_evidence()
    equal = CompositionState.from_dict(evidence.effects.initial_state.to_dict())
    assert equal == evidence.effects.initial_state and equal is not evidence.effects.initial_state
    evidence.effects = replace(evidence.effects, latest_verified_state=equal)
    retained = snapshot(evidence, messages, recorder, logical)
    assert not retained.initial_is_latest
    restored = _restore_current_evidence(decode_turn_evidence(encode_turn_evidence(retained), env_ref_names=frozenset()), context=CONTEXT)
    assert restored.evidence.effects.initial_state == restored.evidence.effects.latest_verified_state
    assert restored.evidence.effects.initial_state is not restored.evidence.effects.latest_verified_state


def test_new_current_context_does_not_promote_historical_abandonment_scope():
    evidence, messages, recorder, logical, policy = rich_evidence()
    review = evidence._abandonment_review[0]
    evidence.accept_abandonment(
        review,
        AdvisorAbandonmentResponse(decision="STOP_CONFIRMED", reason="No supported action", next_action=None, question=None),
        current_state=evidence.effects.latest_verified_state,
        plugin_snapshot=policy,
    )
    assert evidence.confirmed_abandonment(current_state=evidence.effects.latest_verified_state, plugin_snapshot=policy)
    retained = snapshot(evidence, messages, recorder, logical)
    current = SessionOperationContext(SessionOperationFence(SID, "new-current-operation", "new-current-lease", 2), CONTEXT.operation_kind)
    restored = _restore_current_evidence(retained, context=current)
    assert restored.evidence.operation_id == current.fence.operation_id
    assert restored.evidence._continuation_current_epoch == 2
    assert restored.evidence._abandonment_review[0].request_scope == evidence._abandonment_review[0].request_scope
    assert (
        restored.evidence.confirmed_abandonment(current_state=restored.evidence.effects.latest_verified_state, plugin_snapshot=policy)
        is False
    )


def test_pending_delivery_resumes_real_confirmation_and_does_not_repeat():
    evidence, messages, recorder, logical, _policy = rich_evidence()
    restored = _restore_current_evidence(snapshot(evidence, messages, recorder, logical), context=CONTEXT)
    owner = restored.evidence
    attempt = owner._pending_deliveries["planner"]
    call = _delivery_call(restored.messages, call_id="cold-planner")
    assert owner.confirm_delivery(attempt, (call,))
    assert "planner" not in owner._pending_deliveries
    assert not owner.confirm_delivery(attempt, (call,))


@pytest.mark.parametrize("field", ("recorder_json", "initial_is_latest", "decision_shelf", "historical_application_count"))
def test_missing_complete_field_cannot_default_on_decode(field):
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.continuation_codec import encode_private_chunk

    evidence, messages, recorder, logical, _policy = rich_evidence()
    retained = snapshot(evidence, messages, recorder, logical)
    assert decode_turn_evidence(encode_turn_evidence(retained), env_ref_names=frozenset()) == retained
    payload = retained.model_dump(mode="json")
    del payload[field]
    chunk = encode_private_chunk(codec="composer.evidence.v1", canonical=canonical_json(payload))
    with pytest.raises(AuditIntegrityError):
        decode_turn_evidence(chunk, env_ref_names=frozenset())


def test_new_metadata_credentials_refuse_after_real_positive():
    evidence, messages, recorder, logical, _policy = rich_evidence()
    retained = snapshot(evidence, messages, recorder, logical)
    encode_turn_evidence(retained)
    bad = retained.model_copy(update={"actor_user_id": "postgresql://user:secret@host/db"})  # secret-scan: allow-this-line
    with pytest.raises(CredentialMaterialRefused):
        encode_turn_evidence(bad)


def test_committed_history_refuses_without_erasing_its_actual_ids():
    evidence, messages, recorder, logical, _policy = rich_evidence()
    snapshot(evidence, messages, recorder, logical)
    evidence.effects = replace(evidence.effects, committed_revision_ids=(str(UUID(int=88)),))
    with pytest.raises(AuditIntegrityError, match="actual retained P4 provenance"):
        snapshot(evidence, messages, recorder, logical)
    assert evidence.effects.committed_revision_ids == (str(UUID(int=88)),)


def test_live_blob_reader_is_refused_without_calling_or_persisting_callback():
    evidence, messages, recorder, logical, _policy = rich_evidence()
    snapshot(evidence, messages, recorder, logical)

    def reader(*args, **kwargs):
        raise AssertionError("capture must never call the live blob reader")

    evidence.transcript.use_blob_reader(reader)
    with pytest.raises(AuditIntegrityError, match="current blob-reader materialization"):
        snapshot(evidence, messages, recorder, logical)
    assert evidence.transcript._blob_reader is reader


def test_model_copy_cannot_skip_complete_owned_frontier_admission():
    evidence, messages, recorder, logical, _policy = rich_evidence()
    retained = snapshot(evidence, messages, recorder, logical)
    encode_turn_evidence(retained)
    corrupt = retained.model_copy(update={"effects": retained.effects.model_copy(update={"net_changed_fields": ("invented",)})})
    with pytest.raises(AuditIntegrityError, match="complete owned DATA contract"):
        encode_turn_evidence(corrupt)
