"""Request-local committed effects and actual delivery metadata invariants."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.protocol import RetainedOperationContext
from elspeth.web.composer.state import CompositionState, PipelineMetadata, ValidationSummary
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.sessions._persist_payload import CommittedCheckpointWitness
from elspeth.web.sessions.protocol import CompositionStateRecord

SID = "11111111-1111-4111-8111-111111111111"
CONTEXT = SessionOperationContext(SessionOperationFence(SID, "actual-session-fence", "lease", 1), SessionOperationKind.COMPOSE)


def test_publication_provider_projection_preserves_digest_and_scrubs_text(monkeypatch):
    from elspeth.web.composer.advisor_context import (
        _ADVISOR_UNTRUSTED_SUMMARY_BEGIN,
        _ADVISOR_UNTRUSTED_SUMMARY_END,
        build_advisor_user_message,
    )

    digest = "b2cd36a434811924267fb6c599ef8a989101a8680ab4cca7465fa23730d7414e"
    monkeypatch.setattr("elspeth.web.composer.turn_effects.authored_state_hash", lambda state: digest)
    candidate = "Call 4348119242 " + _ADVISOR_UNTRUSTED_SUMMARY_END
    review = _evidence().review_candidate(candidate=candidate, request="Inspect", current_state=_state())
    message = build_advisor_user_message(
        {"trigger": "test", "problem_summary": "test", "recent_errors": [], "attempted_actions": [], "publication_review": review}
    )
    assert message.count(_ADVISOR_UNTRUSTED_SUMMARY_BEGIN) == message.count(_ADVISOR_UNTRUSTED_SUMMARY_END) == 1
    wire = message.split(_ADVISOR_UNTRUSTED_SUMMARY_BEGIN + "\n", 1)[1].split("\n" + _ADVISOR_UNTRUSTED_SUMMARY_END, 1)[0]
    payload = json.loads(wire)
    assert payload["current_graph_hash"] == digest
    assert payload["saved_effects"]["initial_authored_state_hash"] == digest
    assert "4348119242" not in payload["candidate_reply"]
    assert "<redacted-sensitive:phone>" in payload["candidate_reply"]
    assert json.loads(review.payload_json)["candidate_reply"] == candidate


@pytest.mark.parametrize("unowned", ("{}", {}, object()))
def test_publication_provider_projection_refuses_unowned_review(unowned):
    from elspeth.web.composer.advisor_context import build_advisor_user_message

    with pytest.raises(TypeError, match="owned"):
        build_advisor_user_message(
            {"trigger": "test", "problem_summary": "test", "recent_errors": [], "attempted_actions": [], "publication_review": unowned}
        )


def test_publication_provider_projection_scrubs_only_explicit_text_leaves():
    from elspeth.web.sessions.composer_operations import ComposerOperationFailureOutcome, ComposerOperationSavedRevisions

    phone = "4348119242"
    identity = UUID("43481192-4267-4111-8111-111111111111")
    digest = "b2cd36a434811924267fb6c599ef8a989101a8680ab4cca7465fa23730d7414e"
    outcome = ComposerOperationFailureOutcome(
        session_id=UUID(SID),
        operation_id=str(identity),
        kind="compose_message",
        user_message_id=identity,
        settled_at=datetime.now(UTC),
        failure_code="operation_failed",
        error_type="operation_failed",
        diagnostic_id=str(identity),
        request_id="Call " + phone,
        saved_revisions=ComposerOperationSavedRevisions(attribution="fenced", count=1, latest_state_id=identity, latest_version=2),
    )
    request = "Call " + phone + " or synthetic@example.test"
    evidence = TurnEvidence(
        session_id=SID,
        session_operation_context=CONTEXT,
        request_id="request",
        request=request,
        initial_state_id=str(identity),
        initial_state=_state(),
        retained_operation_context=RetainedOperationContext(
            session_id=UUID(SID), actor_user_id="owner", availability="available", outcomes=(outcome,)
        ),
        actor_user_id="owner",
    )
    evidence.effects = replace(evidence.effects, net_changed_fields=("nodes.Call " + phone + ".options",))
    admitted, content = _page(0, 6, content_hash=digest)
    evidence.register_blob_page(tool_call_id="page", admitted=admitted, selected_content=content, producer_call_id="producer")
    messages = [{"role": "tool", "tool_call_id": "page", "content": content}]
    attempt = evidence.prepare_delivery(messages, previous_calls=(), tools=None, scope="primary")
    assert evidence.confirm_delivery(attempt, (_delivery_call(messages, call_id="Call " + phone),))
    review = evidence.review_candidate(candidate="Email synthetic@example.test", request=request, current_state=_state())
    raw = json.loads(review.payload_json)
    provider = json.loads(review.provider_payload_json)
    assert phone not in provider["current_request"]
    assert "synthetic@example.test" not in provider["candidate_reply"]
    assert phone not in provider["saved_effects"]["changed_fields"][0]
    assert provider["saved_effects"]["latest_verified_state_id"] == str(identity)
    assert phone not in provider["inspection"]["current_candidate_call"][0]
    assert provider["inspection"]["current_candidate_call"][1] == raw["inspection"]["current_candidate_call"][1]
    assert provider["inspection"]["currently_available"][0]["content_hash"] == digest
    actual = provider["retained_operation_history"]["outcomes"][0]
    assert phone not in actual["request_id"]
    assert {key: value for key, value in actual.items() if key != "request_id"} == {
        key: value for key, value in raw["retained_operation_history"]["outcomes"][0].items() if key != "request_id"
    }
    assert raw["current_request"] == request


def test_retained_history_binds_review_without_becoming_current_saved_effects():
    state = _state()
    contexts = [
        RetainedOperationContext(session_id=UUID(SID), actor_user_id="owner", availability=status)
        for status in ("available", "unavailable")
    ]
    reviews = []
    for context in contexts:
        owner = TurnEvidence(
            session_id=SID,
            session_operation_context=CONTEXT,
            request_id="same-request",
            request="Explain",
            initial_state_id=None,
            initial_state=state,
            retained_operation_context=context,
            actor_user_id="owner",
        )
        review = owner.review_candidate(candidate="History explanation", request="Explain", current_state=state)
        payload = json.loads(review.payload_json)
        assert payload["retained_operation_history"]["availability"] == context.availability
        assert payload["saved_effects"]["saved_revision_count"] == 0
        assert payload["saved_effects"]["net_changed"] is False
        reviews.append(review)
    assert reviews[0].binding != reviews[1].binding
    assert reviews[0].semantic_repair_key != reviews[1].semantic_repair_key
    with pytest.raises(AuditIntegrityError):
        TurnEvidence(
            session_id=SID,
            session_operation_context=CONTEXT,
            request_id="same-request",
            request="Explain",
            initial_state_id=None,
            initial_state=state,
            retained_operation_context=contexts[0],
            actor_user_id="another-owner",
        )


def _state(name=None, *, version=1):
    return CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name=name), version=version)


def _witness(state):
    payload = state.to_dict()
    state_id = uuid4()
    record = CompositionStateRecord(
        id=state_id,
        session_id=UUID(SID),
        version=state.version,
        sources=payload["sources"],
        nodes=payload["nodes"],
        edges=payload["edges"],
        outputs=payload["outputs"],
        metadata_=payload["metadata"],
        is_valid=False,
        validation_errors=None,
        created_at=datetime.now(UTC),
        derived_from_state_id=None,
    )
    return CommittedCheckpointWitness(state=record, creator_operation_id=CONTEXT.fence.operation_id, inserted_state_ids=(state_id,))


def test_request_net_effects_do_not_turn_reverted_or_noop_saved_revisions_into_edits():
    baseline = _state()
    evidence = TurnEvidence(
        session_id=SID,
        session_operation_context=CONTEXT,
        request_id="request-one",
        request="Change name",
        initial_state_id=None,
        initial_state=baseline,
    )
    changed = _state("Changed", version=2)
    first = _witness(changed)
    evidence.accept_checkpoint(first, expected_state=changed, tool_call_ids=("call-one",), assistant_message_id=str(uuid4()))
    assert evidence.effects.net_changed
    assert evidence.effects.changed_fields == ("metadata.name",)
    assert evidence.effects.saved_revision_count == 1
    reverted = _state(version=3)
    evidence.accept_checkpoint(_witness(reverted), expected_state=reverted, tool_call_ids=("call-two",), assistant_message_id=str(uuid4()))
    assert not evidence.effects.net_changed
    assert evidence.effects.changed_fields == ()
    assert evidence.effects.saved_revision_count == 2
    assert evidence.effects.latest_verified_state_id != str(first.state.id)
    assert "Changed" not in evidence.render_saved_scope()
    other = TurnEvidence(
        session_id=SID,
        session_operation_context=CONTEXT,
        request_id="request-two",
        request="Explain",
        initial_state_id=evidence.effects.latest_verified_state_id,
        initial_state=reverted,
    )
    assert other.effects.saved_revision_count == 0
    assert other.identity != evidence.identity


@pytest.mark.parametrize("tamper", ("content", "session", "fence"))
def test_owned_witness_mismatch_never_becomes_zero_or_verified_completion(tamper):
    baseline = _state()
    evidence = TurnEvidence(
        session_id=SID,
        session_operation_context=CONTEXT,
        request_id="request",
        request="Change",
        initial_state_id=None,
        initial_state=baseline,
    )
    expected = _state("Expected", version=2)
    witness = _witness(expected)
    if tamper == "content":
        witness = replace(witness, state=replace(witness.state, metadata_={"name": "Different", "description": None}))
    elif tamper == "session":
        witness = replace(witness, state=replace(witness.state, session_id=uuid4()))
    else:
        witness = replace(witness, creator_operation_id="other-session-fence")
    with pytest.raises(AuditIntegrityError):
        evidence.accept_checkpoint(witness, expected_state=expected, tool_call_ids=("call",), assistant_message_id=str(uuid4()))
    assert evidence.effects.verification == "unavailable"
    assert not evidence.effects.net_changed
    assert "No pipeline changes" not in evidence.render_saved_scope()


def test_sessionless_evidence_cannot_claim_persisted_edits():
    evidence = TurnEvidence(
        session_id=None,
        session_operation_context=None,
        request_id="sessionless",
        request="Explain",
        initial_state_id=None,
        initial_state=_state(),
    )
    assert evidence.effects.verification == "not_persisted"
    with pytest.raises(AuditIntegrityError):
        evidence.accept_checkpoint(_witness(_state()), expected_state=_state(), tool_call_ids=("call",), assistant_message_id=str(uuid4()))


def _evidence():
    return TurnEvidence(
        session_id=SID,
        session_operation_context=CONTEXT,
        request_id="request",
        request="Inspect",
        initial_state_id=None,
        initial_state=_state(),
    )


def _page(start, end, *, total=6, content_hash="a" * 64):
    result = ToolResult(
        success=True,
        updated_state=_state(),
        validation=ValidationSummary(is_valid=True, errors=()),
        affected_nodes=[],
        data={
            "blob_id": "blob",
            "filename": "file.txt",
            "mime_type": "text/plain",
            "content": "abcdef"[start:end],
            "truncated": end < total,
            "size_bytes": total,
            "content_hash": content_hash,
            "total_chars": total,
            "start_char": start,
            "end_char": end,
            "next_offset_chars": end if end < total else None,
            "created_by": "user",
            "creation_modality": "verbatim",
        },
    )
    admitted = admit_discovery_result("get_blob_content", result)
    return admitted, serialize_admitted_discovery_result(admitted)


def _delivery_call(messages, *, call_id="physical", status=ComposerLLMCallStatus.SUCCESS):
    now = datetime.now(UTC)
    return ComposerLLMCall(
        model_requested="model",
        model_returned=None,
        status=status,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        latency_ms=1,
        provider_request_id=None,
        messages_hash=stable_hash(messages),
        tools_spec_hash=None,
        declared_tool_names=(),
        started_at=now,
        finished_at=now,
        error_class=None if status is ComposerLLMCallStatus.SUCCESS else "ProviderError",
        error_message=None if status is ComposerLLMCallStatus.SUCCESS else "Provider delivery failed",
        temperature=None,
        seed=None,
        call_id=call_id,
    )


def test_blob_coverage_requires_new_retained_call_and_only_current_selected_pages():
    evidence = _evidence()
    messages = []
    first = _delivery_call(messages, call_id="producer")
    attempt = evidence.prepare_delivery(messages, previous_calls=(), tools=None, scope="primary")
    assert evidence.confirm_delivery(attempt, (first,))
    for tool_id, start, end in (("prefix", 0, 3), ("suffix", 3, 6)):
        admitted, content = _page(start, end)
        evidence.register_blob_page(tool_call_id=tool_id, admitted=admitted, selected_content=content, producer_call_id=first.call_id)
        messages.append({"role": "tool", "tool_call_id": tool_id, "content": content})
    attempt = evidence.prepare_delivery(messages, previous_calls=(first,), tools=None, scope="primary")
    assert evidence.inspection_coverage("primary") == ()
    assert not evidence.confirm_delivery(attempt, (first,))
    delivered = _delivery_call(messages, call_id="candidate")
    assert evidence.confirm_delivery(attempt, (first, delivered))
    assert evidence.inspection_coverage("primary")[0].complete
    assert evidence.inspection_coverage("primary")[0].ranges == ((0, 6),)
    prefix_only = messages[:1]
    attempt = evidence.prepare_delivery(prefix_only, previous_calls=(first, delivered), tools=None, scope="primary")
    assert evidence.confirm_delivery(attempt, (first, delivered, _delivery_call(prefix_only, call_id="later")))
    assert evidence.inspection_coverage("primary")[0].ranges == ((0, 3),)
    assert not evidence.inspection_coverage("primary")[0].complete
    assert evidence.historical_inspection_coverage()[0].complete


@pytest.mark.parametrize("change", ("content", "tool_id", "role", "duplicate", "omitted", "old_audit", "wrong_hash", "failed_audit"))
def test_blob_delivery_cannot_borrow_changed_context_or_old_audit(change):
    evidence = _evidence()
    admitted, content = _page(0, 6)
    evidence.register_blob_page(tool_call_id="page", admitted=admitted, selected_content=content, producer_call_id="producer")
    messages = [{"role": "tool", "tool_call_id": "page", "content": content}]
    if change == "content":
        messages[0]["content"] = content + " "
    elif change == "tool_id":
        messages[0]["tool_call_id"] = "other"
    elif change == "role":
        messages[0]["role"] = "user"
    elif change == "duplicate":
        messages.append(dict(messages[0]))
    elif change == "omitted":
        messages.clear()
    call = _delivery_call(messages)
    previous = (call,) if change == "old_audit" else ()
    attempt = evidence.prepare_delivery(messages, previous_calls=previous, tools=None, scope="primary")
    if change == "wrong_hash":
        call = replace(call, messages_hash="wrong")
    elif change == "failed_audit":
        call = replace(call, status=ComposerLLMCallStatus.API_ERROR, error_class="SyntheticFailure", error_message="Synthetic")
    evidence.confirm_delivery(attempt, (call,))
    assert evidence.inspection_coverage("primary") == ()


def test_blob_delivery_rejects_conflicting_ids_stale_attempts_and_other_request_owners():
    evidence = _evidence()
    admitted, content = _page(0, 6)
    assert evidence.register_blob_page(tool_call_id="page", admitted=admitted, selected_content=content, producer_call_id="producer")
    messages = [{"role": "tool", "tool_call_id": "page", "content": content}]
    stale = evidence.prepare_delivery(messages, previous_calls=(), tools=None, scope="primary")
    current = evidence.prepare_delivery(messages, previous_calls=(), tools=None, scope="primary")
    assert not evidence.confirm_delivery(stale, (_delivery_call(messages),))
    with pytest.raises(TypeError, match="different request"):
        _evidence().confirm_delivery(current, (_delivery_call(messages),))
    other, other_content = _page(0, 6, content_hash="b" * 64)
    assert not evidence.register_blob_page(tool_call_id="page", admitted=other, selected_content=other_content, producer_call_id="producer")
    assert not evidence.confirm_delivery(current, (_delivery_call(messages),))
    retry = evidence.prepare_delivery(messages, previous_calls=(), tools=None, scope="primary")
    assert evidence.confirm_delivery(retry, (_delivery_call(messages),))
    assert evidence.inspection_coverage("primary") == ()


def test_blob_delivery_requires_matching_tools_and_actual_selected_envelope():
    evidence = _evidence()
    admitted, content = _page(0, 6)
    with pytest.raises(AuditIntegrityError, match="selected result envelope"):
        evidence.register_blob_page(tool_call_id="page", admitted=admitted, selected_content=content + " ", producer_call_id="producer")
    assert not evidence.register_blob_page(tool_call_id="page", admitted=admitted, selected_content=content, producer_call_id=None)
    assert evidence.register_blob_page(tool_call_id="page", admitted=admitted, selected_content=content, producer_call_id="producer")
    messages = [{"role": "tool", "tool_call_id": "page", "content": content}]
    attempt = evidence.prepare_delivery(messages, previous_calls=(), tools=[{"type": "function"}], scope="primary")
    assert not evidence.confirm_delivery(attempt, (_delivery_call(messages),))
    assert evidence.inspection_coverage("primary") == ()


def test_publication_review_binding_moves_for_new_prose_request_effects_and_current_coverage():
    evidence = _evidence()
    state = _state()
    first = evidence.review_candidate(candidate="Explanation", request="Inspect", current_state=state)
    assert first.binding != evidence.review_candidate(candidate="All edits complete", request="Inspect", current_state=state).binding
    changed = _state("Changed", version=2)
    assert first.binding != evidence.review_candidate(candidate="Explanation", request="Inspect", current_state=changed).binding
    with pytest.raises(AuditIntegrityError, match="request"):
        evidence.review_candidate(candidate="Explanation", request="Different request", current_state=state)
    evidence.accept_checkpoint(_witness(changed), expected_state=changed, tool_call_ids=("change",), assistant_message_id=str(uuid4()))
    saved = evidence.review_candidate(candidate="Explanation", request="Inspect", current_state=changed)
    assert saved.binding != first.binding
    assert '"saved_revision_count": 1' in saved.payload_json
    assert '"changed_fields": ["metadata.name"]' in saved.payload_json
    assert '"candidate_reply": "Explanation"' in saved.payload_json
    attempt = evidence.prepare_delivery([], previous_calls=(), tools=None, scope="primary")
    evidence.confirm_delivery(attempt, (_delivery_call([], call_id="new-candidate"),))
    assert saved.binding != evidence.review_candidate(candidate="Explanation", request="Inspect", current_state=changed).binding


def test_blob_coverage_keeps_content_identities_and_planner_availability_separate():
    evidence = _evidence()
    messages = []
    for tool_id, start, end, content_hash in (("prefix", 0, 3, "a" * 64), ("suffix", 3, 6, "b" * 64)):
        admitted, content = _page(start, end, content_hash=content_hash)
        evidence.register_blob_page(tool_call_id=tool_id, admitted=admitted, selected_content=content, producer_call_id="producer")
        messages.append({"role": "tool", "tool_call_id": tool_id, "content": content, "cache_control": {"type": "ephemeral"}})
    attempt = evidence.prepare_delivery(messages, previous_calls=(), tools=None, scope="planner")
    assert evidence.confirm_delivery(attempt, (_delivery_call(messages),))
    assert len(evidence.inspection_coverage("planner")) == 2
    assert not any(item.complete for item in evidence.inspection_coverage("planner"))
    assert evidence.inspection_coverage("primary") == ()


def test_semantic_repair_key_ignores_new_transport_and_page_partition_but_retains_current_coverage():
    evidence = _evidence()
    calls = []

    def deliver(pages, call_id):
        messages = [{"role": "user", "content": call_id}]
        for tool_id, start, end in pages:
            admitted, content = _page(start, end)
            evidence.register_blob_page(tool_call_id=tool_id, admitted=admitted, selected_content=content, producer_call_id="producer")
            messages.append({"role": "tool", "tool_call_id": tool_id, "content": content})
        attempt = evidence.prepare_delivery(messages, previous_calls=tuple(calls), tools=None, scope="primary")
        calls.append(_delivery_call(messages, call_id=call_id))
        assert evidence.confirm_delivery(attempt, tuple(calls))
        return evidence.review_candidate(candidate="Explanation", request="Inspect", current_state=_state())

    first = deliver((("whole", 0, 6),), "first-physical-call")
    second = deliver((("whole", 0, 6),), "second-physical-call")
    assert first.binding != second.binding
    assert first.semantic_repair_key == second.semantic_repair_key
    partitioned = deliver((("prefix", 0, 3), ("suffix", 3, 6)), "partitioned-call")
    assert partitioned.binding != second.binding
    assert partitioned.semantic_repair_key == second.semantic_repair_key
    less = deliver((("prefix", 0, 3),), "prefix-only-call")
    assert less.semantic_repair_key != partitioned.semantic_repair_key
    omitted = deliver((), "omitted-call")
    assert omitted.semantic_repair_key != less.semantic_repair_key
    assert evidence.historical_inspection_coverage()[0].complete
    candidate_changed = evidence.review_candidate(candidate="Different explanation", request="Inspect", current_state=_state())
    assert candidate_changed.semantic_repair_key != omitted.semantic_repair_key
    graph_changed = evidence.review_candidate(candidate="Explanation", request="Inspect", current_state=_state("Changed"))
    assert graph_changed.semantic_repair_key != omitted.semantic_repair_key
    evidence.mark_verification_unavailable()
    unknown = evidence.review_candidate(candidate="Explanation", request="Inspect", current_state=_state())
    assert unknown.semantic_repair_key != omitted.semantic_repair_key


def test_semantic_repair_key_retains_saved_revision_scope_and_current_content_identity():
    evidence = _evidence()
    before = evidence.review_candidate(candidate="Explanation", request="Inspect", current_state=_state())
    # Even a no-op authored revision changes the truth of saved-progress claims.
    saved = _state(version=2)
    evidence.accept_checkpoint(_witness(saved), expected_state=saved, tool_call_ids=("no-op",), assistant_message_id=str(uuid4()))
    after = evidence.review_candidate(candidate="Explanation", request="Inspect", current_state=saved)
    assert evidence.effects.net_changed is False
    assert before.semantic_repair_key != after.semantic_repair_key
    reviews = []
    calls = []
    for tool_id, content_hash in (("old-content", "a" * 64), ("new-content", "b" * 64)):
        admitted, content = _page(0, 6, content_hash=content_hash)
        evidence.register_blob_page(tool_call_id=tool_id, admitted=admitted, selected_content=content, producer_call_id="producer")
        messages = [{"role": "tool", "tool_call_id": tool_id, "content": content}]
        attempt = evidence.prepare_delivery(messages, previous_calls=tuple(calls), tools=None, scope="primary")
        calls.append(_delivery_call(messages, call_id=tool_id))
        assert evidence.confirm_delivery(attempt, tuple(calls))
        reviews.append(evidence.review_candidate(candidate="Explanation", request="Inspect", current_state=saved))
    assert reviews[0].semantic_repair_key != reviews[1].semantic_repair_key
    other = TurnEvidence(
        session_id=SID,
        session_operation_context=CONTEXT,
        request_id="different-request",
        request="Inspect",
        initial_state_id=None,
        initial_state=_state(),
    )
    assert (
        before.semantic_repair_key
        != other.review_candidate(candidate="Explanation", request="Inspect", current_state=_state()).semantic_repair_key
    )
