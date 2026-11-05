from datetime import UTC, datetime

import pytest

from elspeth.contracts import hashing
from elspeth.contracts.composer_audit import (
    ComposerToolInvocation,
    ComposerToolStatus,
    PrivateFailureFacts,
    PrivateToolAuthoring,
)
from elspeth.contracts.composer_planner_audit import (
    ComposerPlannerAttempt,
    ComposerPlannerAttemptLedTo,
    ComposerPlannerAttemptOutcome,
    ComposerPlannerAttemptPhase,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.session_operation import (
    SessionOperationContext,
    SessionOperationFence,
    SessionOperationKind,
)
from elspeth.web.composer import continuation_audit_buffer as audit_buffer
from elspeth.web.composer.audit import BufferingRecorder, PrivatePlannerCandidate
from elspeth.web.composer.continuation_audit_buffer import (
    RecorderDrainCursors,
    decode_recorder_snapshot,
    encode_recorder_snapshot,
)
from elspeth.web.composer.continuation_codec import PrivateChunk
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from tests.unit.web.composer.test_continuation_physical_outcomes import actual_record


def test_recorder_projection_selects_native_writer_and_reader_preserves_public_canonical_bytes(monkeypatch):
    from tests.unit.web.composer.test_continuation_audit_buffer_blocks import _snapshot

    snapshot = _snapshot(2)
    native, public = audit_buffer.canonical_json_native_strings, audit_buffer.canonical_json
    native_calls, public_calls = [], []

    def observe_native(value):
        native_calls.append(True)
        result = native(value)
        assert result == public(value)
        return result

    def observe_public(value):
        public_calls.append(True)
        return public(value)

    monkeypatch.setattr(audit_buffer, "canonical_json_native_strings", observe_native)
    monkeypatch.setattr(audit_buffer, "canonical_json", observe_public)
    chunk = encode_recorder_snapshot(snapshot)
    assert native_calls == [True] and public_calls
    assert decode_recorder_snapshot(chunk) == snapshot
    assert native_calls == [True, True]
    assert hashing.canonical_json is public


@pytest.mark.asyncio
async def test_changed_final_writer_cannot_hide_behind_warm_request_proofs(monkeypatch):
    from elspeth.web.composer.continuation_clock import ContinuationActionClock
    from elspeth.web.composer.continuation_policy import ContinuationActionPolicy
    from elspeth.web.continuation_retention_memo import borrow_v4_preparation_memo_scope, v4_preparation_memo_scope
    from tests.unit.web.composer.test_continuation_audit_buffer_blocks import _snapshot

    snapshot = _snapshot(2)
    original_prepared = audit_buffer._prepared_request_text
    original_writer = audit_buffer.canonical_json_native_strings
    projected = []

    def prepare(key):
        projected.append(True)
        return original_prepared(key)

    def changed_writer(value):
        return original_writer(value) + " "

    monkeypatch.setattr(audit_buffer, "_prepared_request_text", prepare)
    clock = ContinuationActionClock(ContinuationActionPolicy(preparation=10, guard=1, quota=1, physical=1, retention=1))
    with v4_preparation_memo_scope(clock):
        with borrow_v4_preparation_memo_scope():
            encode_recorder_snapshot(snapshot)
        assert projected == [True, True]
        monkeypatch.setattr(audit_buffer, "canonical_json_native_strings", changed_writer)
        with borrow_v4_preparation_memo_scope(), pytest.raises(AuditIntegrityError, match="canonical strict JSON"):
            encode_recorder_snapshot(snapshot)
        assert projected == [True, True]


def test_buffer_cold_roundtrip_preserves_private_channels_lock_alias_and_partial_drain():
    now = datetime(2026, 10, 20, tzinfo=UTC)
    authoring = PrivateToolAuthoring("call-1", "list_sinks", "{}", "{}", "{}")
    invocation = ComposerToolInvocation(
        tool_call_id="call-1",
        tool_name="list_sinks",
        arguments_canonical="{}",
        arguments_hash=stable_hash({}),
        result_canonical="{}",
        result_hash=stable_hash({}),
        status=ComposerToolStatus.SUCCESS,
        error_class=None,
        error_message=None,
        version_before=1,
        version_after=1,
        started_at=now,
        finished_at=now,
        latency_ms=0,
        actor="composer-web:user-owner",
        private_authoring=authoring,
        private_failure=PrivateFailureFacts('{"failure_class":"request_rejected"}'),
    )
    context = SessionOperationContext(
        SessionOperationFence("11111111-1111-4111-8111-111111111111", "creator", "lease", 1),
        SessionOperationKind.COMPOSE,
    )
    visible = VisibleToolEvent.capture(
        context=context,
        request_id="original",
        tool_call_id="call-1",
        tool_name="list_sinks",
        received_arguments="{}",
        effective_arguments="{}",
        selected_result="{}",
        display_arguments="{}",
        display_result="{}",
    )
    owner = BufferingRecorder()
    call, _metadata = actual_record()
    owner.restore_received_llm_call(call)
    planner = ComposerPlannerAttempt(
        ordinal=1,
        planner_call_ordinal=1,
        phase=ComposerPlannerAttemptPhase.DISCOVERY,
        outcome=ComposerPlannerAttemptOutcome.DISCOVERY_EXECUTED,
        planner_code=None,
        selected_tools=("list_sinks",),
        requested_information=(),
        new_information=(),
        rejection_codes=(),
        candidate_shape_hash=None,
        repeated_fingerprint=False,
        led_to=ComposerPlannerAttemptLedTo.CONTINUE,
    )
    owner.record_planner_attempt(planner)
    owner.record_private_tool_authoring(authoring)
    owner.record(invocation)
    recorded = owner.invocations[0]
    owner.record_visible_tool_event(recorded, visible)
    owner.record_withheld_reply("repair_gate_superseded", "A superseded reply")
    owner.record_private_candidate(
        PrivatePlannerCandidate(
            "candidate-1",
            "future-call",
            "list_sources",
            "{}",
            "{}",
            "{}",
        )
    )
    snapshot = owner.snapshot_for_continuation(drained=RecorderDrainCursors(invocations=1, llm_calls=1, planner_attempts=1))
    chunk = encode_recorder_snapshot(snapshot)
    restored, cursors = BufferingRecorder.for_continuation(
        decode_recorder_snapshot(PrivateChunk(chunk.reference, chunk.canonical.encode().decode())),
    )
    assert restored is not owner
    assert restored._lock is not owner._lock
    assert restored.visible_tool_events[0][0] is restored.invocations[0]
    assert restored.invocations[0].private_authoring == authoring
    assert restored.invocations[0].private_failure == invocation.private_failure
    assert restored.llm_calls == (call,)
    assert restored.planner_attempts == (planner,)
    assert restored.private_candidates == owner.private_candidates
    assert restored.withheld_replies == owner.withheld_replies
    assert cursors.invocations == 1 and cursors.withheld_replies == 0
    assert cursors.llm_calls == 1 and cursors.planner_attempts == 1
    assert restored.invocations[cursors.invocations :] == ()
    assert restored.withheld_replies[cursors.withheld_replies :] == owner.withheld_replies
    assert encode_recorder_snapshot(restored.snapshot_for_continuation(drained=cursors)) == chunk


def test_buffer_snapshot_refuses_a_drain_past_the_retained_prefix():
    owner = BufferingRecorder()
    with pytest.raises(AuditIntegrityError, match="drain cursor"):
        owner.snapshot_for_continuation(drained=RecorderDrainCursors(llm_calls=1))
