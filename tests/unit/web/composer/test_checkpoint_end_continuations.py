"""Saved post-gate caller cuts do not rerun provider or earlier repair work."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage, _AdvisorReviewState
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.composition_completion import _source_proof_unavailable_validation, _TerminalNoToolAdvisorGateOutcome
from elspeth.web.composer.no_tool_response import parse_no_tool_response
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools.generation import MalformedSourceProofClaim
from tests.unit.web.composer._helpers import _make_settings, _mock_catalog


@pytest.fixture
def completion(monkeypatch):
    import litellm

    async def forbidden_provider(**kwargs):
        pytest.fail("A post-gate continuation must not invoke a provider")

    monkeypatch.setattr(litellm, "acompletion", forbidden_provider)
    return ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())._completion


def _state():
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _ordinary_args(state, gate):
    return {
        "advisor_gate": gate,
        "proof_runtime_preflight": None,
        "proof_repair": None,
        "no_tool_response": parse_no_tool_response("Original reply"),
        "state": state,
        "assistant_message": _AdmittedAssistantMessage(content="Original reply"),
        "session_operation_context": None,
        "session_id": None,
        "current_state_id": None,
        "progress": None,
        "recorder": BufferingRecorder(),
        "initial_version": state.version,
        "user_id": None,
        "last_runtime_preflight": None,
        "runtime_preflight_cache": {},
        "session_scope": "sessionless",
        "message": "Original request",
        "mutation_success_seen": False,
        "repair_turns_used": 1,
        "plugin_snapshot": None,
        "deadline": None,
        "completion_gates": None,
        "persisted_assistant_message_id": None,
        "persisted_assistant_content": None,
        "persisted_tool_call_turn": False,
    }


@pytest.mark.asyncio
async def test_ordinary_post_gate_preserves_original_repair_accounting(completion):
    review = _AdvisorReviewState(completed_passes=2, previous_findings=("original",))
    gate = _TerminalNoToolAdvisorGateOutcome(
        action="continue", advisor_passes_delta=2, advisor_injection_index=7, advisor_review_state=review
    )
    result = await completion._continue_no_tools_after_advisor_gate(**_ordinary_args(_state(), gate))
    assert result.action == "continue"
    assert result.advisor_passes_delta == 2
    assert result.advisor_injection_index == 7
    assert result.advisor_review_state is review


@pytest.mark.asyncio
async def test_ordinary_post_gate_preserves_exhausted_proof_over_review_result(completion):
    state = _state()
    proof = _source_proof_unavailable_validation(state, MalformedSourceProofClaim("source", "Original invalid claim"), None)
    original = ComposerResult(message="Original reply with reviewed blocker", raw_assistant_content="Original reply", state=state)
    gate = _TerminalNoToolAdvisorGateOutcome(action="return", result=original, advisor_passes_delta=1)
    arguments = _ordinary_args(state, gate)
    arguments["proof_runtime_preflight"] = proof
    result = await completion._continue_no_tools_after_advisor_gate(**arguments)
    assert result.result == replace(original, runtime_preflight=proof)
    assert result.advisor_passes_delta == 1


def test_source_stop_post_gate_never_reopens_authoring(completion):
    state = _state()
    runtime = _source_proof_unavailable_validation(state, MalformedSourceProofClaim("source", "Original invalid claim"), None)
    arguments = {
        "runtime": runtime,
        "state": state,
        "assistant_message": _AdmittedAssistantMessage(content="Original reply"),
        "recorder": BufferingRecorder(),
        "repair_turns_used": 1,
        "persisted_assistant_message_id": None,
        "persisted_assistant_content": None,
        "persisted_tool_call_turn": False,
        "completion_gates": None,
    }
    with pytest.raises(AuditIntegrityError, match="reopened primary authoring"):
        completion._continue_source_proof_after_advisor_gate(gate=_TerminalNoToolAdvisorGateOutcome(action="continue"), **arguments)
    result = completion._continue_source_proof_after_advisor_gate(
        gate=_TerminalNoToolAdvisorGateOutcome(action="fall_through", advisor_passes_delta=1, publication_binding="a" * 64), **arguments
    )
    assert result.action == "return"
    assert result.result.runtime_preflight == runtime
    assert result.publication_binding == "a" * 64
    assert result.advisor_passes_delta == 1
