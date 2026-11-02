"""The classifier handoff retains exact paid source DATA without END permission."""

import json
from typing import Literal

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_classifier_handoff import (
    ClassifyPostPrimary,
    ClassifySourceProofStop,
    decode_classifier_runtime_preflight,
    decode_classifier_source_stop_proof,
    decode_classify_post_primary,
    decode_classify_source_proof_stop,
    encode_classifier_runtime_preflight,
    encode_classifier_source_stop_proof,
    encode_classify_post_primary,
    encode_classify_source_proof_stop,
)
from elspeth.web.composer.tools.generation import MalformedSourceProofClaim, ProofUnavailable
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult


def _handoff(*, branch: Literal["staged_review", "composition_last_chance"] = "staged_review") -> ClassifyPostPrimary:
    return ClassifyPostPrimary(
        branch=branch,
        source_checkpoint_id="original-applied-p4",
        source_cursor=17,
        composition_turns_used=3,
        discovery_turns_used=5,
        primary_effect_key="provider:0:2",
        primary_input_sha256="a" * 64,
        response=PrivateChunkRef(codec="composer.response.v1", digest="b" * 64, byte_length=113),
        runtime_preflight_json=None,
        composition_turns_delta=1,
        discovery_turns_delta=0,
    )


def test_classifier_handoff_preserves_original_branch_source_and_response() -> None:
    value = _handoff()
    assert decode_classify_post_primary(encode_classify_post_primary(value)) == value
    last_chance = _handoff(branch="composition_last_chance")
    assert decode_classify_post_primary(encode_classify_post_primary(last_chance)) == last_chance


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("primary_effect_key", "provider:0:3"),
        ("source_checkpoint_id", "foreign-applied-p4"),
        ("primary_input_sha256", "c" * 64),
        ("response", {"codec": "composer.response.v1", "digest": "d" * 64, "byte_length": 113}),
    ),
)
def test_classifier_handoff_tampering_changes_exact_original_source(field: str, replacement: object) -> None:
    original = encode_classify_post_primary(_handoff())
    changed = json.loads(original)
    changed[field] = replacement
    substituted = decode_classify_post_primary(canonical_json(changed))
    assert substituted != _handoff()
    assert encode_classify_post_primary(substituted) != original


def test_classifier_handoff_refuses_foreign_response_codec_and_last_chance_charge() -> None:
    wire = json.loads(encode_classify_post_primary(_handoff(branch="composition_last_chance")))
    wire["response"]["codec"] = "composer.state.v1"
    with pytest.raises((AuditIntegrityError, ValueError)):
        decode_classify_post_primary(canonical_json(wire))
    wire["response"]["codec"] = "composer.response.v1"
    wire["composition_turns_delta"] = 0
    with pytest.raises((AuditIntegrityError, ValueError)):
        decode_classify_post_primary(canonical_json(wire))


def test_classifier_handoff_preflight_roundtrip_and_normalization_refusal() -> None:
    original = ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        warnings=[],
        semantic_contracts=[],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
    )
    encoded = encode_classifier_runtime_preflight(original)
    assert encoded is not None
    assert decode_classifier_runtime_preflight(encoded) == original
    with pytest.raises(AuditIntegrityError):
        decode_classifier_runtime_preflight(encoded + " ")


def _unavailable_source_proof() -> ProofUnavailable:
    return ProofUnavailable(
        (
            {
                "code": "source_blob_unavailable",
                "severity": "blocking",
                "message": "Claimed source content was unavailable.",
                "suggested_repair": "Restore the verified source.",
                "evidence_locator": {"source": "blob", "blob_id": "b" * 36},
            },
        )
    )


@pytest.mark.parametrize(
    ("proof", "composition_delta", "discovery_delta"),
    (
        (_unavailable_source_proof(), 1, 0),
        (MalformedSourceProofClaim("source_a", "The source blob claim must use canonical UUID spelling."), 0, 1),
    ),
)
def test_source_proof_stop_retains_original_nominal_refusal_and_exclusive_original_delta(
    proof: ProofUnavailable | MalformedSourceProofClaim, composition_delta: int, discovery_delta: int
) -> None:
    value = ClassifySourceProofStop(
        source_checkpoint_id="original-applied-p4",
        source_cursor=17,
        composition_turns_used=3,
        discovery_turns_used=5,
        proof_json=encode_classifier_source_stop_proof(proof),
        composition_turns_delta=composition_delta,
        discovery_turns_delta=discovery_delta,
    )
    restored = decode_classify_source_proof_stop(encode_classify_source_proof_stop(value))
    assert restored == value
    original_proof = decode_classifier_source_stop_proof(restored.proof_json)
    assert type(original_proof) is type(proof)
    if type(original_proof) is ProofUnavailable and type(proof) is ProofUnavailable:
        assert original_proof.diagnostics == proof.diagnostics
    else:
        assert type(original_proof) is MalformedSourceProofClaim
        assert type(proof) is MalformedSourceProofClaim
        assert original_proof.source_name == proof.source_name
        assert original_proof.reason == proof.reason


def test_source_proof_stop_rejects_substituted_or_unavailable_proof_without_original_blocker() -> None:
    malformed = encode_classifier_source_stop_proof(
        MalformedSourceProofClaim("source_a", "The source blob claim must be a canonical UUID string.")
    )
    wire = json.loads(malformed)
    wire["reason"] = "Arbitrary provider text"
    with pytest.raises(AuditIntegrityError, match="original malformed claim"):
        decode_classifier_source_stop_proof(canonical_json(wire))
    unavailable = json.loads(encode_classifier_source_stop_proof(_unavailable_source_proof()))
    unavailable["diagnostics"][0]["code"] = "source_inspection_failed"
    with pytest.raises(AuditIntegrityError, match="original unavailable proof"):
        decode_classifier_source_stop_proof(canonical_json(unavailable))
    unavailable["diagnostics"][0]["code"] = "source_blob_unavailable"
    del unavailable["diagnostics"][0]["message"]
    with pytest.raises(AuditIntegrityError, match="original diagnostic cohort"):
        decode_classifier_source_stop_proof(canonical_json(unavailable))
    with pytest.raises(ValueError, match="mutually exclusive"):
        ClassifySourceProofStop(
            source_checkpoint_id="original-applied-p4",
            source_cursor=17,
            composition_turns_used=3,
            discovery_turns_used=5,
            proof_json=encode_classifier_source_stop_proof(_unavailable_source_proof()),
            composition_turns_delta=1,
            discovery_turns_delta=1,
        )


def test_classifier_handoff_refuses_legacy_bodies_without_original_used_counts() -> None:
    post = json.loads(encode_classify_post_primary(_handoff()))
    del post["composition_turns_used"]
    post["schema_version"] = "composer.classify-post-primary.v1"
    with pytest.raises(AuditIntegrityError, match="closed original source"):
        decode_classify_post_primary(canonical_json(post))
    stop = ClassifySourceProofStop(
        source_checkpoint_id="original-applied-p4",
        source_cursor=17,
        composition_turns_used=3,
        discovery_turns_used=5,
        proof_json=encode_classifier_source_stop_proof(_unavailable_source_proof()),
        composition_turns_delta=1,
        discovery_turns_delta=0,
    )
    old_stop = json.loads(encode_classify_source_proof_stop(stop))
    del old_stop["discovery_turns_used"]
    old_stop["schema_version"] = "composer.classify-source-proof-stop.v1"
    with pytest.raises(AuditIntegrityError, match="closed original source"):
        decode_classify_source_proof_stop(canonical_json(old_stop))
