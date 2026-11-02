"""Shared END return carriers retain consumed pass state for later actions.

These controls use the existing advisor unit harness. They do not establish
the separate live SQL, physical provider or cold-result publication joins.
"""

import pytest

from elspeth.web.composer.advisor_decision import AdvisorGatePassed
from tests.unit.web.composer import test_advisor_checkpoint as advisor_fixture

make_service = advisor_fixture.make_service
simple_state = advisor_fixture.simple_state
clean_runnable_state = advisor_fixture.clean_runnable_state


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition,used", (("clean", 0), ("clean", 1), ("blocked", 1), ("repair", 0)))
async def test_shared_end_keeps_the_actual_pass_delta_and_review_state(make_service, clean_runnable_state, disposition, used):
    service = make_service()
    clean = disposition == "clean"
    candidate = "The original provider candidate remains the completed reply."
    service._advisor_checkpoint._call_advisor_with_audit = advisor_fixture._AsyncRecorder(
        return_value=(
            advisor_fixture._checkpoint_reply(
                "CLEAN" if clean else "FLAGGED",
                findings="" if clean else "The candidate still does not meet the request.",
                publication_verdict="ADMIT" if clean else "WITHHOLD",
            ),
            {},
        )
    )
    outcome = await advisor_fixture.drive_try_terminate(
        service,
        clean_runnable_state,
        advisor_checkpoint_passes_used=used,
        candidate=candidate,
    )
    assert outcome.advisor_passes_delta == 1
    assert outcome.advisor_review_state is not None
    assert outcome.advisor_review_state.completed_passes == used + 1
    assert outcome.repair_turns_delta == 0
    if disposition == "repair":
        assert outcome.action == "continue" and outcome.result is None
    else:
        assert outcome.action == "return" and outcome.result is not None
        if clean:
            assert outcome.result.message == candidate
            assert type(outcome.result.advisor_gate_decision) is AdvisorGatePassed
            assert outcome.publication_binding is not None
        else:
            assert outcome.result.runtime_preflight.readiness.completion_ready is False
