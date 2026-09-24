"""Graph findings must not erase the advisor's independent terminal verdict."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from elspeth.web.composer.advisor_decision import AdvisorBlockCause, AdvisorGateBlocked
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.service import (
    AdvisorCheckpointVerdict,
    ComposerServiceImpl,
    _admit_composer_llm_completion,
    _AdvisorCheckpointComposeDeadlineExpired,
)
from elspeth.web.execution.schemas import ValidationError, ValidationReadiness, ValidationResult
from elspeth.web.sessions.protocol import SessionServiceProtocol
from tests.unit.web.execution.test_validation_complaint_triage import complaint_triage_state

from .conftest import _fake_llm_response
from .test_advisor_checkpoint import (
    _AsyncRecorder,
    _compose_context,
    _make_settings,
    _mock_catalog,
    _pending_handoff_preflight,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_pending_graph_error_preserves_fresh_advisor_block_and_deadline(expired: bool) -> None:
    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
    state = complaint_triage_state(pending=True, narrow_consumer=True)
    service._missing_pending_interpretation_review_sites = _AsyncRecorder(return_value=())
    service._run_advisor_checkpoint = _AsyncRecorder(
        return_value=AdvisorCheckpointVerdict(
            ok=True, blocking=True, findings_text="FLAGGED: requested behavior was removed", note="Requested behavior was removed"
        )
    )
    structural_message = "tidy_columns requires str while attach_sla emits Any"
    findings = ValidationResult(
        is_valid=False,
        checks=[],
        errors=[
            ValidationError(
                component_id="tidy_columns", component_type="transform", message=structural_message, suggestion=None, error_code=None
            )
        ],
        readiness=ValidationReadiness(authoring_valid=False, execution_ready=False, completion_ready=False, blockers=[]),
    )
    masked_preflight = _AsyncRecorder(return_value=findings)
    service._pending_handoff_outstanding_findings = masked_preflight
    surface = _AsyncRecorder(return_value=None)
    service.surface_pending_interpretation_reviews = surface
    messages = _AsyncRecorder(return_value=None)
    service._sessions_service = MagicMock(spec=SessionServiceProtocol, add_message=messages)
    session_id = str(uuid4())
    deadline = asyncio.get_running_loop().time() + (-1 if expired else 60)

    async def evaluate():
        return await service._evaluate_terminal_no_tool_advisor_gate(
            state=state,
            session_id=session_id,
            session_operation_context=_compose_context(session_id),
            current_state_id=str(uuid4()),
            assistant_message=_admit_composer_llm_completion(_fake_llm_response(content="The pipeline is ready.")).message,
            llm_messages=[],
            recorder=BufferingRecorder(),
            progress=None,
            advisor_checkpoint_passes_used=0,
            repair_turns_used=2,
            persisted_assistant_message_id=None,
            persisted_assistant_content=None,
            persisted_tool_call_turn=False,
            allow_repair_continue=False,
            runtime_preflight=_pending_handoff_preflight(),
            user_message="Keep the requested behavior and fix the graph",
            user_id="alice",
            runtime_preflight_cache=service._new_runtime_preflight_cache(),
            initial_version=0,
            session_scope="pending-advisor-terminal",
            plugin_snapshot=None,
            deadline=deadline,
        )

    if expired:
        with pytest.raises(_AdvisorCheckpointComposeDeadlineExpired):
            await evaluate()
        masked_preflight.assert_not_awaited()
        surface.assert_not_awaited()
        messages.assert_not_awaited()
        return

    outcome = await evaluate()
    assert outcome.action == "return"
    result = outcome.result
    assert result is not None
    assert isinstance(result.advisor_gate_decision, AdvisorGateBlocked)
    assert result.advisor_gate_decision.fact.cause is AdvisorBlockCause.GRAPH_REJECTED
    assert result.advisor_gate_decision.fact.note == "Requested behavior was removed"
    assert structural_message in result.message
    assert result.runtime_preflight is not None
    assert result.runtime_preflight.readiness.completion_ready is False
    surface.assert_not_awaited()
    assert masked_preflight.await_count == 1
    assert masked_preflight.await_args.kwargs["deadline"] == deadline
    assert messages.await_count == 2
    disclosure, publication = messages.calls
    assert disclosure.kwargs["tool_calls"][0]["origin"] == "advisor_signoff_withheld"
    assert result.advisor_terminal_publication is not None
    assert result.advisor_terminal_publication.branch == "terminal_block"
    assert publication.kwargs["tool_calls"][0]["publication"] == result.advisor_terminal_publication.to_dict()
