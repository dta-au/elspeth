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
from elspeth.web.composer.advisor_output import AdvisorAbandonmentResponse
from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.protocol import RetainedOperationContext
from elspeth.web.composer.state import CompositionState, PipelineMetadata, ValidationSummary
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
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
    assert message.count(_ADVISOR_UNTRUSTED_SUMMARY_BEGIN) == message.count(_ADVISOR_UNTRUSTED_SUMMARY_END) == 2
    wire = message.rsplit(_ADVISOR_UNTRUSTED_SUMMARY_BEGIN + "\n", 1)[1].split("\n" + _ADVISOR_UNTRUSTED_SUMMARY_END, 1)[0]
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


def _abandonment_snapshot(seed="policy"):
    return PluginAvailabilitySnapshot.create(
        policy_hash=stable_hash(seed),
        principal_scope="user:test",
        available=frozenset(),
        unavailable=(),
        selected=(),
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint=stable_hash("generation"),
    )


def test_scoped_limitation_survives_supported_work_without_authorizing_a_request_stop():
    evidence = _evidence()
    snapshot = _abandonment_snapshot()
    review = evidence.abandonment_context(request="Inspect", current_state=_state(), plugin_snapshot=snapshot, messages=())
    decision = AdvisorAbandonmentResponse(
        decision="CONTINUE_WITH_LIMITATION",
        reason="Operation B is unavailable; contact private@example.test.",
        next_action="Complete supported operation A.",
        question=None,
    )
    evidence.accept_abandonment(review, decision, current_state=_state(), plugin_snapshot=snapshot)
    assert not evidence.confirmed_abandonment(current_state=_state(), plugin_snapshot=snapshot)
    evidence.invalidate_abandonment()
    changed = _state("Supported A completed", version=2)
    evidence.accept_checkpoint(
        _witness(changed), expected_state=changed, tool_call_ids=("supported-action",), assistant_message_id=str(uuid4())
    )
    publication = evidence.review_candidate(candidate="A is saved; B remains unavailable.", request="Inspect", current_state=changed)
    payload = json.loads(publication.payload_json)
    provider = json.loads(publication.provider_payload_json)
    limitations = payload["historical_authoring_limitations"]
    assert len(limitations) == 1
    assert limitations[0]["binding"] == review.binding
    assert limitations[0]["reviewed_graph_hash"] == review.graph_hash
    assert limitations[0]["reason"] == decision.reason
    assert "private@example.test" not in provider["historical_authoring_limitations"][0]["reason"]
    assert "authoring_stop_review" not in payload
    assert not evidence.confirmed_abandonment(current_state=changed, plugin_snapshot=snapshot)
    other = _evidence().review_candidate(candidate="A is saved; B remains unavailable.", request="Inspect", current_state=changed)
    assert "historical_authoring_limitations" not in json.loads(other.payload_json)


@pytest.mark.parametrize("decision_kind", ["CONTINUE_WITH_LIMITATION", "STOP_CONFIRMED"])
def test_advisor_decision_changes_bind_publication_and_semantic_repair(decision_kind):
    evidence = _evidence()
    snapshot = _abandonment_snapshot()
    review = evidence.abandonment_context(request="Inspect", current_state=_state(), plugin_snapshot=snapshot, messages=())
    publications = []
    for reason in ("Capability B is unavailable.", "Prerequisite C is unavailable."):
        decision = AdvisorAbandonmentResponse(
            decision=decision_kind,
            reason=reason,
            next_action="Complete supported A." if decision_kind == "CONTINUE_WITH_LIMITATION" else None,
            question=None,
        )
        evidence.accept_abandonment(review, decision, current_state=_state(), plugin_snapshot=snapshot)
        publications.append(evidence.review_candidate(candidate="Here is the current result.", request="Inspect", current_state=_state()))
    assert publications[0].binding != publications[1].binding
    assert publications[0].semantic_repair_key != publications[1].semantic_repair_key
    payload = json.loads(publications[1].payload_json)
    if decision_kind == "CONTINUE_WITH_LIMITATION":
        assert len(payload["historical_authoring_limitations"]) == 1
    assert evidence.confirmed_abandonment(current_state=_state(), plugin_snapshot=snapshot) is (decision_kind == "STOP_CONFIRMED")


def test_repeated_limitation_review_is_not_semantic_repair_progress():
    evidence = _evidence()
    snapshot = _abandonment_snapshot()
    decision = AdvisorAbandonmentResponse(
        decision="CONTINUE_WITH_LIMITATION", reason="B is unavailable.", next_action="Complete A.", question=None
    )
    publications = []
    for messages in ((), ({"role": "user", "content": "The same capability evidence was reviewed again."},)):
        review = evidence.abandonment_context(request="Inspect", current_state=_state(), plugin_snapshot=snapshot, messages=messages)
        evidence.accept_abandonment(review, decision, current_state=_state(), plugin_snapshot=snapshot)
        publications.append(
            evidence.review_candidate(candidate="A remains to do; B is unavailable.", request="Inspect", current_state=_state())
        )
    assert publications[0].binding != publications[1].binding
    assert publications[0].semantic_repair_key == publications[1].semantic_repair_key
    assert len(json.loads(publications[1].payload_json)["historical_authoring_limitations"]) == 2


@pytest.mark.parametrize("stale_part", ["graph", "policy", "request", "revision"])
def test_abandonment_scopes_refuse_stale_or_different_request_evidence(stale_part):
    evidence = _evidence()
    snapshot = _abandonment_snapshot()
    review = evidence.abandonment_context(request="Inspect", current_state=_state(), plugin_snapshot=snapshot, messages=())
    if stale_part == "request":
        review = replace(review, request_scope=(SID, CONTEXT.fence.operation_id, "another-request"))
    elif stale_part == "revision":
        review = replace(review, registration_revision=review.registration_revision + 1)
    decision = AdvisorAbandonmentResponse(
        decision="STOP_CONFIRMED", reason="All remaining work requires the missing input.", next_action=None, question="Which input?"
    )
    with pytest.raises(AuditIntegrityError, match="stale evidence"):
        evidence.accept_abandonment(
            review,
            decision,
            current_state=_state("Different") if stale_part == "graph" else _state(),
            plugin_snapshot=_abandonment_snapshot("other") if stale_part == "policy" else snapshot,
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


@pytest.mark.parametrize("kind", ["blob", "source"])
def test_reply_only_conversion_keeps_current_inspection_on_its_actual_physical_call(kind):
    from elspeth.contracts.prompt_guard import detach_prompt_guard_evidence
    from elspeth.web.composer.service import _reply_only_messages

    evidence = _evidence()
    admitted, content = _page(0, 6) if kind == "blob" else _source_inspection_receipt()
    assert evidence.register_blob_page(tool_call_id="page", admitted=admitted, selected_content=content, producer_call_id="producer")
    original = [{"role": "tool", "tool_call_id": "page", "content": content}]
    quoted = _reply_only_messages(original, turn_evidence=evidence)
    assert quoted == _reply_only_messages(original)
    messages = detach_prompt_guard_evidence(quoted).messages
    assert messages[0]["role"] == "user"
    attempt = evidence.prepare_delivery(messages, previous_calls=(), tools=None, scope="primary")
    assert evidence.inspection_coverage("primary") == ()
    assert not evidence.confirm_delivery(attempt, (_delivery_call(original),))
    assert evidence.confirm_delivery(attempt, (_delivery_call(messages),))
    payload = json.loads(evidence.review_candidate(candidate="Inspect", request="Inspect", current_state=_state()).payload_json)
    assert payload["inspection"]["current_candidate_call"] == ["physical", stable_hash(messages)]
    if kind == "blob":
        assert evidence.inspection_coverage("primary")[0].complete
        assert payload["inspection"]["currently_available"][0]["ranges"] == [[0, 6]]
    else:
        assert evidence.inspection_coverage("primary") == ()
        assert payload["inspection"]["source_metadata_currently_available"][0]["byte_range_inspected"] == [0, 8192]


@pytest.mark.parametrize(
    "change",
    [
        "unminted-prefix",
        "unregistered",
        "wrong-original",
        "changed",
        "truncated",
        "role",
        "omitted",
        "duplicate-quote",
        "duplicate-direct",
        "other-owner",
        "conflict",
        "stale",
        "failed",
        "cancelled",
        "wrong-call",
        "wrong-tools",
        "old-call",
    ],
)
def test_quoted_inspection_never_borrows_authorship_registration_or_delivery(change):
    from elspeth.contracts.prompt_guard import detach_prompt_guard_evidence
    from elspeth.web.composer.service import _reply_only_messages

    evidence = _evidence()
    admitted, content = _page(0, 6)
    if change != "unregistered":
        assert evidence.register_blob_page(tool_call_id="page", admitted=admitted, selected_content=content, producer_call_id="producer")
    original = [{"role": "tool", "tool_call_id": "page", "content": content + " " if change == "wrong-original" else content}]
    quoted = _reply_only_messages(original, turn_evidence=None if change == "unminted-prefix" else evidence)
    messages = detach_prompt_guard_evidence(quoted).messages
    if change == "changed":
        messages[0]["content"] += " "
    elif change == "truncated":
        messages[0]["content"] = messages[0]["content"][:-1]
    elif change == "role":
        messages[0]["role"] = "assistant"
    elif change == "omitted":
        messages.clear()
    elif change == "duplicate-quote":
        messages.append(dict(messages[0]))
    elif change == "duplicate-direct":
        messages.extend(original)
    elif change == "other-owner":
        evidence = _evidence()
        assert evidence.register_blob_page(tool_call_id="page", admitted=admitted, selected_content=content, producer_call_id="producer")
    elif change == "conflict":
        other, other_content = _page(0, 6, content_hash="b" * 64)
        assert not evidence.register_blob_page(
            tool_call_id="page", admitted=other, selected_content=other_content, producer_call_id="producer"
        )
    call = _delivery_call(messages)
    attempt = evidence.prepare_delivery(messages, previous_calls=(call,) if change == "old-call" else (), tools=None, scope="primary")
    if change == "stale":
        evidence.prepare_delivery([], previous_calls=(), tools=None, scope="primary")
    elif change == "failed":
        call = replace(call, status=ComposerLLMCallStatus.API_ERROR, error_class="Synthetic", error_message="Synthetic")
    elif change == "cancelled":
        call = replace(call, status=ComposerLLMCallStatus.CANCELLED, error_class="Synthetic", error_message="Synthetic")
    elif change == "wrong-call":
        call = replace(call, messages_hash="wrong")
    elif change == "wrong-tools":
        call = replace(call, tools_spec_hash="wrong")
    evidence.confirm_delivery(attempt, (call,))
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


def _source_inspection_receipt(*, byte_size=234567):
    from elspeth.web.composer.source_inspection import SourceInspectionFacts, facts_to_dict

    facts = SourceInspectionFacts(
        source_kind="csv",
        redacted_identity={
            "filename": "records.csv",
            "mime_type": "text/csv",
            "byte_size": str(byte_size),
            "blob_id": "22222222-2222-4222-8222-222222222222",
            "content_hash_prefix": "abcdef01",
        },
        byte_range_inspected=(0, 8192),
        sample_row_count=15,
        observed_headers=("record",),
        inferred_types={"record": "str"},
        url_candidates=(),
        warnings=(),
    )
    result = ToolResult(
        success=True,
        updated_state=_state(),
        validation=ValidationSummary(is_valid=True, errors=()),
        affected_nodes=[],
        data=facts_to_dict(facts),
    )
    admitted = admit_discovery_result("inspect_source", result)
    return admitted, serialize_admitted_discovery_result(admitted)


def test_source_metadata_requires_exact_successfully_delivered_receipt_without_content_coverage():
    evidence = _evidence()
    admitted, content = _source_inspection_receipt()
    assert evidence.register_blob_page(
        tool_call_id="source-facts", admitted=admitted, selected_content=content, producer_call_id="producer"
    )
    before = json.loads(evidence.review_candidate(candidate="Inspect", request="Inspect", current_state=_state()).provider_payload_json)
    assert "source_metadata_currently_available" not in before["inspection"]
    messages = [{"role": "tool", "tool_call_id": "source-facts", "content": content}]
    attempt = evidence.prepare_delivery(messages, previous_calls=(), tools=None, scope="primary")
    assert evidence.confirm_delivery(attempt, (_delivery_call(messages),))
    review = evidence.review_candidate(candidate="Inspect", request="Inspect", current_state=_state())
    payload = json.loads(review.provider_payload_json)
    expected = {
        "blob_id": "22222222-2222-4222-8222-222222222222",
        "total_size_bytes": 234567,
        "byte_range_inspected": [0, 8192],
        "source_kind": "csv",
        "receipt_envelope_hash": stable_hash(content),
    }
    assert payload["inspection"]["source_metadata_currently_available"] == [expected]
    assert payload["inspection"]["source_metadata_historically_delivered"] == [expected]
    assert payload["inspection"]["currently_available"] == []
    assert evidence.inspection_coverage("primary") == ()
    assert "records.csv" not in review.provider_payload_json
    assert "observed_headers" not in review.provider_payload_json
    assert "sample_row_count" not in review.provider_payload_json
    assert "content_hash_prefix" not in review.provider_payload_json
    binding = review.binding
    later = evidence.prepare_delivery([], previous_calls=(), tools=None, scope="primary")
    assert evidence.confirm_delivery(later, (_delivery_call([], call_id="without-metadata"),))
    later_review = evidence.review_candidate(candidate="Inspect", request="Inspect", current_state=_state())
    later_payload = json.loads(later_review.provider_payload_json)
    assert later_payload["inspection"]["source_metadata_currently_available"] == []
    assert later_payload["inspection"]["source_metadata_historically_delivered"] == [expected]
    assert later_review.binding != binding
    assert later_review.semantic_repair_key != review.semantic_repair_key


@pytest.mark.parametrize("failure", ["changed-envelope", "failed-provider", "duplicate-selected-id", "conflicting-registration"])
def test_source_metadata_cannot_be_claimed_without_confirmed_current_delivery(failure):
    evidence = _evidence()
    admitted, content = _source_inspection_receipt()
    assert evidence.register_blob_page(
        tool_call_id="source-facts", admitted=admitted, selected_content=content, producer_call_id="producer"
    )
    messages = [{"role": "tool", "tool_call_id": "source-facts", "content": content}]
    if failure == "changed-envelope":
        messages[0]["content"] += " "
    elif failure == "duplicate-selected-id":
        messages.append(dict(messages[0]))
    attempt = evidence.prepare_delivery(messages, previous_calls=(), tools=None, scope="primary")
    if failure == "conflicting-registration":
        changed, changed_content = _source_inspection_receipt(byte_size=234568)
        assert not evidence.register_blob_page(
            tool_call_id="source-facts", admitted=changed, selected_content=changed_content, producer_call_id="producer"
        )
    status = ComposerLLMCallStatus.API_ERROR if failure == "failed-provider" else ComposerLLMCallStatus.SUCCESS
    evidence.confirm_delivery(attempt, (_delivery_call(messages, status=status),))
    payload = json.loads(evidence.review_candidate(candidate="Inspect", request="Inspect", current_state=_state()).provider_payload_json)
    assert "source_metadata_currently_available" not in payload["inspection"]
    assert "source_metadata_historically_delivered" not in payload["inspection"]


def test_source_metadata_planner_scope_cannot_substitute_for_primary_current_evidence():
    evidence = _evidence()
    admitted, content = _source_inspection_receipt()
    evidence.register_blob_page(tool_call_id="source-facts", admitted=admitted, selected_content=content, producer_call_id="producer")
    messages = [{"role": "tool", "tool_call_id": "source-facts", "content": content}]
    attempt = evidence.prepare_delivery(messages, previous_calls=(), tools=None, scope="planner")
    assert evidence.confirm_delivery(attempt, (_delivery_call(messages),))
    primary = json.loads(evidence.review_candidate(candidate="Inspect", request="Inspect", current_state=_state()).provider_payload_json)
    planner = json.loads(
        evidence.review_candidate(candidate="Inspect", request="Inspect", current_state=_state(), scope="planner").provider_payload_json
    )
    assert primary["inspection"]["source_metadata_currently_available"] == []
    assert len(primary["inspection"]["source_metadata_historically_delivered"]) == 1
    assert len(planner["inspection"]["source_metadata_currently_available"]) == 1
    assert planner["inspection"]["currently_available"] == []


def test_rereading_same_source_metadata_does_not_manufacture_semantic_repair_progress():
    evidence = _evidence()
    admitted, content = _source_inspection_receipt()
    reviews = []
    calls = []
    for index in range(2):
        tool_id = f"source-facts-{index}"
        evidence.register_blob_page(tool_call_id=tool_id, admitted=admitted, selected_content=content, producer_call_id="producer")
        messages = [{"role": "tool", "tool_call_id": tool_id, "content": content}]
        attempt = evidence.prepare_delivery(messages, previous_calls=tuple(calls), tools=None, scope="primary")
        calls.append(_delivery_call(messages, call_id=f"consumer-{index}"))
        assert evidence.confirm_delivery(attempt, tuple(calls))
        reviews.append(evidence.review_candidate(candidate="Inspect", request="Inspect", current_state=_state()))
    assert reviews[0].binding != reviews[1].binding
    assert reviews[0].semantic_repair_key == reviews[1].semantic_repair_key
