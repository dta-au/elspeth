"""Closed post-PRIMARY classifier DATA awaiting the original END decision.

The value records a completed provider source and original branch residue. It
does not admit an SDK call, certify a physical effect, or synthesize an END
candidate; those are separate SQL and driver owners.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation import ContinuationValue, PrivateChunkRef
from elspeth.web.composer.continuation_codec import owned_json
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.tools.generation import MalformedSourceProofClaim, ProofUnavailable
from elspeth.web.execution.schemas import ValidationResult

_RUNTIME = TypeAdapter(ValidationResult)
_MALFORMED_REASONS = frozenset(
    {
        "The source blob claim must be an exact canonical UUID string.",
        "The source blob claim must be a canonical UUID string.",
        "The source blob claim must use canonical UUID spelling.",
    }
)


def encode_classifier_source_stop_proof(proof: ProofUnavailable | MalformedSourceProofClaim) -> str:
    """Retain the original closed source-refusal facts, never raw blob claims."""
    if type(proof) is ProofUnavailable:
        value = {"kind": "unavailable", "diagnostics": deep_thaw(proof.diagnostics)}
    elif type(proof) is MalformedSourceProofClaim and proof.reason in _MALFORMED_REASONS:
        value = {"kind": "malformed", "source_name": proof.source_name, "reason": proof.reason}
    else:
        raise TypeError("classifier source stop requires its original bounded proof")
    encoded = canonical_json(value)
    decode_classifier_source_stop_proof(encoded)
    return encoded


def decode_classifier_source_stop_proof(encoded: str) -> ProofUnavailable | MalformedSourceProofClaim:
    if type(encoded) is not str or len(encoded.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("classifier source stop lost its bounded canonical proof")
    value, error = parse_json_strict(encoded)
    if error is not None or canonical_json(value) != encoded or type(value) is not dict:
        raise AuditIntegrityError("classifier source stop lost its canonical proof")
    if value.get("kind") == "malformed":
        if (
            set(value) != {"kind", "source_name", "reason"}
            or type(value["source_name"]) is not str
            or not value["source_name"]
            or type(value["reason"]) is not str
            or value["reason"] not in _MALFORMED_REASONS
        ):
            raise AuditIntegrityError("classifier source stop changed its original malformed claim")
        return MalformedSourceProofClaim(value["source_name"], value["reason"])
    if value.get("kind") != "unavailable" or set(value) != {"kind", "diagnostics"}:
        raise AuditIntegrityError("classifier source stop changed its original unavailable proof")
    diagnostics = value["diagnostics"]
    if type(diagnostics) is not list or any(
        type(item) is not dict
        or set(item) != {"code", "severity", "message", "suggested_repair", "evidence_locator"}
        or type(item["code"]) is not str
        or not item["code"]
        or item["severity"] not in ("blocking", "info")
        or type(item["message"]) is not str
        or type(item["suggested_repair"]) not in (str, type(None))
        or type(item["evidence_locator"]) is not dict
        for item in diagnostics
    ):
        raise AuditIntegrityError("classifier source stop lost its original diagnostic cohort")
    try:
        restored = ProofUnavailable(tuple(diagnostics))
    except (AuditIntegrityError, KeyError, TypeError, ValueError) as error:
        raise AuditIntegrityError("classifier source stop lost its original unavailable proof") from error
    if canonical_json({"kind": "unavailable", "diagnostics": deep_thaw(restored.diagnostics)}) != encoded:
        raise AuditIntegrityError("classifier source stop normalized its original diagnostic cohort")
    return restored


def encode_classifier_runtime_preflight(value: ValidationResult | None) -> str | None:
    """Preserve the exact owned preflight value used by the original branch."""
    if value is None:
        return None
    if type(value) is not ValidationResult:
        raise TypeError("classifier preflight requires its original owned result")
    encoded = canonical_json(owned_json(deep_thaw(_RUNTIME.dump_python(value, mode="json", warnings="error", fallback=owned_json))))
    decode_classifier_runtime_preflight(encoded)
    return encoded


def decode_classifier_runtime_preflight(encoded: str | None) -> ValidationResult | None:
    if encoded is None:
        return None
    if type(encoded) is not str:
        raise AuditIntegrityError("classifier preflight lost its canonical owned encoding")
    value, error = parse_json_strict(encoded)
    if error is not None or canonical_json(value) != encoded:
        raise AuditIntegrityError("classifier preflight lost its canonical owned encoding")
    try:
        restored = _RUNTIME.validate_json(encoded, strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("classifier preflight lost its complete owned result") from error
    if canonical_json(owned_json(deep_thaw(_RUNTIME.dump_python(restored, mode="json", warnings="error", fallback=owned_json)))) != encoded:
        raise AuditIntegrityError("classifier preflight normalized its original owned result")
    return restored


class ClassifyPostPrimary(ContinuationValue):
    """Original no-tool reply after an actual committed classifier PRIMARY."""

    schema_version: Literal["composer.classify-post-primary.v2"] = "composer.classify-post-primary.v2"
    branch: Literal["staged_review", "composition_last_chance"]
    source_checkpoint_id: str = Field(min_length=1)
    source_cursor: int = Field(ge=0)
    composition_turns_used: int = Field(ge=0)
    discovery_turns_used: int = Field(ge=0)
    primary_effect_key: str = Field(pattern=r"^provider:0:(0|[1-9][0-9]*)$")
    primary_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response: PrivateChunkRef
    runtime_preflight_json: str | None
    composition_turns_delta: int = Field(ge=0, le=1)
    discovery_turns_delta: int = Field(ge=0, le=1)

    @model_validator(mode="after")
    def require_original_branch(self) -> Self:
        if self.response.codec != "composer.response.v1":
            raise ValueError("classifier handoff lost its original PRIMARY response codec")
        if self.branch == "composition_last_chance" and (self.composition_turns_delta, self.discovery_turns_delta) != (1, 0):
            raise ValueError("last-chance classifier changed its original composition charge")
        decode_classifier_runtime_preflight(self.runtime_preflight_json)
        return self


def encode_classify_post_primary(value: ClassifyPostPrimary) -> str:
    if type(value) is not ClassifyPostPrimary:
        raise TypeError("classifier handoff requires its complete owned value")
    encoded = canonical_json(value.model_dump(mode="json"))
    if len(encoded.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("classifier handoff exceeds its complete private document bound")
    decode_classify_post_primary(encoded)
    return encoded


def decode_classify_post_primary(encoded: str) -> ClassifyPostPrimary:
    if type(encoded) is not str or len(encoded.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("classifier handoff lost its bounded canonical encoding")
    value, error = parse_json_strict(encoded)
    if error is not None or canonical_json(value) != encoded:
        raise AuditIntegrityError("classifier handoff lost its bounded canonical encoding")
    try:
        restored = ClassifyPostPrimary.model_validate_json(encoded, strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("classifier handoff lost its closed original source") from error
    if canonical_json(restored.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("classifier handoff normalized its original source")
    return restored


class ClassifySourceProofStop(ContinuationValue):
    """Original applied-P4 source refusal awaiting its genuine END advisor."""

    schema_version: Literal["composer.classify-source-proof-stop.v2"] = "composer.classify-source-proof-stop.v2"
    source_checkpoint_id: str = Field(min_length=1)
    source_cursor: int = Field(ge=0)
    composition_turns_used: int = Field(ge=0)
    discovery_turns_used: int = Field(ge=0)
    proof_json: str
    composition_turns_delta: int = Field(ge=0, le=1)
    discovery_turns_delta: int = Field(ge=0, le=1)

    @model_validator(mode="after")
    def require_original_source_stop(self) -> Self:
        if self.composition_turns_delta + self.discovery_turns_delta > 1:
            raise ValueError("source stop changed its mutually exclusive original turn charge")
        decode_classifier_source_stop_proof(self.proof_json)
        return self


def encode_classify_source_proof_stop(value: ClassifySourceProofStop) -> str:
    if type(value) is not ClassifySourceProofStop:
        raise TypeError("classifier source stop requires its complete owned value")
    encoded = canonical_json(value.model_dump(mode="json"))
    if len(encoded.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("classifier source stop exceeds its complete private document bound")
    decode_classify_source_proof_stop(encoded)
    return encoded


def decode_classify_source_proof_stop(encoded: str) -> ClassifySourceProofStop:
    if type(encoded) is not str or len(encoded.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("classifier source stop lost its bounded canonical encoding")
    value, error = parse_json_strict(encoded)
    if error is not None or canonical_json(value) != encoded:
        raise AuditIntegrityError("classifier source stop lost its bounded canonical encoding")
    try:
        restored = ClassifySourceProofStop.model_validate_json(encoded, strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("classifier source stop lost its closed original source") from error
    if canonical_json(restored.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("classifier source stop normalized its original source")
    return restored
