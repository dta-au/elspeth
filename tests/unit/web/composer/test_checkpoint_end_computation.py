"""Pure END branch controls; these confer no V4 physical or SQL authority."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage, _AdvisorReviewState
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointVerdict
from elspeth.web.composer.composition_completion import _FINALIZATION_AUTO_SURFACEABLE_KINDS
from elspeth.web.composer.continuation_checkpoint_end_computation import compute_end_gate
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.turn_effects import TurnEvidence


def gate_inputs():
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    evidence = TurnEvidence(
        session_id=None,
        session_operation_context=None,
        request_id="pure-end-branch",
        request="Original request",
        initial_state_id=None,
        initial_state=state,
    )
    review = evidence.review_candidate(candidate="Original candidate", request="Original request", current_state=state)
    return {
        "publication_review": review,
        "passes_delta": 1,
        "review_state": _AdvisorReviewState(completed_passes=1),
        "stalled_repair": False,
        "orphaned_precheck": (),
        "auto_surfaceable_kinds": _FINALIZATION_AUTO_SURFACEABLE_KINDS,
        "state": state,
        "turn_evidence": evidence,
        "assistant_message": _AdmittedAssistantMessage(content="Original candidate"),
        "user_message": "Original request",
        "advisor_checkpoint_passes_used": 0,
        "max_passes": 2,
        "allow_repair_continue": True,
        "dialogue_length": 7,
    }


@pytest.mark.parametrize(
    ("blocking", "publication", "expected"),
    [
        (False, "ADMIT", "fallthrough"),
        (True, "ADMIT", "blocked_or_orphan_terminal"),
        (True, "WITHHOLD", "repair_continue"),
        (False, "WITHHOLD", "repair_continue"),
    ],
)
def test_pure_end_branch_preserves_graph_and_publication_distinction(blocking, publication, expected):
    inputs = gate_inputs()
    before = inputs["state"].to_dict()
    transcript = inputs["turn_evidence"].transcript.snapshot()
    verdict = AdvisorCheckpointVerdict(
        ok=True,
        blocking=blocking,
        findings_text="Original finding",
        publication_verdict=publication,
        publication_binding=inputs["publication_review"].binding,
    )
    result = compute_end_gate(verdict=verdict, **inputs)
    assert result.action == expected
    assert result.verdict is verdict and result.review_state is inputs["review_state"]
    assert inputs["state"].to_dict() == before
    assert inputs["turn_evidence"].transcript.snapshot() == transcript
    if expected == "repair_continue":
        assert result.advisor_injection_index == 7
        assert "Original finding" in result.repair_dialogue_content
        assert ("A prose-only correction is valid" in result.repair_dialogue_content) is (not blocking)
    else:
        assert result.advisor_injection_index is None and result.repair_dialogue_content is None


@pytest.mark.parametrize("change", [{"max_passes": 1}, {"stalled_repair": True}, {"allow_repair_continue": False}])
def test_pure_end_terminal_conditions_do_not_grant_another_repair(change):
    inputs = gate_inputs()
    inputs.update(change)
    verdict = AdvisorCheckpointVerdict(ok=True, blocking=True, findings_text="Original finding", publication_verdict="WITHHOLD")
    assert compute_end_gate(verdict=verdict, **inputs).action == "blocked_or_orphan_terminal"


def test_changed_candidate_cannot_borrow_prior_publication_admission():
    inputs = gate_inputs()
    verdict = AdvisorCheckpointVerdict(
        ok=True, blocking=False, findings_text="", publication_verdict="ADMIT", publication_binding=inputs["publication_review"].binding
    )
    assert compute_end_gate(verdict=verdict, **inputs).action == "fallthrough"
    inputs["assistant_message"] = _AdmittedAssistantMessage(content="Changed candidate")
    changed = compute_end_gate(verdict=verdict, **inputs)
    assert not changed.publication_admitted and changed.action == "repair_continue"
    inputs["dialogue_length"] = None
    with pytest.raises(AuditIntegrityError, match="Terminal-only review"):
        compute_end_gate(verdict=verdict, **inputs)
    assert compute_end_gate(verdict=replace(verdict, retryable=False), **inputs).action == "blocked_or_orphan_terminal"
