"""Advisor-only decisions survive real route, fenced write, and DB reload seams."""

from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient

from elspeth.web.composer.advisor_decision import AdvisorBlockCause, AdvisorGateBlocked, AdvisorGatePassed, AdvisorSignoffGateFact
from elspeth.web.composer.guided.protocol import GuidedStep
from elspeth.web.composer.guided.state_machine import GuidedSession, TerminalKind, TerminalReason, TerminalState
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.completion_gates import (
    CompletionGateFacts,
    completion_gate_fingerprint,
    completion_gates_meta_from_facts,
    merge_completion_gates,
    parse_completion_gates,
)
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.protocol import CompositionStateData
from tests.unit.web.sessions.test_completion_gate_roundtrip import _green_result
from tests.unit.web.sessions.test_routes import _make_app, _make_authoring_valid_partial


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["messages", "recompose"])
@pytest.mark.parametrize("cause", [AdvisorBlockCause.UNAVAILABLE, AdvisorBlockCause.MALFORMED, AdvisorBlockCause.MESSAGE_REJECTED])
@pytest.mark.parametrize("adjudicates", [True, False], ids=["clean-decision", "validation-only"])
@pytest.mark.parametrize("guided_terminal", [False, True], ids=["freeform", "guided-exit"])
async def test_advisor_recovery_is_durable_only_with_explicit_clean_decision(tmp_path, route, cause, adjudicates, guided_terminal):
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Advisor recovery", "local")
    state = _make_authoring_valid_partial("advisor-recovery")
    if guided_terminal:
        state = replace(
            state,
            guided_session=GuidedSession(
                step=GuidedStep.STEP_1_SOURCE,
                history=(),
                transition_consumed=False,
                terminal=TerminalState(kind=TerminalKind.EXITED_TO_FREEFORM, reason=TerminalReason.USER_PRESSED_EXIT, pipeline_yaml=None),
            ),
        )
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
                **({"guided_session": state.guided_session.to_dict()} if state.guided_session is not None else {}),
            },
        ),
        provenance="session_seed",
    )
    before = await service.get_current_state(session.id)
    assert before is not None
    calls = []

    class DecisionComposer:
        async def compose(self, message, chat_messages, state: CompositionState, **kwargs):
            del message, chat_messages
            assert kwargs["completion_gates"] == facts
            calls.append(state.version)
            # The validation-only control forces a save but is not allowed to
            # erase the prior advisor fact merely because preflight is green.
            result_state = state if adjudicates else replace(state, version=state.version + 1)
            return ComposerResult(
                message="Review complete",
                state=result_state,
                runtime_preflight=_green_result(),
                advisor_gate_decision=AdvisorGatePassed(fingerprint) if adjudicates else None,
            )

    app.state.composer_service = DecisionComposer()
    if route == "recompose":
        await service.add_message(session.id, "user", "Please retry the review", writer_principal="route_user_message")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/sessions/{session.id}/{route}",
            **({"json": {"content": "Please retry the review"}} if route == "messages" else {}),
        )
    assert response.status_code == 200, response.text
    assert calls == [before.version]
    after = await service.get_current_state(session.id)
    assert after is not None
    assert after.id != before.id
    assert after.version == before.version + 1
    rebuilt = state_from_record(after)
    if guided_terminal:
        assert rebuilt.guided_session is not None
        assert rebuilt.guided_session.transition_consumed is True
    assert completion_gate_fingerprint(rebuilt) == fingerprint
    reloaded_facts = parse_completion_gates(after.composer_meta)
    response_body = response.json()
    assert response_body["state"]["id"] == str(after.id)
    assert response_body["message"]["composition_state_id"] == str(after.id)
    assert parse_completion_gates(response_body["state"]["composer_meta"]) == reloaded_facts
    assert reloaded_facts == (CompletionGateFacts(advisor_signoff=None) if adjudicates else facts)
    assert merge_completion_gates(_green_result(), reloaded_facts, rebuilt).readiness.completion_ready is adjudicates


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["messages", "recompose"])
async def test_unchanged_graph_first_block_and_changed_failure_cause_are_saved(tmp_path, route):
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
    decisions = [
        AdvisorGateBlocked(
            AdvisorSignoffGateFact(
                detail="Review did not complete",
                suggestion=None,
                note=None,
                for_graph=fingerprint,
                cause=cause,
            )
        )
        for cause in (AdvisorBlockCause.UNAVAILABLE, AdvisorBlockCause.MALFORMED, AdvisorBlockCause.MALFORMED)
    ]

    class FailureComposer:
        async def compose(self, message, chat_messages, state, **kwargs):
            del message, chat_messages, kwargs
            return ComposerResult(
                message="Review failed", state=state, runtime_preflight=_green_result(), advisor_gate_decision=decisions.pop(0)
            )

    app.state.composer_service = FailureComposer()
    previous = await service.get_current_state(session.id)
    assert previous is not None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for cause, should_save in (
            (AdvisorBlockCause.UNAVAILABLE, True),
            (AdvisorBlockCause.MALFORMED, True),
            (AdvisorBlockCause.MALFORMED, False),
        ):
            if route == "recompose":
                await service.add_message(session.id, "user", "Retry review", writer_principal="route_user_message")
            response = await client.post(
                f"/api/sessions/{session.id}/{route}", **({"json": {"content": "Retry review"}} if route == "messages" else {})
            )
            assert response.status_code == 200, response.text
            record = await service.get_current_state(session.id)
            assert record is not None
            assert (record.id != previous.id) is should_save
            assert completion_gate_fingerprint(state_from_record(record)) == fingerprint
            fact = parse_completion_gates(record.composer_meta).advisor_signoff
            assert fact is not None and fact.cause is cause
            previous = record
    assert decisions == []
