"""Closed versioned pipeline settlement evidence, without mutation authority."""

from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from elspeth.contracts.hashing import stable_hash

Hash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _ClosedEvidence(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class ComposerOperationBinding(_ClosedEvidence):
    operation_id: str
    session_operation_id: str
    session_operation_epoch: int = Field(gt=0)
    attempt: int = Field(gt=0)

    @field_validator("operation_id", "session_operation_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("noncanonical owned UUID")
        return value


class ReviewSemanticMaterial(_ClosedEvidence):
    ordinal: int = Field(ge=0)
    candidate_state_id: str
    affected_node_id: str = Field(min_length=1)
    kind: Literal["vague_term", "llm_prompt_template", "invented_source", "source_data_contract", "pipeline_decision", "llm_model_choice"]
    user_term: str = Field(min_length=1)
    llm_draft: str = Field(min_length=1)
    surface_origin: Literal["composer_llm", "state_revert", "yaml_import", "e2e_seed"]
    model_identifier: str | None
    model_version: str | None
    provider: str | None
    composer_skill_hash: Hash | None

    @field_validator("candidate_state_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("noncanonical owned UUID")
        return value

    @field_validator("user_term")
    @classmethod
    def trimmed_term(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("semantic term must be canonical")
        return value

    def content_hash(self) -> str:
        return stable_hash({"schema": "composer.pipeline-review-semantic.v1", "material": self.model_dump(mode="json")})

    @model_validator(mode="after")
    def coherent_provenance(self) -> Self:
        provenance = (self.model_identifier, self.model_version, self.provider, self.composer_skill_hash)
        if self.surface_origin == "composer_llm":
            if any(value is None or not value.strip() for value in provenance):
                raise ValueError("composer review provenance incomplete")
        elif any(value is not None for value in provenance):
            raise ValueError("server review carries composer provenance")
        return self


class ReviewInitialResolution(_ClosedEvidence):
    accepted_value: str | None
    arguments_hash: Hash | None
    hash_domain_version: Literal["v2"] | None
    approved_prompt_artifact_hash: Hash | None
    actor: Literal["composer-llm"]
    interpretation_source: Literal["user_approved", "auto_interpreted_opt_out", "auto_interpreted_no_surfaces"]


class ReviewCohortMember(_ClosedEvidence):
    ordinal: int = Field(ge=0)
    candidate_state_id: str
    event_id: str
    tool_call_id: str = Field(min_length=1)
    semantic_material: ReviewSemanticMaterial
    semantic_hash: Hash
    initial_disposition: Literal["pending", "opted_out"]
    initial_resolution: ReviewInitialResolution
    initial_resolution_hash: Hash
    produced_state_id: str | None
    produced_state_content_hash: Hash | None

    @field_validator("candidate_state_id", "event_id", "produced_state_id")
    @classmethod
    def canonical_uuid(cls, value: str | None) -> str | None:
        if value is not None and str(UUID(value)) != value:
            raise ValueError("noncanonical owned UUID")
        return value

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.ordinal != self.semantic_material.ordinal or self.candidate_state_id != self.semantic_material.candidate_state_id:
            raise ValueError("cohort semantic identity mismatch")
        if self.semantic_hash != self.semantic_material.content_hash():
            raise ValueError("cohort semantic hash mismatch")
        if self.initial_resolution_hash != stable_hash(self.initial_resolution.model_dump(mode="json")):
            raise ValueError("cohort resolution hash mismatch")
        if (self.produced_state_id is None) != (self.produced_state_content_hash is None):
            raise ValueError("derived state binding incomplete")
        resolution = self.initial_resolution
        if self.initial_disposition == "pending":
            if (
                self.produced_state_id is not None
                or resolution.accepted_value is not None
                or resolution.arguments_hash is not None
                or resolution.hash_domain_version is not None
                or resolution.approved_prompt_artifact_hash is not None
                or resolution.interpretation_source != "user_approved"
            ):
                raise ValueError("pending member contains resolution")
        elif (
            self.produced_state_id is None
            or resolution.accepted_value != self.semantic_material.llm_draft
            or resolution.hash_domain_version != "v2"
            or resolution.arguments_hash is None
            or resolution.interpretation_source != "auto_interpreted_opt_out"
        ):
            raise ValueError("opted-out member resolution incomplete")
        return self


class TransitionAssistantBinding(_ClosedEvidence):
    message_id: str
    final_state_id: str
    content_hash: Hash
    raw_content_hash: Hash | None

    @field_validator("message_id", "final_state_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("noncanonical owned UUID")
        return value


class PipelineDispatchEvidence(_ClosedEvidence):
    tool_call_id: str = Field(min_length=1)
    tool_name: Literal["set_pipeline"]
    status: Literal["success"]
    arguments_hash: Hash
    result_hash: Hash


class PipelineAcceptedEvidence(_ClosedEvidence):
    schema_tag: Literal["pipeline_proposal_accepted.v2"] = Field(alias="schema")
    tool_call_id: str = Field(min_length=1)
    tool_name: Literal["set_pipeline"]
    status: Literal["committed"]
    outcome: Literal["accepted"]
    draft_hash: Hash
    committed_state_id: str
    committed_state_content_hash: Hash
    final_composer_metadata_hash: Hash
    dispatch: PipelineDispatchEvidence
    creation_composer_operation: ComposerOperationBinding | None
    settlement_composer_operation: ComposerOperationBinding | None
    review_cohort: tuple[ReviewCohortMember, ...]
    final_state_id: str
    final_state_content_hash: Hash
    transition_assistant: TransitionAssistantBinding | None

    @field_validator("committed_state_id", "final_state_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("noncanonical owned UUID")
        return value

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if tuple(member.ordinal for member in self.review_cohort) != tuple(range(len(self.review_cohort))):
            raise ValueError("cohort ordinal order malformed")
        for identities in (
            [member.event_id for member in self.review_cohort],
            [member.tool_call_id for member in self.review_cohort],
            [
                (member.semantic_material.affected_node_id, member.semantic_material.kind, member.semantic_material.user_term)
                for member in self.review_cohort
            ],
        ):
            if len(identities) != len(set(identities)):
                raise ValueError("duplicate cohort authority")
        if any(member.candidate_state_id != self.committed_state_id for member in self.review_cohort):
            raise ValueError("foreign candidate cohort")
        derived = [member for member in self.review_cohort if member.produced_state_id is not None]
        if len({member.produced_state_id for member in derived}) != len(derived) or any(
            member.produced_state_id == self.committed_state_id for member in derived
        ):
            raise ValueError("derived state identities are duplicated or reuse the candidate")
        last_id = derived[-1].produced_state_id if derived else self.committed_state_id
        last_hash = derived[-1].produced_state_content_hash if derived else self.committed_state_content_hash
        if self.final_state_id != last_id or self.final_state_content_hash != last_hash:
            raise ValueError("final cohort head mismatch")
        if self.transition_assistant is not None and self.transition_assistant.final_state_id != self.final_state_id:
            raise ValueError("assistant final head mismatch")
        return self


class ComposerRevocationEvidence(_ClosedEvidence):
    schema_tag: Literal["auto_commit.revoked.v2"] = Field(alias="schema")
    required_trust_mode: Literal["auto_commit"]
    current_trust_mode: Literal["explicit_approve"]
    composer_operation: ComposerOperationBinding
    proposal_id: str
    user_message_id: str
    tool_call_id: str = Field(min_length=1)
    dispatch: PipelineDispatchEvidence

    @field_validator("proposal_id", "user_message_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("noncanonical owned UUID")
        return value
