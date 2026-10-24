"""Complete cold evidence DATA; construction of a live owner is current-read owned."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.advisor_output import AdvisorAbandonmentResponse
from elspeth.web.composer.continuation import ContinuationValue, LogicalRequestBinding
from elspeth.web.composer.continuation_audit_buffer import RecorderSnapshot
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk, verify_private_chunk
from elspeth.web.composer.continuation_evidence_application_values import PortableP4ApplicationEntry
from elspeth.web.composer.continuation_message_shelf import MessageShelfSnapshot
from elspeth.web.composer.protocol import RetainedOperationContext
from elspeth.web.composer.state import CompositionState
from elspeth.web.credential_guard import require_no_credential_material, require_no_credential_material_in_state
from elspeth.web.sessions.continuation import HistoricalEffectLocator


class RetainedBlobPage(ContinuationValue):
    kind: Literal["blob"] = "blob"
    tool_call_id: str
    producer_call_id: str
    envelope_hash: str
    blob_id: str
    content_hash: str
    total_chars: int = Field(ge=0)
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_range(self) -> RetainedBlobPage:
        if not self.start_char <= self.end_char <= self.total_chars:
            raise ValueError("retained blob page exceeds its original character range")
        return self


class RetainedSourceInspection(ContinuationValue):
    kind: Literal["source"] = "source"
    tool_call_id: str
    producer_call_id: str
    envelope_hash: str
    blob_id: str
    total_size_bytes: int = Field(ge=0)
    byte_range_inspected: tuple[int, int]
    source_kind: Literal["csv", "jsonl", "json", "text", "unknown"]

    @model_validator(mode="after")
    def valid_range(self) -> RetainedSourceInspection:
        start, end = self.byte_range_inspected
        if not 0 <= start <= end <= self.total_size_bytes or str(UUID(self.blob_id)) != self.blob_id:
            raise ValueError("retained source inspection changed its immutable identity/range")
        return self


RetainedInspection = Annotated[RetainedBlobPage | RetainedSourceInspection, Field(discriminator="kind")]


class RetainedPageLink(ContinuationValue):
    key: str
    page_index: int = Field(ge=0)


class RetainedDeliveryAttempt(ContinuationValue):
    generation: int = Field(ge=0)
    registration_revision: int = Field(ge=0)
    messages_hash: str
    tools_hash: str | None
    previous_call_ids: tuple[str, ...]
    page_indexes: tuple[int, ...]


class RetainedDeliveryScope(ContinuationValue):
    scope: Literal["primary", "planner"]
    generation: int = Field(ge=0)
    current_call: tuple[str, str] | None
    current_page_indexes: tuple[int, ...]
    pending: RetainedDeliveryAttempt | None


class RetainedUntrustedPart(ContinuationValue):
    text: str
    inspection_text: str | None


class RetainedAbandonmentRecord(ContinuationValue):
    label: str
    content: str
    parts: tuple[RetainedUntrustedPart, ...]

    @model_validator(mode="after")
    def complete_partition(self) -> RetainedAbandonmentRecord:
        if "".join(part.text for part in self.parts) != self.content:
            raise ValueError("abandonment record lost part of its exact producer text")
        return self


class RetainedAbandonmentReview(ContinuationValue):
    binding: str
    request_scope: tuple[str | None, str | None, str]
    request_hash: str
    graph_hash: str
    policy_hash: str
    registration_revision: int = Field(ge=0)
    records: tuple[RetainedAbandonmentRecord, ...]
    transcript_binding: str | None


class RetainedAbandonmentDecision(ContinuationValue):
    review_index: int = Field(ge=0)
    decision_index: int = Field(ge=0)


class RetainedLimitation(ContinuationValue):
    binding: str
    value: RetainedAbandonmentDecision


class RetainedBatchEffects(ContinuationValue):
    assistant_message_id: str
    state_id: str
    version: int = Field(ge=1)
    tool_call_ids: tuple[str, ...]
    inserted_state_ids: tuple[str, ...]
    authored_state_hash: str


class RetainedTurnEffects(ContinuationValue):
    initial_state_json: str
    initial_state_id: str | None
    latest_verified_state_json: str
    latest_verified_state_id: str | None
    latest_committed_state_id: str | None
    verification: Literal["verified", "unavailable", "not_persisted"]
    initial_authored_state_hash: str
    latest_authored_state_hash: str
    net_changed_fields: tuple[str, ...]
    batches: tuple[RetainedBatchEffects, ...]
    committed_revision_ids: tuple[str, ...]


class RetainedHistoricalEffect(ContinuationValue):
    effect_key: str
    input_sha256: str
    creator_operation_id: str
    receipt_id: str
    inserted_state_ids: tuple[UUID, ...]
    authored_state_hash: str

    def locator(self) -> HistoricalEffectLocator:
        return HistoricalEffectLocator(
            self.effect_key,
            self.input_sha256,
            self.creator_operation_id,
            self.receipt_id,
            self.inserted_state_ids,
            self.authored_state_hash,
        )


class TurnEvidenceSnapshot(ContinuationValue):
    schema_version: Literal["composer.evidence.v1"] = "composer.evidence.v1"
    logical: LogicalRequestBinding
    actor_user_id: str
    session_id: str
    physical_operation_id: str
    physical_operation_epoch: int = Field(ge=1)
    request_id: str
    request_content_hash: str
    effects: RetainedTurnEffects
    transcript: MessageShelfSnapshot
    retained_history: str | None
    registration_revision: int = Field(ge=0)
    page_shelf: tuple[RetainedInspection, ...]
    registered_pages: tuple[RetainedPageLink, ...]
    quoted_inspections: tuple[RetainedPageLink, ...]
    historical_pages: tuple[RetainedPageLink, ...]
    conflicting_page_ids: tuple[str, ...]
    deliveries: tuple[RetainedDeliveryScope, ...]
    review_shelf: tuple[RetainedAbandonmentReview, ...]
    decision_shelf: tuple[AdvisorAbandonmentResponse, ...]
    abandonment_review: RetainedAbandonmentDecision | None
    abandonment_transcript_binding: str | None
    abandonment_limitations: tuple[RetainedLimitation, ...]
    effect_locators: tuple[RetainedHistoricalEffect, ...]
    initial_is_latest: bool
    recorder_json: str
    requires_blob_reader: bool
    historical_application_count: int = Field(ge=0)
    joint_graph_json: str | None = None
    application_entry: PortableP4ApplicationEntry | None = None

    @model_validator(mode="after")
    def complete_links(self) -> TurnEvidenceSnapshot:
        if self.session_id != self.logical.session_id or self.request_id != self.logical.original_user_message_id:
            raise ValueError("evidence differs from its logical request")
        if tuple(item.scope for item in self.deliveries) != ("primary", "planner"):
            raise ValueError("evidence must retain both ordered delivery scopes")
        links = (*self.registered_pages, *self.quoted_inspections, *self.historical_pages)
        indexes = [link.page_index for link in links]
        for delivery in self.deliveries:
            indexes.extend(delivery.current_page_indexes)
            if delivery.pending is not None:
                indexes.extend(delivery.pending.page_indexes)
                if tuple(sorted(set(delivery.pending.previous_call_ids))) != delivery.pending.previous_call_ids:
                    raise ValueError("delivery previous-call set lost its exact ordered encoding")
        if any(type(index) is not int or not 0 <= index < len(self.page_shelf) for index in indexes):
            raise ValueError("evidence page alias is out of range")
        if tuple(dict.fromkeys(indexes)) != tuple(range(len(self.page_shelf))):
            raise ValueError("evidence page shelf contains orphaned or reordered identities")
        if tuple(sorted(set(self.conflicting_page_ids))) != self.conflicting_page_ids:
            raise ValueError("evidence conflict set lost its exact ordered encoding")
        for collection in (self.registered_pages, self.quoted_inspections, self.historical_pages):
            if len({link.key for link in collection}) != len(collection):
                raise ValueError("evidence map contains duplicate keys")
        for link in (*self.registered_pages, *self.historical_pages):
            if link.key != self.page_shelf[link.page_index].tool_call_id:
                raise ValueError("evidence page-map key differs from its actual tool identity")
        if set(self.conflicting_page_ids).intersection(link.key for link in self.registered_pages):
            raise ValueError("conflicted page remains in the admitted registry")
        decisions = [] if self.abandonment_review is None else [self.abandonment_review]
        decisions.extend(item.value for item in self.abandonment_limitations)
        if any(not 0 <= item.review_index < len(self.review_shelf) for item in decisions):
            raise ValueError("evidence review alias is out of range")
        if any(not 0 <= item.decision_index < len(self.decision_shelf) for item in decisions):
            raise ValueError("evidence decision alias is out of range")
        if tuple(dict.fromkeys(item.review_index for item in decisions)) != tuple(range(len(self.review_shelf))) or tuple(
            dict.fromkeys(item.decision_index for item in decisions)
        ) != tuple(range(len(self.decision_shelf))):
            raise ValueError("evidence review/decision shelf contains orphaned or reordered identities")
        if self.initial_is_latest and self.effects.initial_state_json != self.effects.latest_verified_state_json:
            raise ValueError("shared initial/latest state changed its complete bytes")
        recorder_from_evidence_json(self.recorder_json)
        if len({item.binding for item in self.abandonment_limitations}) != len(self.abandonment_limitations):
            raise ValueError("evidence limitation map contains duplicate bindings")
        for item in self.abandonment_limitations:
            if item.binding != self.review_shelf[item.value.review_index].binding:
                raise ValueError("evidence limitation lost its actual review binding")
        if len({item.effect_key for item in self.effect_locators}) != len(self.effect_locators):
            raise ValueError("evidence contains duplicate SQL effect locators")
        if self.application_entry is None and self.historical_application_count != 0:
            raise ValueError("evidence application count lost its actual journal")
        if self.application_entry is None and (self.effect_locators or self.effects.batches or self.effects.committed_revision_ids):
            raise ValueError("cold evidence requires actual retained P4 provenance for committed history")
        if self.application_entry is not None and (
            self.joint_graph_json is not None
            or self.application_entry.logical != self.logical
            or self.historical_application_count != len(self.application_entry.applications)
        ):
            raise ValueError("cold evidence lost its complete portable application journal")
        if self.application_entry is None and (
            self.effects.initial_state_id != self.logical.base_state_id
            or self.effects.latest_verified_state_id != self.effects.initial_state_id
            or self.effects.latest_committed_state_id != self.effects.initial_state_id
            or self.effects.initial_state_json != self.effects.latest_verified_state_json
            or self.effects.net_changed_fields
            or self.effects.verification == "not_persisted"
        ):
            raise ValueError("pre-P4 evidence changed its complete admitted base frontier")
        # Validate complete owned state bytes now; the SQL authority separately
        # compares them with the actual base/receipt rows before construction.
        from elspeth.web.composer.turn_effects import authored_state_hash

        initial = state_from_evidence_json(self.effects.initial_state_json)
        latest = state_from_evidence_json(self.effects.latest_verified_state_json)
        if (
            authored_state_hash(initial) != self.effects.initial_authored_state_hash
            or authored_state_hash(latest) != self.effects.latest_authored_state_hash
        ):
            raise ValueError("evidence altered its actual authored graph hashes")
        if self.retained_history is not None:
            value, error = parse_json_strict(self.retained_history)
            if error is not None or type(value) is not dict or set(value) != {"retained_operation_history"}:
                raise ValueError("evidence retained history is not its complete object")
            history = value["retained_operation_history"]
            if type(history) is not dict or set(history) != {"availability", "has_more", "outcomes", "scope", "interpretation"}:
                raise ValueError("evidence retained history changed its producer fields")
            owned = TypeAdapter(RetainedOperationContext).validate_json(
                canonical_json(
                    {
                        "session_id": self.session_id,
                        "actor_user_id": self.actor_user_id,
                        "availability": history["availability"],
                        "has_more": history["has_more"],
                        "outcomes": history["outcomes"],
                    }
                ),
                strict=True,
            )
            if owned.provider_message() != self.retained_history:
                raise ValueError("evidence retained history changed its actual producer bytes")
        return self


def state_from_evidence_json(text: str) -> CompositionState:
    value, error = parse_json_strict(text)
    if error is not None or type(value) is not dict:
        raise ValueError("retained evidence state is not a strict object")
    state = CompositionState.from_dict(value)
    if canonical_json(state.to_dict()) != text:
        raise ValueError("retained evidence state is not a lossless canonical owned state")
    return state


def recorder_from_evidence_json(encoded: str) -> RecorderSnapshot:
    from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot

    chunk = encode_private_chunk(codec="composer.audit-buffer.v1", canonical=encoded)
    return decode_recorder_snapshot(chunk)


def _require_evidence_metadata(snapshot: TurnEvidenceSnapshot, *, env_ref_names: frozenset[str]) -> None:
    """Inspect owned metadata without treating an effect locator as a secret slot."""
    if type(snapshot) is not TurnEvidenceSnapshot:
        raise TypeError("evidence metadata requires its exact snapshot owner")
    # Keep every other locator/member under its original keyed traversal.
    # The actual effect_key field is a historical identifier, not a declared
    # credential field; its contents still receive the complete string guard.
    metadata = snapshot.model_dump(
        mode="json",
        exclude={
            "effects": True,
            "transcript": True,
            "recorder_json": True,
            "joint_graph_json": True,
            "application_entry": True,
            "effect_locators": {"__all__": {"effect_key"}},
        },
    )
    require_no_credential_material(metadata, surface="composer_continuation_evidence_metadata", env_ref_names=env_ref_names)
    for locator in snapshot.effect_locators:
        require_no_credential_material(locator.effect_key, surface="composer_continuation_effect_locator", env_ref_names=env_ref_names)


def encode_turn_evidence(
    snapshot: TurnEvidenceSnapshot,
    *,
    env_ref_names: frozenset[str] = frozenset(),
) -> PrivateChunk:
    if type(snapshot) is not TurnEvidenceSnapshot:
        raise TypeError("evidence requires its owned closed snapshot")
    encoded = canonical_json(snapshot.model_dump(mode="json"))
    try:
        checked = TurnEvidenceSnapshot.model_validate_json(encoded)
    except (ValidationError, ValueError, TypeError) as error:
        raise AuditIntegrityError("evidence capture violates its complete owned DATA contract") from error
    if canonical_json(checked.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("evidence capture changed fields during complete admission")
    require_no_credential_material_in_state(
        state_from_evidence_json(snapshot.effects.initial_state_json),
        surface="composer_continuation_evidence_initial_state",
        env_ref_names=env_ref_names,
    )
    require_no_credential_material_in_state(
        state_from_evidence_json(snapshot.effects.latest_verified_state_json),
        surface="composer_continuation_evidence_latest_state",
        env_ref_names=env_ref_names,
    )
    from elspeth.web.composer.continuation_evidence_application_data import require_evidence_application_links

    require_evidence_application_links(snapshot, env_ref_names=env_ref_names)
    # Graph, receipt, transcript and recorder admission stays with its actual
    # complete owner; this projection only admits the remaining metadata.
    _require_evidence_metadata(snapshot, env_ref_names=env_ref_names)
    return encode_private_chunk(
        codec="composer.evidence.v1",
        canonical=canonical_json(snapshot.model_dump(mode="json")),
        env_ref_names=env_ref_names,
    )


def decode_turn_evidence(chunk: PrivateChunk, *, env_ref_names: frozenset[str]) -> TurnEvidenceSnapshot:
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.evidence.v1":
        raise AuditIntegrityError("evidence chunk has the wrong codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    value, error = parse_json_strict(chunk.canonical)
    if error is not None or type(value) is not dict or canonical_json(value) != chunk.canonical:
        raise AuditIntegrityError("evidence chunk is not its complete canonical object")
    try:
        snapshot = TurnEvidenceSnapshot.model_validate_json(chunk.canonical)
    except (ValidationError, ValueError, TypeError) as exc:
        raise AuditIntegrityError("evidence chunk violates its closed owned codec") from exc
    if canonical_json(snapshot.model_dump(mode="json")) != chunk.canonical:
        raise AuditIntegrityError("evidence chunk changed fields during its owned decode")
    if encode_turn_evidence(snapshot, env_ref_names=env_ref_names) != chunk:
        raise AuditIntegrityError("evidence complete admission changed its chunk")
    return snapshot
