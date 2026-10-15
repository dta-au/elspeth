"""Advisor-only decisions survive real route, fenced write, and DB reload seams."""

from dataclasses import replace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from elspeth.web.composer.advisor_decision import AdvisorBlockCause, AdvisorGateBlocked, AdvisorGatePassed, AdvisorSignoffGateFact
from elspeth.web.composer.protocol import ComposerConvergenceError, ComposerResult
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.completion_gates import (
    ADVISOR_SIGNOFF_PENDING_DETAIL,
    CompletionGateFacts,
    advisor_block_covers_unchanged_graph,
    completion_gate_fingerprint,
    completion_gates_meta_from_facts,
    merge_completion_gates,
    parse_completion_gates,
)
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.protocol import CompositionStateData
from tests.helpers.composer_operations import submit_and_settle
from tests.unit.web.sessions.test_completion_gate_roundtrip import _green_result
from tests.unit.web.sessions.test_routes import _make_app, _make_authoring_valid_partial


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["messages", "recompose"])
@pytest.mark.parametrize(
    "patch", [{"name": "Renamed intent"}, {"description": "Different stated intent"}, {}], ids=["name", "description", "unchanged"]
)
async def test_metadata_recovery_save_does_not_reattribute_prior_advisor_note(tmp_path, route, patch):
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Metadata recovery", "local")
    reviewed = _make_authoring_valid_partial("metadata-recovery")
    facts = CompletionGateFacts(
        advisor_signoff=AdvisorSignoffGateFact(
            detail="The reviewed intent does not match the pipeline.",
            suggestion="Revise the stated intent.",
            note="The pipeline does not perform the requested transformation.",
            for_graph=completion_gate_fingerprint(reviewed),
            cause=AdvisorBlockCause.GRAPH_REJECTED,
        )
    )
    state_d = reviewed.to_dict()
    await service.save_composition_state(
        session.id,
        CompositionStateData(
            sources=state_d["sources"],
            nodes=state_d["nodes"],
            edges=state_d["edges"],
            outputs=state_d["outputs"],
            metadata_=state_d["metadata"],
            is_valid=True,
            validation_errors=None,
            composer_meta={"completion_gates": completion_gates_meta_from_facts(facts)},
        ),
        provenance="session_seed",
    )
    before = await service.get_current_state(session.id)
    assert before is not None

    class InterruptedComposer:
        async def compose(self, message, chat_messages, state: CompositionState, **kwargs):
            del message, chat_messages
            assert kwargs["completion_gates"] == facts
            # Production metadata mutation increments the version. The turn
            # then fails before adjudication and the route saves its partial state.
            raise ComposerConvergenceError(max_turns=5, budget_exhausted="composition", partial_state=state.with_metadata(patch))

    app.state.composer_service = InterruptedComposer()
    if route == "recompose":
        user_message = await service.add_message(session.id, "user", "Revise the intent", writer_principal="route_user_message")
        request_body = {"expected_user_message_id": str(user_message.id), "operation_id": str(uuid4()), "state_id": str(before.id)}
    else:
        request_body = {"content": "Revise the intent", "operation_id": str(uuid4()), "state_id": str(before.id)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        settled = await submit_and_settle(client, app, path=f"/api/sessions/{session.id}/{route}", body=request_body)
        status, response_body = settled.error()
    assert status == 422, response_body
    after = await service.get_current_state(session.id)
    assert after is not None
    assert after.id != before.id
    assert after.version == before.version + 1
    assert response_body["detail"]["error_type"] == "convergence"
    assert response_body["detail"]["partial_state"]["id"] == str(after.id)
    rebuilt = state_from_record(after)
    assert rebuilt.metadata == reviewed.with_metadata(patch).metadata
    reloaded_facts = parse_completion_gates(after.composer_meta)
    assert reloaded_facts == facts
    result = merge_completion_gates(_green_result(), reloaded_facts, rebuilt)
    assert result.readiness.completion_ready is False
    (blocker,) = result.readiness.blockers
    assert facts.advisor_signoff is not None
    assert blocker.detail == (ADVISOR_SIGNOFF_PENDING_DETAIL if patch else facts.advisor_signoff.detail)
    assert blocker.note == (None if patch else facts.advisor_signoff.note)
    assert blocker.suggestion == (None if patch else facts.advisor_signoff.suggestion)
    # The next unchanged turn must not skip review of the changed intent.
    assert advisor_block_covers_unchanged_graph(reloaded_facts, rebuilt, initial_version=rebuilt.version) is (not patch)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["messages", "recompose"])
@pytest.mark.parametrize("cause", [AdvisorBlockCause.UNAVAILABLE, AdvisorBlockCause.MALFORMED, AdvisorBlockCause.MESSAGE_REJECTED])
@pytest.mark.parametrize("adjudicates", [True, False], ids=["clean-decision", "validation-only"])
async def test_advisor_recovery_is_durable_only_with_explicit_clean_decision(tmp_path, route, cause, adjudicates, monkeypatch):
    import json

    import litellm

    from elspeth.web.composer.advisor_audit import (
        ADVISOR_CHECKPOINT_PASS_AUDIT_KIND,
        ADVISOR_GRAPH_REVIEW_AUDIT_KIND,
        AdvisorGraphReviewIdentity,
    )
    from elspeth.web.composer.advisor_context import _ADVISOR_UNTRUSTED_SUMMARY_BEGIN, _ADVISOR_UNTRUSTED_SUMMARY_END
    from elspeth.web.composer.audit import BufferingRecorder
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.composer.turn_effects import TurnEvidence

    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Advisor recovery", "local")
    state = _make_authoring_valid_partial("advisor-recovery")
    fingerprint = completion_gate_fingerprint(state)
    facts = CompletionGateFacts(
        advisor_signoff=AdvisorSignoffGateFact(
            detail="Review did not complete",
            suggestion=None,
            note=None,
            for_graph=fingerprint,
            cause=cause,
        )
    )
    state_d = state.to_dict()
    await service.save_composition_state(
        session.id,
        CompositionStateData(
            sources=state_d["sources"],
            nodes=state_d["nodes"],
            edges=state_d["edges"],
            outputs=state_d["outputs"],
            metadata_=state_d["metadata"],
            is_valid=True,
            validation_errors=None,
            composer_meta={
                "completion_gates": completion_gates_meta_from_facts(facts),
                "repair_turns_used": 2,
            },
        ),
        provenance="session_seed",
    )
    before = await service.get_current_state(session.id)
    assert before is not None
    calls = []
    reviews = []
    identities = []
    checkpoint_service = ComposerServiceImpl.for_trained_operator(
        catalog=app.state.catalog_service, settings=app.state.settings, sessions_service=service
    )

    async def clean_sdk(**kwargs):
        assert adjudicates and reviews == [], "Only one actual END response is supplied for the clean-decision branch"
        assert kwargs["model"] == app.state.settings.composer_advisor_model
        assert "tools" not in kwargs and kwargs["num_retries"] == kwargs["max_retries"] == 0
        assert kwargs["response_format"]["json_schema"]["name"] == "advisor_end_response"
        payload = kwargs["messages"][1]["content"].rsplit(_ADVISOR_UNTRUSTED_SUMMARY_BEGIN + "\n", 1)[1]
        review = json.loads(payload.split("\n" + _ADVISOR_UNTRUSTED_SUMMARY_END, 1)[0])
        assert review["current_request"] == "Please retry the review"
        assert review["candidate_reply"] == "Review complete"
        assert review["saved_effects"]["saved_revision_count"] == 0 and review["saved_effects"]["net_changed"] is False
        reviews.append(review)
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "verdict": "CLEAN",
                                "category": "other",
                                "steps": [],
                                "findings": "",
                                "note": None,
                                "publication_verdict": "ADMIT",
                            }
                        ),
                    },
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 20},
        )

    monkeypatch.setattr(litellm, "acompletion", clean_sdk)

    class DecisionComposer:
        async def compose(self, message, chat_messages, state: CompositionState, **kwargs):
            del chat_messages
            assert kwargs["completion_gates"] == facts
            calls.append(state.version)
            # The validation-only control forces a save but is not allowed to
            # erase the prior advisor fact merely because preflight is green.
            result_state = state if adjudicates else replace(state, version=state.version + 1)
            identity = None
            recorder = BufferingRecorder()
            if adjudicates:
                evidence = TurnEvidence(
                    session_id=str(session.id),
                    session_operation_context=kwargs["session_operation_context"],
                    request_id=kwargs["user_message_id"],
                    request=message,
                    initial_state_id=kwargs["current_state_id"],
                    initial_state=state,
                )
                review = evidence.review_candidate(candidate="Review complete", request=message, current_state=state)
                verdict = await checkpoint_service._advisor_checkpoint._run_advisor_checkpoint(
                    phase="end",
                    state=state,
                    session_id=str(session.id),
                    recorder=recorder,
                    user_message=message,
                    session_operation_context=kwargs["session_operation_context"],
                    provider_owner=kwargs["provider_owner"],
                    publication_review=review,
                )
                assert verdict.ok and not verdict.blocking and verdict.publication_verdict == "ADMIT"
                assert verdict.publication_binding == review.binding
                identity = AdvisorGraphReviewIdentity(binding=verdict.publication_binding, for_graph=fingerprint, source="model")
                identities.append(identity)
            return ComposerResult(
                message="Review complete",
                state=result_state,
                runtime_preflight=_green_result(),
                advisor_gate_decision=AdvisorGatePassed(fingerprint) if adjudicates else None,
                graph_review_identity=identity,
                llm_calls=recorder.llm_calls,
            )

    app.state.composer_service = DecisionComposer()
    if route == "recompose":
        user_message = await service.add_message(session.id, "user", "Please retry the review", writer_principal="route_user_message")
        request_body = {"expected_user_message_id": str(user_message.id), "operation_id": str(uuid4()), "state_id": str(before.id)}
    else:
        request_body = {"content": "Please retry the review", "operation_id": str(uuid4()), "state_id": str(before.id)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        settled = await submit_and_settle(client, app, path=f"/api/sessions/{session.id}/{route}", body=request_body)
    assert settled.final.status_code == 200, settled.final.text
    response_body = settled.result()
    assert calls == [before.version]
    after = await service.get_current_state(session.id)
    assert after is not None
    assert (after.id == before.id) is adjudicates
    assert after.version == before.version + int(not adjudicates)
    if adjudicates:
        assert after.review_projection is not None
        assert after.review_projection.stored.id == before.id
        assert after.review_projection.stored.composer_meta == before.composer_meta
    rebuilt = state_from_record(after)
    assert completion_gate_fingerprint(rebuilt) == fingerprint
    reloaded_facts = parse_completion_gates(after.composer_meta)
    assert response_body["state"]["id"] == str(after.id)
    assert response_body["message"]["composition_state_id"] == str(after.id)
    assert parse_completion_gates(response_body["state"]["composer_meta"]) == reloaded_facts
    assert reloaded_facts == (CompletionGateFacts(advisor_signoff=None) if adjudicates else facts)
    assert merge_completion_gates(_green_result(), reloaded_facts, rebuilt).readiness.completion_ready is adjudicates
    assert len(reviews) == len(identities) == int(adjudicates)
    stored = await service.get_messages(session.id, limit=None)
    pass_records = [
        row.tool_calls[0]["pass"]
        for row in stored
        if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    ]
    graph_records = [
        row.tool_calls[0]["review"]
        for row in stored
        if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_GRAPH_REVIEW_AUDIT_KIND
    ]
    assert len(pass_records) == len(graph_records) == int(adjudicates)
    if adjudicates:
        assert pass_records[0]["verdict"] == "clean" and pass_records[0]["publication_verdict"] == "ADMIT"
        assert pass_records[0]["provider_attempts"] == 1 and pass_records[0]["first_attempt_accepted"] is True
        assert pass_records[0]["publication_binding"] == identities[0].binding == graph_records[0]["review_binding"]
        assert graph_records[0]["state_id"] == str(before.id) and graph_records[0]["action"] == "passed"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["messages", "recompose"])
async def test_unchanged_graph_first_block_and_changed_failure_cause_are_saved(tmp_path, route, monkeypatch):
    import json

    import litellm

    from elspeth.web.composer.advisor_audit import (
        ADVISOR_CHECKPOINT_PASS_AUDIT_KIND,
        ADVISOR_GRAPH_REVIEW_AUDIT_KIND,
        AdvisorGraphReviewIdentity,
    )
    from elspeth.web.composer.advisor_context import _ADVISOR_UNTRUSTED_SUMMARY_BEGIN, _ADVISOR_UNTRUSTED_SUMMARY_END
    from elspeth.web.composer.audit import BufferingRecorder
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.composer.turn_effects import TurnEvidence

    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Advisor failures", "local")
    state = _make_authoring_valid_partial("advisor-failures")
    state_d = state.to_dict()
    await service.save_composition_state(
        session.id,
        CompositionStateData(
            sources=state_d["sources"],
            nodes=state_d["nodes"],
            edges=state_d["edges"],
            outputs=state_d["outputs"],
            metadata_=state_d["metadata"],
            is_valid=True,
            validation_errors=None,
            composer_meta={"completion_gates": {"schema_version": 2}},
        ),
        provenance="session_seed",
    )
    fingerprint = completion_gate_fingerprint(state)
    causes = [AdvisorBlockCause.UNAVAILABLE, AdvisorBlockCause.MALFORMED, AdvisorBlockCause.MALFORMED]
    identities = []
    checkpoint_service = ComposerServiceImpl.for_trained_operator(
        catalog=app.state.catalog_service, settings=app.state.settings, sessions_service=service
    )

    class FailureComposer:
        async def compose(self, message, chat_messages, state, **kwargs):
            del chat_messages
            assert causes, "Only the original three negative review outcomes are supplied"
            expected_cause = causes.pop(0)
            evidence = TurnEvidence(
                session_id=str(session.id),
                session_operation_context=kwargs["session_operation_context"],
                request_id=kwargs["user_message_id"],
                request=message,
                initial_state_id=kwargs["current_state_id"],
                initial_state=state,
            )
            review = evidence.review_candidate(candidate="Review failed", request=message, current_state=state)
            attempts = []

            async def failing_sdk(**request):
                assert request["model"] == app.state.settings.composer_advisor_model
                assert "tools" not in request and request["num_retries"] == request["max_retries"] == 0
                assert request["response_format"]["json_schema"]["name"] == "advisor_end_response"
                assert len(attempts) < (1 if expected_cause is AdvisorBlockCause.UNAVAILABLE else 2), "No extra END response is supplied"
                payload = request["messages"][1]["content"].rsplit(_ADVISOR_UNTRUSTED_SUMMARY_BEGIN + "\n", 1)[1]
                supplied = json.loads(payload.split("\n" + _ADVISOR_UNTRUSTED_SUMMARY_END, 1)[0])
                assert supplied["current_request"] == "Retry review" and supplied["candidate_reply"] == "Review failed"
                assert supplied["saved_effects"]["saved_revision_count"] == 0 and supplied["saved_effects"]["net_changed"] is False
                attempts.append(supplied)
                if expected_cause is AdvisorBlockCause.UNAVAILABLE:
                    raise litellm.AuthenticationError(
                        message="fixture advisor unavailable", llm_provider="anthropic", model=request["model"]
                    )
                return litellm.ModelResponse(
                    model=request["model"],
                    choices=[{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "not structured JSON"}}],
                    usage={"prompt_tokens": 10, "completion_tokens": 20},
                )

            monkeypatch.setattr(litellm, "acompletion", failing_sdk)
            recorder = BufferingRecorder()
            verdict = await checkpoint_service._advisor_checkpoint._run_advisor_checkpoint(
                phase="end",
                state=state,
                session_id=str(session.id),
                recorder=recorder,
                user_message=message,
                session_operation_context=kwargs["session_operation_context"],
                provider_owner=kwargs["provider_owner"],
                publication_review=review,
            )
            assert verdict.ok is False and verdict.publication_verdict is None and verdict.publication_binding is None
            source = "unavailable" if verdict.failure_class == "unavailable" else "malformed"
            actual_cause = AdvisorBlockCause.UNAVAILABLE if source == "unavailable" else AdvisorBlockCause.MALFORMED
            assert actual_cause is expected_cause
            assert len(attempts) == (1 if actual_cause is AdvisorBlockCause.UNAVAILABLE else 2)
            identity = AdvisorGraphReviewIdentity(binding=review.binding, for_graph=fingerprint, source=source)
            identities.append(identity)
            return ComposerResult(
                message="Review failed",
                state=state,
                runtime_preflight=_green_result(),
                advisor_gate_decision=AdvisorGateBlocked(
                    AdvisorSignoffGateFact(
                        detail="Review did not complete",
                        suggestion=None,
                        note=None,
                        for_graph=fingerprint,
                        cause=actual_cause,
                    )
                ),
                graph_review_identity=identity,
                llm_calls=recorder.llm_calls,
            )

    app.state.composer_service = FailureComposer()
    previous = await service.get_current_state(session.id)
    assert previous is not None
    original = previous
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for cause, should_save in (
            (AdvisorBlockCause.UNAVAILABLE, True),
            (AdvisorBlockCause.MALFORMED, True),
            (AdvisorBlockCause.MALFORMED, False),
        ):
            if route == "recompose":
                user_message = await service.add_message(session.id, "user", "Retry review", writer_principal="route_user_message")
                request_body = {
                    "expected_user_message_id": str(user_message.id),
                    "operation_id": str(uuid4()),
                    "state_id": str(previous.id),
                }
            else:
                request_body = {"content": "Retry review", "operation_id": str(uuid4()), "state_id": str(previous.id)}
            settled = await submit_and_settle(client, app, path=f"/api/sessions/{session.id}/{route}", body=request_body)
            assert settled.final.status_code == 200, settled.final.text
            response = settled.result()
            record = await service.get_current_state(session.id)
            assert record is not None
            assert record.id == original.id and record.version == original.version
            assert record.review_projection is not None
            assert record.review_projection.stored.composer_meta == original.composer_meta
            assert completion_gate_fingerprint(state_from_record(record)) == fingerprint
            fact = parse_completion_gates(record.composer_meta).advisor_signoff
            assert fact is not None and fact.cause is cause
            assert (record.composer_meta != previous.composer_meta) is should_save
            assert parse_completion_gates(response["state"]["composer_meta"]).advisor_signoff == fact
            assert (
                merge_completion_gates(
                    _green_result(), parse_completion_gates(record.composer_meta), state_from_record(record)
                ).readiness.completion_ready
                is False
            )
            rows = await service.get_messages(session.id, limit=None)
            reviews = [
                row.tool_calls[0]["review"]
                for row in rows
                if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_GRAPH_REVIEW_AUDIT_KIND
            ]
            passes = [
                row.tool_calls[0]["pass"]
                for row in rows
                if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
            ]
            assert len(reviews) == len(passes) == len(identities)
            assert reviews[-1]["state_id"] == str(original.id) and reviews[-1]["action"] == "blocked"
            assert reviews[-1]["review_binding"] == identities[-1].binding
            assert reviews[-1]["review_source"] == identities[-1].source == passes[-1]["verdict"]
            assert reviews[-1]["gates"]["advisor_signoff"]["cause"] == cause.value
            assert passes[-1]["publication_binding"] is None and passes[-1]["publication_verdict"] is None
            assert passes[-1]["provider_attempts"] == (1 if cause is AdvisorBlockCause.UNAVAILABLE else 2)
            previous = record
    assert causes == [] and len({identity.binding for identity in identities}) == 3
