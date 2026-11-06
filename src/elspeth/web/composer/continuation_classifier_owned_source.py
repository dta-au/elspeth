"""Original classifier refusal observations, retained DATA rather than grants."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation import ContinuationValue
from elspeth.web.composer.continuation_classifier_handoff import decode_classifier_source_stop_proof
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy


class ClassifierOwnedSourceObservation(ContinuationValue):
    schema_version: Literal["composer.classifier-owned-observation.v1"] = "composer.classifier-owned-observation.v1"
    kind: Literal["source_proof_refusal", "reply_protocol_echo", "reply_deadline_exhausted"]
    source_checkpoint_id: str = Field(min_length=1)
    source_cursor: int = Field(ge=0)
    state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    physical_operation_id: str = Field(min_length=1)
    physical_operation_epoch: int = Field(gt=0)
    proof_json: str | None = None
    proof_origin: Literal["preview", "source_assessment"] | None = None
    terminal_tool_call_id: str | None = None
    assessment_trigger: Literal["staged_review", "composition_limit"] | None = None
    composition_limit: int | None = Field(default=None, gt=0)
    deadline: float | None = Field(default=None, allow_inf_nan=False)
    observed_monotonic: float | None = Field(default=None, allow_inf_nan=False)
    clock_started_monotonic: float | None = Field(default=None, allow_inf_nan=False)
    clock_policy_json: str | None = None

    @model_validator(mode="after")
    def require_original_kind_fields(self) -> Self:
        if self.kind == "source_proof_refusal":
            if self.proof_json is None or self.proof_origin is None or self.deadline is not None or self.observed_monotonic is not None:
                raise ValueError("source refusal requires its exact original proof observation")
            decode_classifier_source_stop_proof(self.proof_json)
            if (self.proof_origin == "preview") != (self.terminal_tool_call_id is not None):
                raise ValueError("preview source refusal requires its original terminal tool identity")
            if self.terminal_tool_call_id == "":
                raise ValueError("preview source refusal lost its original terminal tool identity")
            if (self.proof_origin == "source_assessment") != (self.assessment_trigger is not None):
                raise ValueError("source assessment lost its original terminal trigger")
            if (self.assessment_trigger == "composition_limit") != (self.composition_limit is not None):
                raise ValueError("source assessment lost its original composition limit")
        elif self.proof_json is not None or self.proof_origin is not None or self.terminal_tool_call_id is not None:
            raise ValueError("reply refusal cannot invent source-proof observations")
        if self.kind != "source_proof_refusal" and (self.assessment_trigger is not None or self.composition_limit is not None):
            raise ValueError("reply refusal cannot invent source assessment triggers")
        if self.kind == "reply_deadline_exhausted":
            if (
                self.deadline is None
                or self.observed_monotonic is None
                or self.observed_monotonic < self.deadline
                or self.clock_started_monotonic is None
                or self.clock_policy_json is None
                or self.observed_monotonic < self.clock_started_monotonic
            ):
                raise ValueError("reply time exhaustion requires its actual original clock boundary")
            policy = ContinuationActionPolicy.model_validate_json(self.clock_policy_json, strict=True)
            if (
                canonical_json(policy.model_dump(mode="json")) != self.clock_policy_json
                or self.deadline > self.clock_started_monotonic + policy.total
            ):
                raise ValueError("reply time exhaustion changed its original action allowance")
        elif (
            self.deadline is not None
            or self.observed_monotonic is not None
            or self.clock_started_monotonic is not None
            or self.clock_policy_json is not None
        ):
            raise ValueError("only reply time exhaustion carries an elapsed clock observation")
        return self


def encode_classifier_owned_source(value: ClassifierOwnedSourceObservation) -> str:
    if type(value) is not ClassifierOwnedSourceObservation:
        raise TypeError("classifier refusal requires its original nominal observation")
    encoded = canonical_json(value.model_dump(mode="json"))
    if len(encoded.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("classifier refusal observation exceeds its complete document bound")
    if decode_classifier_owned_source(encoded) != value:
        raise AuditIntegrityError("classifier refusal observation changed in its cold encoding")
    return encoded


def decode_classifier_owned_source(encoded: str) -> ClassifierOwnedSourceObservation:
    if type(encoded) is not str or len(encoded.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("classifier refusal lost its bounded original observation")
    value, error = parse_json_strict(encoded)
    if error is not None or canonical_json(value) != encoded:
        raise AuditIntegrityError("classifier refusal changed its canonical observation")
    try:
        restored = ClassifierOwnedSourceObservation.model_validate_json(encoded, strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("classifier refusal lost its closed original observation") from error
    if canonical_json(restored.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("classifier refusal normalized its original observation")
    return restored


class ClassifyReplyRefusalReady(ContinuationValue):
    """Original unpaid reply tail inputs; this DATA grants no execution."""

    schema_version: Literal["composer.classifier-reply-refusal.v1"] = "composer.classifier-reply-refusal.v1"
    source_checkpoint_id: str = Field(min_length=1)
    source_cursor: int = Field(ge=0)
    observation_json: str
    runtime_preflight_json: str | None
    composition_turns_used: int = Field(ge=0)
    discovery_turns_used: int = Field(ge=0)

    @model_validator(mode="after")
    def require_original_unpaid_reply(self) -> Self:
        from elspeth.web.composer.continuation_classifier_handoff import decode_classifier_runtime_preflight

        observation = decode_classifier_owned_source(self.observation_json)
        if observation.kind != "reply_deadline_exhausted" or (
            observation.source_checkpoint_id,
            observation.source_cursor,
        ) != (self.source_checkpoint_id, self.source_cursor):
            raise ValueError("reply refusal ready DATA substituted its original elapsed source")
        decode_classifier_runtime_preflight(self.runtime_preflight_json)
        return self


def encode_classify_reply_refusal_ready(value: ClassifyReplyRefusalReady) -> str:
    if type(value) is not ClassifyReplyRefusalReady:
        raise TypeError("reply refusal requires its original nominal ready DATA")
    encoded = canonical_json(value.model_dump(mode="json"))
    if decode_classify_reply_refusal_ready(encoded) != value:
        raise AuditIntegrityError("reply refusal changed its complete ready DATA")
    return encoded


def decode_classify_reply_refusal_ready(encoded: str) -> ClassifyReplyRefusalReady:
    if type(encoded) is not str or len(encoded.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("reply refusal lost its bounded original ready DATA")
    value, error = parse_json_strict(encoded)
    if error is not None or canonical_json(value) != encoded:
        raise AuditIntegrityError("reply refusal changed its canonical ready DATA")
    try:
        restored = ClassifyReplyRefusalReady.model_validate_json(encoded, strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("reply refusal lost its closed original ready DATA") from error
    if canonical_json(restored.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("reply refusal normalized its original ready DATA")
    return restored


def original_protocol_history_has_tool_calls(messages_json: tuple[str, ...], current_indexes: tuple[int, ...]) -> bool:
    """Project only original active message slots; this grants no admission."""
    if (
        type(messages_json) is not tuple
        or type(current_indexes) is not tuple
        or any(type(index) is not int or not 0 <= index < len(messages_json) for index in current_indexes)
    ):
        raise AuditIntegrityError("protocol history lost its exact original message selectors")
    has_protocol = False
    for index in current_indexes:
        text = messages_json[index]
        if type(text) is not str:
            raise AuditIntegrityError("protocol history lost its original message bytes")
        row, error = parse_json_strict(text)
        if error is not None or type(row) is not dict or type(row.get("role")) is not str:
            raise AuditIntegrityError("protocol refusal lost its original message history")
        has_protocol = has_protocol or row["role"] == "tool" or "tool_calls" in row
    return has_protocol
