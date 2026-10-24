"""Closed historical END candidate DATA; no publication or current clearance."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ContinuationActionSource, ContinuationValue, PrivateChunkRef
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk, verify_private_chunk


class ContinuationControlHandoff(ContinuationValue):
    predecessor_sequence: int = Field(gt=0)
    predecessor_generation: int = Field(gt=0)
    successor_sequence: int = Field(gt=0)
    successor_generation: int = Field(gt=0)

    @model_validator(mode="after")
    def require_one_actual_successor(self) -> Self:
        if (self.successor_sequence, self.successor_generation) != (
            self.predecessor_sequence + 1,
            self.predecessor_generation + 1,
        ):
            raise ValueError("control handoff changed its exact queued successor")
        return self


class EndContinuationCandidate(ContinuationValue):
    schema_version: Literal["composer.end.v1"] = "composer.end.v1"
    kind: Literal["reply", "recovery", "control"]
    response: PrivateChunkRef
    primary_effect_key: str = Field(pattern=r"^provider:0:(0|[1-9][0-9]*)$")
    primary_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_text: str
    state: PrivateChunkRef
    evidence: PrivateChunkRef
    iteration: PrivateChunkRef
    superseded_reply_index: int | None = Field(default=None, ge=0)
    runtime_preflight_json: str | None = None
    preparation_feedback: tuple[str, ...] = ()
    allow_repair_continue: bool = True
    publication_binding: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    publication_source: ContinuationActionSource | None = None
    publication_record_json: str | None = None
    semantic_source: ContinuationActionSource | None = None
    semantic_decision_json: str | None = None
    control_handoff: ContinuationControlHandoff | None = None

    @model_validator(mode="after")
    def require_complete_actual_sources(self) -> Self:
        expected = (
            (self.response, "composer.response.v1"),
            (self.state, "composer.state.v1"),
            (self.evidence, "composer.evidence.v1"),
            (self.iteration, "composer.iteration.v1"),
        )
        if any(reference.codec != codec for reference, codec in expected):
            raise ValueError("END candidate lost a complete actual source variant")
        if (self.publication_source is None) != (self.publication_record_json is None):
            raise ValueError("END publication lost its actual pass source/record pair")
        if (self.semantic_source is None) != (self.semantic_decision_json is None):
            raise ValueError("END semantic assessment lost its actual source/decision pair")
        if self.control_handoff is not None and self.kind != "control":
            raise ValueError("only the scheduling control can retain a handoff receipt")
        return self


def encode_end_candidate(value: EndContinuationCandidate) -> PrivateChunk:
    if type(value) is not EndContinuationCandidate:
        raise TypeError("END candidate encoding requires its complete owned value")
    chunk = encode_private_chunk(codec="composer.end.v1", canonical=canonical_json(value.model_dump(mode="json")))
    # model_copy/model_construct must not bypass the closed reference/pair guards.
    decode_end_candidate(chunk)
    return chunk


def decode_end_candidate(chunk: PrivateChunk) -> EndContinuationCandidate:
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.end.v1":
        raise AuditIntegrityError("END frame selected another private codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    try:
        result = EndContinuationCandidate.model_validate_json(chunk.canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("END candidate violates its complete closed shape") from error
    if canonical_json(result.model_dump(mode="json")) != chunk.canonical:
        raise AuditIntegrityError("END candidate lost or changed fields on decode")
    return result


def continuation_candidate_id(candidate: EndContinuationCandidate) -> str:
    if type(candidate) is not EndContinuationCandidate:
        raise TypeError("candidate identity requires its actual retained value")
    return "candidate:" + stable_hash({"text": candidate.candidate_text})
