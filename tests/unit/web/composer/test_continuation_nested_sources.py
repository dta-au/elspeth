"""Closed historical source codecs; no review/current-authority acceptance."""

import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import ContinuationActionSource, PrivateChunkRef
from elspeth.web.composer.continuation_advisor import (
    ContinuationAdvisorInvocation,
    ContinuationAdvisorPremise,
    decode_continuation_advisor_invocation,
    encode_continuation_advisor_invocation,
)
from elspeth.web.composer.continuation_codec import encode_continuation_response, encode_frontier_chunk, encode_private_chunk
from elspeth.web.composer.continuation_end_values import (
    ContinuationControlHandoff,
    EndContinuationCandidate,
    decode_end_candidate,
    encode_end_candidate,
)
from elspeth.web.composer.tools.sessions import RequestAdvisorHintArgumentsModel
from elspeth.web.sessions.continuation_selection import nested_private_chunks
from tests.unit.web.composer.test_advisor_audit import _pass_record
from tests.unit.web.composer.test_continuation_batch_data import actual_cut
from tests.unit.web.composer.test_continuation_frontier_codec import reviewed_frontier
from tests.unit.web.composer.test_continuation_message_family_builders import completion_with_native


def manual_arguments():
    return RequestAdvisorHintArgumentsModel(
        trigger="cannot_complete",
        problem_summary="Review the actual remaining issue, café.",
        recent_errors=["first", "first"],
        attempted_actions=["inspect current options"],
        schema_excerpt="Exact schema excerpt",
    )


def nested_source_values(*, state, evidence, transcript, iteration):
    completion = completion_with_native(calls=())
    primary = encode_continuation_response(completion)
    publication = encode_continuation_response(replace(completion, message=replace(completion.message, content="Actual publication input")))
    semantic = encode_continuation_response(replace(completion, message=replace(completion.message, content="Actual semantic input")))
    frontier, decision, _bindings = reviewed_frontier()
    frontier_chunk = encode_frontier_chunk(frontier)
    record = _pass_record()
    candidate = EndContinuationCandidate(
        kind="control",
        response=primary.reference,
        primary_effect_key="provider:0:3",
        primary_input_sha256="a" * 64,
        candidate_text="Exact provider text: café λ. " + "full-tail " * 50,
        state=state,
        evidence=evidence,
        iteration=iteration,
        superseded_reply_index=2,
        runtime_preflight_json=canonical_json(actual_cut()[0].last_runtime_preflight.model_dump(mode="json")),
        preparation_feedback=("first", "first", "last"),
        allow_repair_continue=False,
        publication_binding=record.publication_binding,
        publication_source=ContinuationActionSource(effect_key="provider:1:4", input_sha256="b" * 64, response=publication.reference),
        publication_record_json=canonical_json(record.to_dict()),
        semantic_source=ContinuationActionSource(effect_key="provider:1:5", input_sha256="c" * 64, response=semantic.reference),
        semantic_decision_json=canonical_json(decision.model_dump(mode="json")),
        control_handoff=ContinuationControlHandoff(
            predecessor_sequence=2, predecessor_generation=3, successor_sequence=3, successor_generation=4
        ),
    )
    advisor = ContinuationAdvisorInvocation(
        kind="semantic",
        state=state,
        evidence=evidence,
        transcript=transcript,
        iteration=iteration,
        coverage_binding="actual-retained-coverage",
        manual_arguments_json=None,
        manual_tool_call_id=None,
        candidate=candidate.candidate_text,
        premise=ContinuationAdvisorPremise(
            input_hash="input", policy_hash="policy", tool_manifest_hash="tools", frontier=frontier_chunk.reference
        ),
    )
    return candidate, advisor, (primary, publication, semantic, frontier_chunk)


def reference(codec, number):
    # Codec tests validate retained locators; these do not claim SQL existence.
    return PrivateChunkRef(codec=codec, digest=f"{number:064x}", byte_length=number)


def source_values():
    return nested_source_values(
        state=reference("composer.state.v1", 1),
        evidence=reference("composer.evidence.v2", 2),
        transcript=reference("composer.transcript.v1", 3),
        iteration=reference("composer.iteration.v1", 4),
    )


def test_complete_end_and_semantic_source_roundtrips_preserve_every_locator_and_byte():
    candidate, advisor, leaves = source_values()
    end_chunk = encode_end_candidate(candidate)
    review = encode_continuation_advisor_invocation(advisor)
    assert decode_end_candidate(end_chunk) == candidate
    assert encode_end_candidate(decode_end_candidate(end_chunk)) == end_chunk
    assert decode_continuation_advisor_invocation(review) == advisor
    assert encode_continuation_advisor_invocation(decode_continuation_advisor_invocation(review)) == review
    assert nested_private_chunks(end_chunk) == (
        leaves[0].reference,
        candidate.state,
        candidate.evidence,
        candidate.iteration,
        leaves[1].reference,
        leaves[2].reference,
    )
    assert nested_private_chunks(review) == (advisor.state, advisor.evidence, advisor.transcript, advisor.iteration, leaves[3].reference)
    # The retained pass is WITHHOLD; mere codec admission cannot turn it into ADMIT.
    assert json.loads(candidate.publication_record_json)["publication_verdict"] == "WITHHOLD"


@pytest.mark.parametrize("kind", ("manual", "early", "publication"))
def test_other_actual_advisor_source_variants_preserve_their_closed_fields(kind):
    _candidate, semantic, _leaves = source_values()
    value = ContinuationAdvisorInvocation(
        kind=kind,
        state=semantic.state,
        evidence=semantic.evidence,
        transcript=semantic.transcript,
        iteration=semantic.iteration,
        coverage_binding=semantic.coverage_binding,
        manual_arguments_json=canonical_json(manual_arguments().model_dump(mode="json")) if kind == "manual" else None,
        manual_tool_call_id="actual-manual-call" if kind == "manual" else None,
        candidate="Exact publication text" if kind == "publication" else None,
        premise=None,
    )
    chunk = encode_continuation_advisor_invocation(value)
    assert decode_continuation_advisor_invocation(chunk) == value
    assert nested_private_chunks(chunk) == (value.state, value.evidence, value.transcript, value.iteration)


@pytest.mark.parametrize("family", ("end", "advisor"))
@pytest.mark.parametrize("change", ("unknown_field", "recursive_codec", "omitted_field", "wrong_scalar"))
def test_closed_source_refuses_unimplemented_or_recursive_shape_after_positive(family, change):
    candidate, advisor, _leaves = source_values()
    chunk = encode_end_candidate(candidate) if family == "end" else encode_continuation_advisor_invocation(advisor)
    decode = decode_end_candidate if family == "end" else decode_continuation_advisor_invocation
    assert decode(chunk) == (candidate if family == "end" else advisor)
    payload = json.loads(chunk.canonical)
    if change == "unknown_field":
        payload["future_nested_source"] = payload["state"]
    elif change == "recursive_codec":
        # No SHA fixed point or authority bypass: the closed edge vocabulary
        # itself must refuse an attempted End/Advisor recursion.
        payload["state"] = chunk.reference.model_dump(mode="json")
    elif change == "omitted_field":
        payload.pop("candidate_text" if family == "end" else "coverage_binding")
    else:
        payload["candidate_text" if family == "end" else "coverage_binding"] = 17
    changed = encode_private_chunk(codec=chunk.reference.codec, canonical=canonical_json(payload))
    with pytest.raises(AuditIntegrityError):
        decode(changed)


def test_candidate_pair_and_successor_guards_cannot_be_bypassed_by_copy():
    candidate, _advisor, _leaves = source_values()
    assert decode_end_candidate(encode_end_candidate(candidate)) == candidate
    with pytest.raises(AuditIntegrityError):
        encode_end_candidate(candidate.model_copy(update={"semantic_decision_json": None}))
    with pytest.raises(ValidationError):
        ContinuationControlHandoff(predecessor_sequence=1, predecessor_generation=1, successor_sequence=3, successor_generation=2)
