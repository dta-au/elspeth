"""Refusal observation DATA cannot erase its branch/clock/proof boundaries."""

import json

import pytest
from pydantic import ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_classifier_handoff import encode_classifier_source_stop_proof
from elspeth.web.composer.continuation_classifier_owned_source import (
    ClassifierOwnedSourceObservation,
    decode_classifier_owned_source,
    encode_classifier_owned_source,
    original_protocol_history_has_tool_calls,
)
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy
from elspeth.web.composer.tools.generation import MalformedSourceProofClaim


def _fields():
    # These are codec DATA controls; constructing this value issues no actor,
    # original observation, no-SDK receipt, or publication capability.
    return {
        "source_checkpoint_id": "actual-source-key-required-by-SQL",
        "source_cursor": 7,
        "state_sha256": "a" * 64,
        "physical_operation_id": "original-physical-operation",
        "physical_operation_epoch": 3,
    }


def _clock_fields():
    policy = ContinuationActionPolicy(preparation=1.0, guard=None, quota=None, physical=1.0, retention=1.0)
    return {"clock_started_monotonic": 8.0, "clock_policy_json": canonical_json(policy.model_dump(mode="json"))}


@pytest.mark.parametrize("kind", ("source_proof_refusal", "reply_protocol_echo", "reply_deadline_exhausted"))
def test_owned_observation_preserves_complete_kind_specific_data(kind):
    proof = MalformedSourceProofClaim("source", "The source blob claim must be a canonical UUID string.")
    fields = _fields()
    if kind == "source_proof_refusal":
        fields.update(
            proof_json=encode_classifier_source_stop_proof(proof),
            proof_origin="preview",
            terminal_tool_call_id="preview-original",
        )
    elif kind == "reply_deadline_exhausted":
        fields.update(deadline=10.25, observed_monotonic=10.25, **_clock_fields())
    original = ClassifierOwnedSourceObservation(kind=kind, **fields)
    encoded = encode_classifier_owned_source(original)
    assert decode_classifier_owned_source(encoded) == original
    assert canonical_json(json.loads(encoded)) == encoded


@pytest.mark.parametrize("observed", (10.0, True, float("inf"), float("nan")))
def test_deadline_observation_rejects_unexpired_boolean_or_nonfinite_boundary(observed):
    with pytest.raises((ValidationError, ValueError)):
        ClassifierOwnedSourceObservation(
            kind="reply_deadline_exhausted",
            **_fields(),
            **_clock_fields(),
            deadline=10.25,
            observed_monotonic=observed,
        )


def test_reply_refusal_cannot_borrow_proof_or_elapsed_clock_fields():
    original = ClassifierOwnedSourceObservation(kind="reply_protocol_echo", **_fields())
    wire = json.loads(encode_classifier_owned_source(original))
    for extra in ({"proof_origin": "source_assessment"}, {"deadline": 1.5}, {"unknown_grant": True}):
        with pytest.raises(AuditIntegrityError):
            decode_classifier_owned_source(canonical_json({**wire, **extra}))


def test_assessment_preserves_its_original_trigger_and_limit():
    proof = MalformedSourceProofClaim("source", "The source blob claim must use canonical UUID spelling.")
    original = ClassifierOwnedSourceObservation(
        kind="source_proof_refusal",
        **_fields(),
        proof_json=encode_classifier_source_stop_proof(proof),
        proof_origin="source_assessment",
        assessment_trigger="composition_limit",
        composition_limit=6,
    )
    assert decode_classifier_owned_source(encode_classifier_owned_source(original)) == original
    with pytest.raises(AuditIntegrityError):
        encode_classifier_owned_source(original.model_copy(update={"composition_limit": None}))
    with pytest.raises(AuditIntegrityError):
        encode_classifier_owned_source(original.model_copy(update={"terminal_tool_call_id": "foreign-preview"}))


def test_original_physical_epoch_and_canonical_document_are_required():
    original = ClassifierOwnedSourceObservation(kind="reply_protocol_echo", **_fields())
    encoded = encode_classifier_owned_source(original)
    with pytest.raises(AuditIntegrityError):
        decode_classifier_owned_source(" " + encoded)
    with pytest.raises(AuditIntegrityError):
        encode_classifier_owned_source(original.model_copy(update={"physical_operation_epoch": 0}))


def test_protocol_history_selects_actual_current_slots_and_checks_complete_tail():
    shelf = (
        canonical_json({"role": "user", "content": "Explain the saved draft."}),
        canonical_json({"role": "tool", "content": "Original P4 result."}),
        canonical_json({"role": "assistant", "content": "Historical explanation."}),
    )
    assert original_protocol_history_has_tool_calls(shelf, (0, 1))
    assert not original_protocol_history_has_tool_calls(shelf, (0, 2))
    # A matching earlier slot cannot hide a malformed selected tail.
    with pytest.raises(AuditIntegrityError, match="original message history"):
        original_protocol_history_has_tool_calls((*shelf, "[]"), (1, 3))
    with pytest.raises(AuditIntegrityError, match="original message selectors"):
        original_protocol_history_has_tool_calls(shelf, (1, True))
    with pytest.raises(AuditIntegrityError, match="original message selectors"):
        original_protocol_history_has_tool_calls(shelf, (1, len(shelf)))
