"""Graph findings must not erase the advisor's independent terminal verdict."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import structlog

from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointVerdict, _AdvisorCheckpointComposeDeadlineExpired
from elspeth.web.composer.advisor_decision import AdvisorBlockCause, AdvisorGateBlocked
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.turn_effects import TurnEvidence
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
    service._interpretation_surfacing._missing_pending_interpretation_review_sites = _AsyncRecorder(return_value=())
    service._advisor_checkpoint._run_advisor_checkpoint = _AsyncRecorder(
        return_value=AdvisorCheckpointVerdict(
            ok=True, blocking=True, findings_text="FLAGGED: requested behavior was removed", note="Requested behavior was removed"
        ),
        side_effect=_AdvisorCheckpointComposeDeadlineExpired if expired else None,
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
    service._preflight.pending_handoff_outstanding_findings = masked_preflight
    surface = _AsyncRecorder(return_value=None)
    service._interpretation_surfacing.surface_pending_interpretation_reviews = surface
    messages = _AsyncRecorder(return_value=None)
    service._sessions_service = MagicMock(spec=SessionServiceProtocol, add_message=messages)
    service._advisor_checkpoint._sessions_service = service._sessions_service
    service._completion._sessions_service = service._sessions_service
    session_id = str(uuid4())
    context = _compose_context(session_id)
    current_state_id = str(uuid4())
    request = "Keep the requested behavior and fix the graph"
    evidence = TurnEvidence(
        session_id=session_id,
        session_operation_context=context,
        request_id="pending-advisor-control",
        request=request,
        initial_state_id=current_state_id,
        initial_state=state,
    )
    deadline = asyncio.get_running_loop().time() + (-1 if expired else 60)

    async def evaluate():
        return await service._completion._evaluate_terminal_no_tool_advisor_gate(
            state=state,
            turn_evidence=evidence,
            session_id=session_id,
            session_operation_context=context,
            current_state_id=current_state_id,
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
            user_message=request,
            user_id="alice",
            runtime_preflight_cache=service._preflight.new_cache(),
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
    assert "The pipeline is ready." not in result.message
    assert messages.await_count == 3
    withheld, disclosure, publication = messages.calls
    assert withheld.kwargs["tool_calls"][0]["origin"] == "advisor_publication_withheld"
    assert disclosure.kwargs["tool_calls"][0]["origin"] == "advisor_signoff_withheld"
    assert result.advisor_terminal_publication is not None
    assert result.advisor_terminal_publication.branch == "terminal_block"
    assert publication.kwargs["tool_calls"][0]["publication"] == result.advisor_terminal_publication.to_dict()


async def _advisor_terminal_with_findings(findings: ValidationResult | None):
    """The same advisor-blocked terminal, with or without structural findings."""
    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
    state = complaint_triage_state(pending=True, narrow_consumer=True)
    service._interpretation_surfacing._missing_pending_interpretation_review_sites = _AsyncRecorder(return_value=())
    service._advisor_checkpoint._run_advisor_checkpoint = _AsyncRecorder(
        return_value=AdvisorCheckpointVerdict(
            ok=True, blocking=True, findings_text="FLAGGED: requested behavior was removed", note="Requested behavior was removed"
        ),
    )
    service._preflight.pending_handoff_outstanding_findings = _AsyncRecorder(return_value=findings)
    surface = _AsyncRecorder(return_value=None)
    service._interpretation_surfacing.surface_pending_interpretation_reviews = surface
    service._sessions_service = MagicMock(spec=SessionServiceProtocol, add_message=_AsyncRecorder(return_value=None))
    service._advisor_checkpoint._sessions_service = service._sessions_service
    service._completion._sessions_service = service._sessions_service
    session_id = str(uuid4())
    context = _compose_context(session_id)
    current_state_id = str(uuid4())
    request = "Keep the requested behavior and fix the graph"
    evidence = TurnEvidence(
        session_id=session_id,
        session_operation_context=context,
        request_id="pending-advisor-skip-log",
        request=request,
        initial_state_id=current_state_id,
        initial_state=state,
    )
    with structlog.testing.capture_logs() as events:
        outcome = await service._completion._evaluate_terminal_no_tool_advisor_gate(
            state=state,
            turn_evidence=evidence,
            session_id=session_id,
            session_operation_context=context,
            current_state_id=current_state_id,
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
            user_message=request,
            user_id="alice",
            runtime_preflight_cache=service._preflight.new_cache(),
            initial_version=0,
            session_scope="pending-advisor-skip-log",
            plugin_snapshot=None,
            deadline=asyncio.get_running_loop().time() + 60,
        )
    skips = [event for event in events if event["event"] == "composer_interpretation.surface_skipped_runtime_findings"]
    return session_id, current_state_id, outcome, surface, skips, events


@pytest.mark.asyncio
async def test_advisor_terminal_logs_when_runtime_findings_pre_empt_surfacing() -> None:
    """Graph repair takes precedence over new cards; that skip leaves one code-only line."""
    credential = "sk-" + "a" * 24  # built at run time; no literal for the secret scanner
    structural_message = f"tidy_columns requires str while attach_sla emits Any ({credential})"
    findings = ValidationResult(
        is_valid=False,
        checks=[],
        errors=[
            ValidationError(
                component_id="tidy_columns",
                component_type="transform",
                message=structural_message,
                suggestion=None,
                error_code="graph_structure",
            )
        ],
        readiness=ValidationReadiness(authoring_valid=False, execution_ready=False, completion_ready=False, blockers=[]),
    )

    _, _, clean_outcome, clean_surface, clean_skips, _ = await _advisor_terminal_with_findings(None)
    assert clean_outcome.action == "return"
    assert clean_surface.await_count == 1
    assert clean_skips == []

    session_id, current_state_id, outcome, surface, skips, events = await _advisor_terminal_with_findings(findings)
    assert outcome.action == "return"
    surface.assert_not_awaited()
    assert skips == [
        {
            "event": "composer_interpretation.surface_skipped_runtime_findings",
            "log_level": "warning",
            "session_id": session_id,
            "composition_state_id": current_state_id,
            "path": "advisor_terminal",
            "error_count": 1,
            "blocker_count": 0,
            "error_codes": ["graph_structure"],
            "blocker_codes": [],
            # complaint_triage_state(pending=True): vague_term, prompt template, model choice
            "unsurfaced_site_count": 3,
        }
    ]
    rendered = repr(events)
    assert credential not in rendered
    assert "tidy_columns requires" not in rendered
