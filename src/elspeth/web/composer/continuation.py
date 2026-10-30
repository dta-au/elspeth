from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from elspeth.contracts.composer_mutation import ComposerMutationCeiling, ComposerMutationRevocation
from elspeth.contracts.errors import AuditIntegrityError

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_codec import PrivateChunk
    from elspeth.web.composer.protocol import ComposerResult


class ContinuationValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LogicalRequestBinding(ContinuationValue):
    session_id: str
    operation_id: str
    original_user_message_id: str
    request_hash: str
    kind: Literal["compose_message", "compose_recompose"]
    base_state_id: str | None


class MutationCeilingSnapshot(ContinuationValue):
    session_id: str
    request_id: str
    initial_trust_mode: Literal["auto_commit", "explicit_approve"]

    def materialize(self) -> ComposerMutationCeiling:
        return ComposerMutationCeiling(self.session_id, self.request_id, self.initial_trust_mode)

    @classmethod
    def capture(cls, value: ComposerMutationCeiling) -> MutationCeilingSnapshot:
        if type(value) is not ComposerMutationCeiling:
            raise TypeError("Continuation capture requires an owned mutation ceiling")
        return cls(session_id=value.session_id, request_id=value.request_id, initial_trust_mode=value.initial_trust_mode)

    def require_logical(self, logical: LogicalRequestBinding) -> None:
        self.materialize()
        if self.session_id != logical.session_id or self.request_id != logical.original_user_message_id:
            raise AuditIntegrityError("Continuation mutation ceiling changed its original request")


class MutationRevocationSnapshot(ContinuationValue):
    ceiling: MutationCeilingSnapshot
    current_trust_mode: Literal["auto_commit", "explicit_approve"]

    def materialize(self) -> ComposerMutationRevocation:
        return ComposerMutationRevocation(self.ceiling.materialize(), self.current_trust_mode)

    @classmethod
    def capture(cls, value: ComposerMutationRevocation) -> MutationRevocationSnapshot:
        if type(value) is not ComposerMutationRevocation:
            raise TypeError("Continuation refusal capture requires its actual nominal value")
        return cls(ceiling=MutationCeilingSnapshot.capture(value.ceiling), current_trust_mode=value.current_trust_mode)


class ContinuationProviderCostPolicy(ContinuationValue):
    applicability: Literal["not_applicable"]
    lifecycle_version: Literal[2]
    accounting_owner: Literal["physical_audit_and_token_ledger"]


class PrivateChunkRef(ContinuationValue):
    codec: Literal[
        "composer.messages.v1",
        "composer.tools.v1",
        "composer.state.v1",
        "composer.evidence.v2",
        "composer.audit-buffer.v1",
        "composer.transcript.v1",
        "composer.tool-batch.v1",
        "composer.coupled-batch-data.v1",
        "composer.p4-pending-batch.v1",
        "composer.prepared-p4.v1",
        "composer.applied-p4-batch.v1",
        "composer.advisor.v1",
        "composer.end.v1",
        "composer.caches.v1",
        "composer.attachment-admissions.v1",
        "composer.response.v1",
        "composer.title-input.v1",
        "composer.title-provider-result.v1",
        "composer.semantic-frontier.v1",
        "composer.owner-result.v1",
        "composer.bootstrap.v1",
        "composer.iteration.v1",
    ]
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=0)


class EffectFrontierEntry(ContinuationValue):
    effect_id: str
    disposition: Literal["prepared", "dispatched", "committed", "refused", "unknown"]
    receipt_id: str | None


class RetainedOwnerResultRef(ContinuationValue):
    effect_key: str
    kind: Literal["proposal", "persisted_turn", "read", "interpretation"]
    result: PrivateChunkRef

    @model_validator(mode="after")
    def require_owner_result_codec(self) -> RetainedOwnerResultRef:
        if self.result.codec != "composer.owner-result.v1":
            raise ValueError("owner result requires its closed receipt codec")
        return self


class ContinuationProviderFrame(ContinuationValue):
    provider_effect_key: str | None = None
    provider_input_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_paired_provider_locator(self) -> ContinuationProviderFrame:
        if (self.provider_effect_key is None) != (self.provider_input_sha256 is None):
            raise ValueError("provider frame lost its selected prepared provider locator")
        return self


class InitializeFrame(ContinuationProviderFrame):
    frame_kind: Literal["initialize"] = "initialize"
    request_json: str
    ingress_phase: Literal["reserved", "inserted", "title_pending", "ready"]
    title_attempt_id: str | None
    ingress_receipt_json: str | None = None
    bootstrap: PrivateChunkRef | None = None
    title_response: PrivateChunkRef | None = None

    @model_validator(mode="after")
    def require_prepared_original_bootstrap(self) -> InitializeFrame:
        if self.ingress_phase in ("title_pending", "ready") and self.bootstrap is None:
            raise ValueError("prepared initialization lost its original pre-title bootstrap")
        return self


class ComposeFrame(ContinuationProviderFrame):
    frame_kind: Literal["compose"] = "compose"
    phase: Literal["action_ready", "awaiting_response", "response_retained", "classify"]
    bootstrap: PrivateChunkRef
    iteration: PrivateChunkRef
    messages: PrivateChunkRef
    tools: PrivateChunkRef | None
    initial_state: PrivateChunkRef
    state: PrivateChunkRef
    evidence: PrivateChunkRef
    audit_buffer: PrivateChunkRef
    transcript: PrivateChunkRef
    caches: PrivateChunkRef
    attachment_admissions: PrivateChunkRef
    initial_version: int = Field(ge=1)
    current_state_id: str | None
    slice_sequence: int = Field(ge=1)
    composition_slice: int = Field(ge=0)
    discovery_slice: int = Field(ge=0)
    composition_total: int = Field(ge=0)
    discovery_total: int = Field(ge=0)
    advisor_calls: int = Field(ge=0)
    repair_turns: int = Field(ge=0)
    advisor_checkpoint_passes: int = Field(ge=0)
    mutation_success_seen: bool
    persisted_assistant_message_id: str | None
    persisted_assistant_content: str | None
    persisted_tool_call_turn: bool
    advisor_repair_context_introduced: bool
    pending_advisor_elision_indices: tuple[int, ...]
    pending_response: PrivateChunkRef | None

    @model_validator(mode="after")
    def require_evidence_recorder_codecs(self) -> ComposeFrame:
        if self.evidence.codec != "composer.evidence.v2" or self.audit_buffer.codec != "composer.audit-buffer.v1":
            raise ValueError("Compose frame changed its closed evidence/recorder source roles")
        return self


class ToolBatchFrame(ContinuationValue):
    frame_kind: Literal["tool_batch"] = "tool_batch"
    phase: Literal["tool_ready", "effect_reconciling", "result_retained"]
    response: PrivateChunkRef
    accumulator: PrivateChunkRef
    next_tool_index: int = Field(ge=0)
    settled_effect_ids: tuple[str, ...]


class AdvisorFrame(ContinuationProviderFrame):
    frame_kind: Literal["advisor"] = "advisor"
    phase: Literal["input_ready", "awaiting_response", "response_retained", "decision_ready"]
    review: PrivateChunkRef
    response: PrivateChunkRef | None
    coverage_binding: str


class EndFrame(ContinuationValue):
    frame_kind: Literal["end"] = "end"
    phase: Literal["review_needed", "repair_needed", "publication_ready"]
    state: PrivateChunkRef
    candidate: PrivateChunkRef
    publication_binding: str | None


Frame = Annotated[
    InitializeFrame | ComposeFrame | ToolBatchFrame | AdvisorFrame | EndFrame,
    Field(discriminator="frame_kind"),
]


class ContinuationActionSource(ContinuationValue):
    effect_key: str = Field(pattern=r"^provider:1:(0|[1-9][0-9]*)$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response: PrivateChunkRef

    @model_validator(mode="after")
    def require_actual_advisor_response(self) -> ContinuationActionSource:
        if self.response.codec != "composer.response.v1":
            raise ValueError("prescribed action source requires its actual admitted Advisor response")
        return self


class ContinuationFrameStack(ContinuationValue):
    schema_version: Literal["composer.continuation.snapshot.v1"] = "composer.continuation.snapshot.v1"
    logical: LogicalRequestBinding
    mutation_ceiling: MutationCeilingSnapshot
    provider_cost_policy: ContinuationProviderCostPolicy
    policy_hash: str
    admission_policy_json: str
    cursor: int = Field(ge=0)
    frames: tuple[Frame, ...] = Field(min_length=1)
    effect_frontier: tuple[EffectFrontierEntry, ...]
    pending_settlement_ids: tuple[str, ...]
    semantic_frontier: PrivateChunkRef | None
    active_action_json: str | None = None
    active_action_source: ContinuationActionSource | None = None
    pending_owner_results: tuple[RetainedOwnerResultRef, ...] = ()

    @property
    def is_handoff_safe(self) -> bool:
        if self.pending_settlement_ids or self.pending_owner_results:
            return False
        if any(e.disposition in ("dispatched", "unknown") for e in self.effect_frontier):
            return False
        top = self.frames[-1]
        return (
            (isinstance(top, InitializeFrame) and top.ingress_phase == "ready")
            or (isinstance(top, ComposeFrame) and top.phase == "action_ready")
            or (isinstance(top, ToolBatchFrame) and top.phase == "tool_ready")
            or (isinstance(top, AdvisorFrame) and top.phase == "input_ready")
            or (isinstance(top, EndFrame) and top.phase in ("review_needed", "repair_needed", "publication_ready"))
        )

    @model_validator(mode="after")
    def require_unique_effects(self) -> ContinuationFrameStack:
        self.mutation_ceiling.require_logical(self.logical)
        if (self.active_action_json is None) != (self.active_action_source is None):
            raise ValueError("prescribed action lost its exact actual Advisor source")
        keys = tuple(effect.effect_id for effect in self.effect_frontier)
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate continuation effect identity")
        pending_keys = tuple(result.effect_key for result in self.pending_owner_results)
        if len(set(pending_keys)) != len(pending_keys):
            raise ValueError("duplicate pending owner result identity")
        effects = {effect.effect_id: effect for effect in self.effect_frontier}
        for result in self.pending_owner_results:
            effect = effects.get(result.effect_key)
            if effect is None or effect.disposition != "committed" or effect.receipt_id is None:
                raise ValueError("pending owner result lacks its committed effect receipt")
        return self


USER_MESSAGE_NAMESPACE = UUID("7ba7381f-68a6-5ae5-a7ec-c9a52b05a3e2")


def reserved_user_message_id(session_id: UUID, operation_id: str) -> str:
    UUID(operation_id)
    return str(uuid5(USER_MESSAGE_NAMESPACE, f"{session_id}:{operation_id}:composer.v2.user-message"))


@dataclass(frozen=True, slots=True)
class ContinuationAdmissionSeed:
    snapshot_json: str
    reserved_user_message_id: str
    action_json: str
    policy_hash: str

    def validate_binding(
        self,
        *,
        session_id: UUID,
        operation_id: str,
        kind: str,
        request_json: str,
        request_hash: str,
        base_state_id: UUID | None,
        reserved_user_message_id: str,
    ) -> None:
        from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, require_continuation_request_admission

        require_continuation_request_admission(request_json)
        stack = decode_continuation_snapshot(self.snapshot_json)
        expected = (
            str(session_id),
            operation_id,
            reserved_user_message_id,
            request_hash,
            kind,
            str(base_state_id) if base_state_id is not None else None,
        )
        logical = stack.logical
        actual = (
            logical.session_id,
            logical.operation_id,
            logical.original_user_message_id,
            logical.request_hash,
            logical.kind,
            logical.base_state_id,
        )
        first = stack.frames[0]
        if (
            actual != expected
            or self.reserved_user_message_id != reserved_user_message_id
            or stack.policy_hash != self.policy_hash
            or stack.cursor != 0
            or len(stack.frames) != 1
            or not isinstance(first, InitializeFrame)
            or first.request_json != request_json
            or first.ingress_phase != "reserved"
        ):
            raise AuditIntegrityError("continuation initializer differs from admission")


def prepare_composer_continuation_admission(
    *,
    session_id: UUID,
    operation_id: str,
    kind: Literal["compose_message", "compose_recompose"],
    request_json: str,
    request_hash: str,
    base_state_id: UUID | None,
    policy_hash: str,
    admission_policy_json: str,
    initial_trust_mode: Literal["auto_commit", "explicit_approve"],
    initial_action_json: str,
    original_user_message_id: str | None = None,
) -> ContinuationAdmissionSeed:
    from elspeth.web.composer.continuation_codec import encode_continuation_snapshot, require_continuation_request_admission

    if kind == "compose_recompose":
        if original_user_message_id is None:
            raise AuditIntegrityError("recompose requires verified original user ingress")
        message_id = original_user_message_id
    else:
        if original_user_message_id is not None:
            raise AuditIntegrityError("compose cannot choose another original message")
        message_id = reserved_user_message_id(session_id, operation_id)
    require_continuation_request_admission(request_json)
    stack = ContinuationFrameStack(
        logical=LogicalRequestBinding(
            session_id=str(session_id),
            operation_id=operation_id,
            original_user_message_id=message_id,
            request_hash=request_hash,
            kind=kind,
            base_state_id=str(base_state_id) if base_state_id is not None else None,
        ),
        mutation_ceiling=MutationCeilingSnapshot(
            session_id=str(session_id),
            request_id=message_id,
            initial_trust_mode=initial_trust_mode,
        ),
        provider_cost_policy=ContinuationProviderCostPolicy(
            applicability="not_applicable",
            lifecycle_version=2,
            accounting_owner="physical_audit_and_token_ledger",
        ),
        policy_hash=policy_hash,
        admission_policy_json=admission_policy_json,
        cursor=0,
        frames=(InitializeFrame(request_json=request_json, ingress_phase="reserved", title_attempt_id=None),),
        effect_frontier=(),
        pending_settlement_ids=(),
        semantic_frontier=None,
    )
    return ContinuationAdmissionSeed(encode_continuation_snapshot(stack), message_id, initial_action_json, policy_hash)


@dataclass(frozen=True, slots=True)
class ContinuationAdvance:
    checkpoint_id: str
    stack: ContinuationFrameStack
    chunks: tuple[PrivateChunk, ...]
    result: ComposerResult | None
