"""Returned checkpoint text is retained before any format retry decision."""

from dataclasses import replace

import litellm
import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.advisor_invocation_outcome import AdvisorInvocationReturned, observe_advisor_invocation
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointIncompleteResponseObservation,
    CheckpointReturnedResponseObservation,
    observe_checkpoint_returned_response,
)
from elspeth.web.composer.continuation_codec import decode_continuation_response, decode_retained_physical_audit_call
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.tools.sessions import ADVISOR_TRIGGER_DETERMINISTIC_EARLY, ADVISOR_TRIGGER_DETERMINISTIC_END
from tests.unit.web.composer.test_advisor_invocation_outcome import USER, advisor_case, reply
from tests.unit.web.composer.test_advisor_invocation_outcome import operation_store as _operation_store
from tests.unit.web.composer.test_continuation_checkpoint_observation import _large_audit

operation_store = _operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ("early", "end"))
async def test_actual_malformed_checkpoint_text_has_complete_original_return_data(operation_store, tmp_path, monkeypatch, phase):
    advisor, provider, context, coordinator, _requests, _custodies, _audits, _projections = advisor_case(
        operation_store, tmp_path, monkeypatch
    )
    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        return reply("A reachable provider returned text outside the checkpoint JSON grammar.")

    monkeypatch.setattr(litellm, "acompletion", completion)
    trigger = ADVISOR_TRIGGER_DETERMINISTIC_EARLY if phase == "early" else ADVISOR_TRIGGER_DETERMINISTIC_END
    try:
        with prompt_guard_user_scope(USER):
            returned = await observe_advisor_invocation(
                advisor,
                {"trigger": trigger, "problem_summary": "Review the graph", "recent_errors": [], "attempted_actions": []},
                recorder=BufferingRecorder(),
                provider_owner=provider,
                structured_output=True,
            )
        assert type(returned) is AdvisorInvocationReturned
        physical = returned.observation
        assert physical.attempt is not None
        complete = observe_checkpoint_returned_response(
            physical, phase=phase, trigger=trigger, attempt=physical.attempt, context=context, env_ref_names=frozenset()
        )
        assert type(complete) is CheckpointReturnedResponseObservation
        assert decode_continuation_response(complete.response).message == physical.message
        assert decode_retained_physical_audit_call(complete.response) == physical.audit_call
        assert len(calls) == 1

        # Pure size control: the original call path above produced malformed
        # text, but the separately enlarged valid audit must not grant a retry
        # or a decision just because grammar admission would fail.
        oversized = _large_audit(physical)
        incomplete = observe_checkpoint_returned_response(
            oversized, phase=phase, trigger=trigger, attempt=physical.attempt, context=context, env_ref_names=frozenset()
        )
        assert type(incomplete) is CheckpointIncompleteResponseObservation
        assert incomplete.evidence_status == "incomplete" and incomplete.retryable is False
        assert incomplete.representation == "physical_audit"
        assert len(calls) == 1
        with pytest.raises(AuditIntegrityError, match="guidance or accounting"):
            observe_checkpoint_returned_response(
                replace(oversized, guidance="substituted"),
                phase=phase,
                trigger=trigger,
                attempt=physical.attempt,
                context=context,
                env_ref_names=frozenset(),
            )
        coordinator.assert_completed()
    finally:
        operation_store[1].release(context)
