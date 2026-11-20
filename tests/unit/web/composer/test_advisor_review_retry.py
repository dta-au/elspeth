"""The extra review round for a newly flagged graph, and its admitted-reply fallback.

Rule: an admitted reply over a newly FLAGGED graph gets one more repair/review
round only when this turn's edits changed the saved graph. Every other
admitted reply ends the turn on the first pass. If the extra round times out,
errors, or ends with its reply withheld, the turn publishes the reply the
first pass admitted, and the block stands. The exception is a second review
that cleared the graph (CLEAN) but withheld its reply: the turn keeps the
plain withheld outcome, so no FLAGGED block outlives the review that lifted it.
"""

import asyncio
import uuid
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from elspeth.web.composer.advisor_audit import AdvisorGraphReviewIdentity
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointVerdict, _AdvisorCheckpointComposeDeadlineExpired
from elspeth.web.composer.advisor_decision import AdvisorGatePassed
from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.composer.turn_effects import TurnEvidence, authored_state_hash
from elspeth.web.execution.completion_gates import completion_gate_fingerprint
from elspeth.web.interpretation_state import interpretation_sites
from tests.unit.web.composer import test_advisor_checkpoint as checkpoint_fixtures

make_service = checkpoint_fixtures.make_service
simple_state = checkpoint_fixtures.simple_state
clean_runnable_state = checkpoint_fixtures.clean_runnable_state

_REQUEST = "Preserve the existing node content and make only the requested wiring change."
_FIRST = "FIRST_ADMITTED_REPLY: the wiring change is in place."
_SECOND = "SECOND_REPLY_FROM_THE_EXTRA_ROUND: the wiring change is in place."


def _turn_evidence(state, *, changed: bool) -> tuple[TurnEvidence, object]:
    """Evidence for a turn that started at an earlier draft (changed) or at ``state`` itself."""
    session_id = str(uuid.uuid4())
    context = checkpoint_fixtures._compose_context(session_id)
    start = replace(state, metadata=replace(state.metadata, name="Earlier draft")) if changed else state
    evidence = TurnEvidence(
        session_id=session_id,
        session_operation_context=context,
        request_id="advisor-unit-request",
        request=_REQUEST,
        initial_state_id="cs0",
        initial_state=start,
    )
    if changed:
        # What accept_checkpoint records once the turn's committed edit lands.
        evidence.effects = replace(
            evidence.effects,
            latest_verified_state=state,
            latest_verified_state_id="cs1",
            latest_committed_state_id="cs1",
            latest_authored_state_hash=authored_state_hash(state),
            net_changed_fields=("metadata",),
        )
    assert evidence.effects.net_changed is changed
    return evidence, context


def _scripted_reviews(service, *verdicts):
    """Replace the END review with scripted verdicts; ADMIT/WITHHOLD bind to the reviewed candidate."""
    script = iter(verdicts)
    seen: list[str] = []

    async def review(**kwargs):
        seen.append(kwargs["publication_review"].binding)
        verdict = next(script)
        if isinstance(verdict, BaseException):
            raise verdict
        if verdict.publication_verdict is not None:
            verdict = replace(verdict, publication_binding=kwargs["publication_review"].binding)
        return verdict

    service._advisor_checkpoint._run_advisor_checkpoint = review
    return seen


def _flagged(publication: str | None) -> AdvisorCheckpointVerdict:
    return AdvisorCheckpointVerdict(
        ok=True, blocking=True, findings_text="FLAGGED: recheck the routing contract.", publication_verdict=publication
    )


def _clean(publication: str) -> AdvisorCheckpointVerdict:
    return AdvisorCheckpointVerdict(ok=True, blocking=False, findings_text="CLEAN", publication_verdict=publication)


async def _first_pass(service, state, evidence, context, messages):
    return await checkpoint_fixtures.drive_try_terminate(
        service,
        state,
        advisor_checkpoint_passes_used=0,
        llm_messages=messages,
        message=_REQUEST,
        candidate=_FIRST,
        turn_evidence=evidence,
        session_operation_context=context,
    )


async def _second_pass(service, state, evidence, context, first, *, deadline=None):
    return await checkpoint_fixtures.drive_try_terminate(
        service,
        state,
        advisor_checkpoint_passes_used=first.advisor_passes_delta,
        llm_messages=[],
        message=_REQUEST,
        candidate=_SECOND,
        turn_evidence=evidence,
        session_operation_context=context,
        advisor_review_state=first.advisor_review_state,
        deadline=deadline,
    )


def _withheld_rows(service) -> list[tuple[str, str]]:
    return [
        (envelope["origin"], call.args[2])
        for call in service._sessions_service.add_message.calls
        for envelope in call.kwargs["tool_calls"]
        if "origin" in envelope and envelope["origin"] in {"advisor_publication_withheld", "compose_deadline_expired"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("followup_verdict", ["CLEAN", "FLAGGED"])
async def test_changed_graph_newly_flagged_gets_the_extra_round(make_service, clean_runnable_state, followup_verdict):
    service = make_service()
    state = clean_runnable_state
    evidence, context = _turn_evidence(state, changed=True)
    replies = iter(
        [
            checkpoint_fixtures._checkpoint_reply(
                "FLAGGED", findings="Recheck the requested routing contract.", publication_verdict="ADMIT"
            ),
            checkpoint_fixtures._checkpoint_reply(
                followup_verdict,
                findings="The routing concern remains." if followup_verdict == "FLAGGED" else "",
                publication_verdict="ADMIT",
            ),
        ]
    )
    provider = checkpoint_fixtures._AsyncRecorder(side_effect=lambda *args, **kwargs: (next(replies), {}))
    service._advisor_checkpoint._call_advisor_with_audit = provider
    evaluate = service._completion._evaluate_terminal_no_tool_advisor_gate
    gate_spy = AsyncMock(wraps=evaluate)
    service._completion._evaluate_terminal_no_tool_advisor_gate = gate_spy
    messages = []

    first = await _first_pass(service, state, evidence, context, messages)

    assert first.action == "continue"
    assert first.advisor_passes_delta == 1
    assert provider.await_count == 1
    assert "Recheck the requested routing contract." in messages[-1]["content"]
    assert first.advisor_review_state.admitted_fallback.reply == _FIRST
    gate_arguments = dict(gate_spy.call_args.kwargs)
    gate_arguments.update(
        advisor_checkpoint_passes_used=first.advisor_passes_delta,
        advisor_review_state=first.advisor_review_state,
    )
    second = await evaluate(**gate_arguments)

    assert provider.await_count == 2
    passes = [
        envelope["pass"]
        for call in service._sessions_service.add_message.calls
        for envelope in call.kwargs["tool_calls"]
        if envelope["_kind"] == "advisor_checkpoint_pass_audit"
    ]
    assert [(entry["pass_index"], entry["verdict"]) for entry in passes] == [(1, "flagged"), (2, followup_verdict.lower())]
    if followup_verdict == "CLEAN":
        assert second.action == "fall_through"
        assert second.advisor_gate_decision == AdvisorGatePassed(for_graph=completion_gate_fingerprint(state))
    else:
        # A repeated FLAGGED stays blocked once the budget is spent.
        assert second.action == "return"
        assert second.advisor_passes_delta == 1
        assert second.result.runtime_preflight.readiness.completion_ready is False
        assert second.result.advisor_terminal_publication.reason == "flagged_final_pass"


@pytest.mark.asyncio
async def test_turn_that_changed_nothing_ends_on_the_first_admitted_reply(make_service, clean_runnable_state):
    service = make_service()
    state = clean_runnable_state
    evidence, context = _turn_evidence(state, changed=False)
    reviewed = _scripted_reviews(service, _flagged("ADMIT"))
    messages = []

    outcome = await _first_pass(service, state, evidence, context, messages)

    assert outcome.action == "return"
    assert len(reviewed) == 1
    assert messages == []
    assert outcome.advisor_passes_delta == 1
    assert _FIRST in outcome.result.message
    assert outcome.result.runtime_preflight.readiness.completion_ready is False
    assert outcome.result.advisor_terminal_publication.reason == "flagged_no_repair"


@pytest.mark.asyncio
async def test_already_blocked_graph_ends_on_the_first_admitted_reply_even_after_a_change(make_service, clean_runnable_state):
    service = make_service()
    state = clean_runnable_state
    evidence, context = _turn_evidence(state, changed=True)
    reviewed = _scripted_reviews(service, _flagged("ADMIT"))
    gates = checkpoint_fixtures._blocked_facts_for(state)

    outcome = await checkpoint_fixtures.drive_try_terminate(
        service,
        state,
        advisor_checkpoint_passes_used=0,
        message=_REQUEST,
        candidate=_FIRST,
        turn_evidence=evidence,
        session_operation_context=context,
        completion_gates=gates,
    )

    assert outcome.action == "return"
    assert len(reviewed) == 1
    assert _FIRST in outcome.result.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_verdict",
    [
        pytest.param(_flagged("WITHHOLD"), id="flagged-withheld"),
        pytest.param(checkpoint_fixtures._unavailable_verdict(), id="advisor-unavailable"),
        pytest.param(
            AdvisorCheckpointVerdict(ok=False, blocking=False, findings_text="malformed", failure_class="malformed"), id="advisor-malformed"
        ),
    ],
)
async def test_second_reply_not_admitted_publishes_the_first_admitted_reply(make_service, clean_runnable_state, second_verdict):
    service = make_service()
    state = clean_runnable_state
    evidence, context = _turn_evidence(state, changed=True)
    reviewed = _scripted_reviews(service, _flagged("ADMIT"), second_verdict)

    first = await _first_pass(service, state, evidence, context, [])
    assert first.action == "continue"
    second = await _second_pass(service, state, evidence, context, first)

    assert len(reviewed) == 2
    assert second.action == "return"
    result = second.result
    assert _FIRST in result.message
    assert _SECOND not in result.message
    # The block stands under the first pass's FLAGGED verdict.
    assert result.runtime_preflight.readiness.completion_ready is False
    assert result.advisor_terminal_publication.reason == "flagged_final_pass"
    assert result.raw_assistant_content == _FIRST
    # The reply the advisor did not admit stays private.
    assert _withheld_rows(service) == [("advisor_publication_withheld", _SECOND)]
    # The review identity is the one that admitted the published reply: pass 1.
    assert second.result.graph_review_identity == AdvisorGraphReviewIdentity(
        binding=reviewed[0], for_graph=completion_gate_fingerprint(state), source="model"
    )


@pytest.mark.asyncio
async def test_clean_second_review_with_withheld_reply_keeps_the_plain_withheld_outcome(make_service, clean_runnable_state):
    """The latest review cleared the graph: no fallback under a FLAGGED block it no longer holds."""
    service = make_service()
    state = clean_runnable_state
    evidence, context = _turn_evidence(state, changed=True)
    reviewed = _scripted_reviews(service, _flagged("ADMIT"), _clean("WITHHOLD"))

    first = await _first_pass(service, state, evidence, context, [])
    assert first.action == "continue"
    second = await _second_pass(service, state, evidence, context, first)

    assert len(reviewed) == 2
    assert second.action == "return"
    result = second.result
    assert _FIRST not in result.message
    assert _SECOND not in result.message
    assert result.advisor_terminal_publication.reason == "publication_withheld"
    assert second.result.graph_review_identity == AdvisorGraphReviewIdentity(
        binding=reviewed[1], for_graph=completion_gate_fingerprint(state), source="model"
    )
    assert _withheld_rows(service) == [("advisor_publication_withheld", _SECOND)]


def _state_without_review_sites(state):
    """The same graph with its LLM step replaced, so no interpretation review is pending."""
    (node,) = state.nodes
    plain = replace(state, nodes=(replace(node, plugin="passthrough", options={}),))
    assert interpretation_sites(plain) == ()
    return plain


@pytest.mark.asyncio
async def test_timeout_in_the_second_review_publishes_the_first_admitted_reply(make_service, clean_runnable_state):
    service = make_service()
    state = _state_without_review_sites(clean_runnable_state)
    evidence, context = _turn_evidence(state, changed=True)
    reviewed = _scripted_reviews(service, _flagged("ADMIT"), _AdvisorCheckpointComposeDeadlineExpired())

    first = await _first_pass(service, state, evidence, context, [])
    assert first.action == "continue"
    second = await _second_pass(service, state, evidence, context, first, deadline=asyncio.get_running_loop().time() + 60)

    assert len(reviewed) == 2
    assert second.action == "return"
    result = second.result
    assert _FIRST in result.message
    assert _SECOND not in result.message
    assert result.runtime_preflight.readiness.completion_ready is False
    assert result.advisor_terminal_publication.reason == "flagged_no_repair"
    assert _withheld_rows(service) == [("compose_deadline_expired", _SECOND)]


@pytest.mark.asyncio
async def test_timeout_fallback_declines_a_graph_with_pending_review_sites(make_service, clean_runnable_state):
    """Its blocked result needs the masked re-validation the expired deadline cannot run: the timeout stands."""
    service = make_service()
    state = clean_runnable_state
    assert interpretation_sites(state) != ()
    evidence, context = _turn_evidence(state, changed=True)
    _scripted_reviews(service, _flagged("ADMIT"), _AdvisorCheckpointComposeDeadlineExpired())

    first = await _first_pass(service, state, evidence, context, [])
    with pytest.raises(ComposerConvergenceError):
        await _second_pass(service, state, evidence, context, first, deadline=asyncio.get_running_loop().time() + 60)
    assert _withheld_rows(service) == [("compose_deadline_expired", _SECOND)]


@pytest.mark.asyncio
async def test_no_fallback_when_the_extra_round_changed_the_graph(make_service, clean_runnable_state):
    """The kept reply was admitted for the earlier graph; it is not published over a different one."""
    service = make_service()
    state = clean_runnable_state
    evidence, context = _turn_evidence(state, changed=True)
    reviewed = _scripted_reviews(service, _flagged("ADMIT"), _flagged("WITHHOLD"))

    first = await _first_pass(service, state, evidence, context, [])
    repaired = replace(state, metadata=replace(state.metadata, name="Repaired draft"))
    second = await _second_pass(service, repaired, evidence, context, first)

    assert len(reviewed) == 2
    assert second.action == "return"
    assert _FIRST not in second.result.message
    assert _SECOND not in second.result.message
    assert second.result.advisor_terminal_publication.reason == "flagged_final_pass"


@pytest.mark.asyncio
async def test_extra_round_never_exceeds_the_pass_budget(make_service, clean_runnable_state):
    service = make_service()
    state = clean_runnable_state
    evidence, context = _turn_evidence(state, changed=True)
    reviewed = _scripted_reviews(service, _flagged("ADMIT"))
    max_passes = service._completion._max_advisor_checkpoint_passes

    outcome = await checkpoint_fixtures.drive_try_terminate(
        service,
        state,
        advisor_checkpoint_passes_used=max_passes,
        message=_REQUEST,
        candidate=_FIRST,
        turn_evidence=evidence,
        session_operation_context=context,
    )

    assert reviewed == []
    assert outcome.action == "return"
    assert outcome.advisor_passes_delta == 0
    assert outcome.result.runtime_preflight.readiness.completion_ready is False


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["graph-changed", "preflight-fails"])
async def test_failed_round_fallback_checks_the_kept_reply_before_preflight(make_service, clean_runnable_state, case):
    """The composer-failure fallback runs no preflight for an unusable kept reply, and hands a failing one back to its caller.

    The caller (the compose-loop handler) then re-raises the round's original
    failure; ``test_failed_preflight_never_replaces_the_extra_round_failure``
    in ``test_run_composer_turn.py`` pins that end to end.
    """
    from elspeth.web.composer.protocol import ComposerRuntimePreflightError

    service = make_service()
    state = _state_without_review_sites(clean_runnable_state)
    evidence, context = _turn_evidence(state, changed=True)
    _scripted_reviews(service, _flagged("ADMIT"))
    first = await _first_pass(service, state, evidence, context, [])
    assert first.advisor_review_state.admitted_fallback is not None
    preflight_runs = []

    async def preflight():
        preflight_runs.append(case)
        raise ComposerRuntimePreflightError(original_exc=RuntimeError("validation crashed"), partial_state=state)

    def fallback():
        return service._completion._admitted_fallback_result(
            advisor_review_state=first.advisor_review_state,
            state=replace(state, metadata=replace(state.metadata, name="Repaired draft")) if case == "graph-changed" else state,
            runtime_preflight=preflight,
            session_id=None,
            session_operation_context=None,
            recorder=None,
            repair_turns_used=0,
            persisted_assistant_message_id=None,
            persisted_assistant_content=None,
            persisted_tool_call_turn=False,
        )

    if case == "graph-changed":
        # None tells the caller to re-raise its own timeout or provider error.
        assert await fallback() is None
        assert preflight_runs == []
    else:
        with pytest.raises(ComposerRuntimePreflightError):
            await fallback()
        assert preflight_runs == ["preflight-fails"]
