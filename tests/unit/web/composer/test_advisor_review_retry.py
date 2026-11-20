"""A newly flagged graph gets its remaining review pass, regardless of prose admission."""

from unittest.mock import AsyncMock

import pytest

from elspeth.web.composer.advisor_decision import AdvisorGatePassed
from elspeth.web.execution.completion_gates import completion_gate_fingerprint
from tests.unit.web.composer import test_advisor_checkpoint as checkpoint_fixtures

make_service = checkpoint_fixtures.make_service
simple_state = checkpoint_fixtures.simple_state
clean_runnable_state = checkpoint_fixtures.clean_runnable_state


@pytest.mark.asyncio
@pytest.mark.parametrize("followup_verdict", ["CLEAN", "FLAGGED"])
async def test_new_graph_finding_uses_remaining_pass_without_requiring_a_mutation(make_service, clean_runnable_state, followup_verdict):
    service = make_service()
    state = clean_runnable_state
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

    first = await checkpoint_fixtures.drive_try_terminate(
        service,
        state,
        advisor_checkpoint_passes_used=0,
        llm_messages=messages,
        message="Preserve the existing node content and make only the requested wiring change.",
    )

    assert first.action == "continue"
    assert first.advisor_passes_delta == 1
    assert provider.await_count == 1
    assert "Recheck the requested routing contract." in messages[-1]["content"]
    gate_arguments = dict(gate_spy.call_args.kwargs)
    gate_arguments.update(
        advisor_checkpoint_passes_used=first.advisor_passes_delta,
        advisor_review_state=first.advisor_review_state,
    )
    # The graph and candidate are unchanged. A second review must actually
    # run and clear both axes before completion; a repeated flag still blocks.
    second = await evaluate(**gate_arguments)

    assert provider.await_count == 2
    passes = [
        envelope["pass"]
        for call in service._sessions_service.add_message.calls
        for envelope in call.kwargs["tool_calls"]
        if envelope["_kind"] == "advisor_checkpoint_pass_audit"
    ]
    assert [(entry["pass_index"], entry["verdict"]) for entry in passes] == [(1, "flagged"), (2, followup_verdict.lower())]
    assert gate_arguments["state"] is state
    if followup_verdict == "CLEAN":
        assert second.action == "fall_through"
        assert second.advisor_gate_decision == AdvisorGatePassed(for_graph=completion_gate_fingerprint(state))
    else:
        assert second.action == "return"
        assert second.advisor_passes_delta == 1
        assert second.result.runtime_preflight.readiness.completion_ready is False
        assert second.result.advisor_terminal_publication.reason == "flagged_final_pass"
