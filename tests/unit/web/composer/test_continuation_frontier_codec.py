import json
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.advisor_output import (
    ContinuationAdvisorDecision,
    ContinuationEvidenceRef,
    ContinuationObligationProposal,
    ReadContinuationAction,
)
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_frontier_chunk,
    encode_frontier_chunk,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_progress import (
    ActionExecutionReceipt,
    ContinuationProtocolError,
    apply_bound_continuation_decision,
    canonicalize_continuation_obligations,
    continuation_action_key,
    initial_continuation_frontier,
    record_action_receipt,
    require_bound_continuation_decision,
)


def reviewed_frontier():
    initial = initial_continuation_frontier(
        request="Build and inspect",
        original_message_id="original",
        graph_hash="graph",
    )
    evidence = ContinuationEvidenceRef(
        receipt_id="inspection:1",
        content_hash="a" * 64,
        original_attempt_id="physical-1",
        coverage_binding="coverage:1",
    )
    action = ReadContinuationAction(
        kind="read_tool",
        obligation_ids=("inspect",),
        evidence_ids=("inspection:1",),
        input_hash="input",
        graph_hash="graph",
        policy_hash="policy",
        tool_manifest_hash="tools",
        tool_name="get_plugin_schema",
        arguments_json='{"name":"csv","type":"source"}',
    )
    decision = ContinuationAdvisorDecision(
        schema_version="composer.continuation-decision.v1",
        decision_id="advisor:1",
        decision="CONTINUE",
        scope="subset",
        obligation_ids=("inspect",),
        obligations=(
            ContinuationObligationProposal(
                obligation_id="inspect",
                parent_id="request",
                description="Inspect",
                original_message_id="original",
                start=10,
                end=17,
                status="open",
            ),
        ),
        evidence=(evidence,),
        reason="Inspect the actual contract.",
        remaining_actionable_ids=("request", "inspect"),
        prescribed_action=action,
    )
    binding = {
        "actual_advisor_call_id": "advisor:1",
        "retained_evidence": {"inspection:1": evidence},
        "input_hash": "input",
        "policy_hash": "policy",
        "tool_manifest_hash": "tools",
        "permitted_read_tools": frozenset({"get_plugin_schema"}),
        "complete_required_coverage": True,
    }
    decision = canonicalize_continuation_obligations(decision, initial)
    accepted = apply_bound_continuation_decision(decision, frontier=initial, **binding)
    accepted = replace(accepted, pending_review_bindings=("remaining-review",))
    canonical_action = decision.prescribed_action
    assert canonical_action is not None
    committed = record_action_receipt(
        accepted,
        ActionExecutionReceipt(
            continuation_action_key(canonical_action, retained_evidence={"inspection:1": evidence}),
            "owner:1",
            "committed",
        ),
        actual_owner_receipt_id="owner:1",
    )
    return committed, decision, binding


def test_complete_frontier_cold_roundtrip_keeps_obligations_review_debt_and_receipts():
    original, _decision, _binding = reviewed_frontier()
    chunk = encode_frontier_chunk(original)
    cold = decode_frontier_chunk(PrivateChunk(chunk.reference, chunk.canonical.encode().decode()))
    assert cold == original
    assert cold is not original
    assert cold.obligations[1].start == 10
    assert cold.pending_review_bindings == ("remaining-review",)
    assert cold.evidence[0].original_attempt_id == "physical-1"
    assert encode_frontier_chunk(cold) == chunk


def test_cold_frontier_refuses_repeating_a_committed_action():
    original, decision, binding = reviewed_frontier()
    chunk = encode_frontier_chunk(original)
    cold = decode_frontier_chunk(PrivateChunk(chunk.reference, chunk.canonical.encode().decode()))
    with pytest.raises(ContinuationProtocolError, match="already been prepared"):
        require_bound_continuation_decision(decision, frontier=cold, **binding)


@pytest.mark.parametrize("missing", ["obligations", "evidence", "pending_review_bindings", "action_receipts", "graph_hash"])
def test_missing_frontier_channel_is_not_defaulted_to_empty(missing):
    original, _, _ = reviewed_frontier()
    value = json.loads(encode_frontier_chunk(original).canonical)
    del value[missing]
    chunk = encode_private_chunk(codec="composer.semantic-frontier.v1", canonical=canonical_json(value))
    with pytest.raises(AuditIntegrityError, match="owned codec"):
        decode_frontier_chunk(chunk)


def test_frontier_refuses_changed_bytes_under_a_known_reference():
    original, _, _ = reviewed_frontier()
    chunk = encode_frontier_chunk(original)
    with pytest.raises(AuditIntegrityError, match="binding"):
        decode_frontier_chunk(PrivateChunk(chunk.reference, chunk.canonical.replace("remaining-review", "different-review")))
