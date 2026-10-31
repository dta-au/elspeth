"""Complete cold evidence DATA; construction of a live owner is current-read owned."""

from __future__ import annotations

from _thread import LockType
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Annotated, Literal, Never, SupportsIndex
from uuid import UUID

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from elspeth.contracts.bounded_serialization import utf8_size
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.advisor_output import AdvisorAbandonmentResponse
from elspeth.web.composer.continuation import ContinuationValue, LogicalRequestBinding, PrivateChunkRef
from elspeth.web.composer.continuation_audit_buffer import RecorderSnapshot
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk, verify_private_chunk
from elspeth.web.composer.continuation_evidence_application_values import P4ApplicationSources, PortableP4ApplicationEntry
from elspeth.web.composer.continuation_limits import (
    CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES,
    CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
)
from elspeth.web.composer.continuation_message_shelf import MessageShelfSnapshot
from elspeth.web.composer.protocol import RetainedOperationContext
from elspeth.web.composer.state import CompositionState
from elspeth.web.credential_guard import require_no_credential_material, require_no_credential_material_in_state
from elspeth.web.sessions.continuation import HistoricalEffectLocator

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_application_graph import ApplicationGraph, ApplicationRecorderSource


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


class TurnEvidenceFields(ContinuationValue):
    """Actual non-transcript evidence fields and their shared live invariants.

    This value has no retained codec. Fresh capture validates these complete
    current fields before deciding a borrowed transcript's resource admission;
    the stored snapshot below inherits the same fields and validator.
    """

    schema_version: Literal["composer.evidence.v2"] = "composer.evidence.v2"
    logical: LogicalRequestBinding
    actor_user_id: str
    session_id: str
    physical_operation_id: str
    physical_operation_epoch: int = Field(ge=1)
    request_id: str
    request_content_hash: str
    effects: RetainedTurnEffects
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
    recorder: PrivateChunkRef
    requires_blob_reader: bool
    historical_application_count: int = Field(ge=0)
    joint_graph_json: str | None = None
    application_entry: PortableP4ApplicationEntry | None = None

    @model_validator(mode="after")
    def complete_links(self) -> TurnEvidenceFields:
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
        if self.recorder.codec != "composer.audit-buffer.v1":
            raise ValueError("evidence recorder changed its closed original source role")
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


class TurnEvidenceSnapshot(TurnEvidenceFields):
    transcript: MessageShelfSnapshot


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


def _require_evidence_metadata(snapshot: TurnEvidenceFields, *, env_ref_names: frozenset[str]) -> None:
    """Inspect owned metadata without treating an effect locator as a secret slot."""
    if type(snapshot) not in (TurnEvidenceFields, TurnEvidenceSnapshot):
        raise TypeError("evidence metadata requires its exact snapshot owner")
    # Keep every other locator/member under its original keyed traversal.
    # The actual effect_key field is a historical identifier, not a declared
    # credential field; its contents still receive the complete string guard.
    metadata = snapshot.model_dump(
        mode="json",
        exclude={
            "effects": True,
            "transcript": True,
            "recorder": True,
            "joint_graph_json": True,
            "application_entry": True,
            "effect_locators": {"__all__": {"effect_key"}},
        },
    )
    require_no_credential_material(metadata, surface="composer_continuation_evidence_metadata", env_ref_names=env_ref_names)
    for locator in snapshot.effect_locators:
        require_no_credential_material(locator.effect_key, surface="composer_continuation_effect_locator", env_ref_names=env_ref_names)


@dataclass(frozen=True, slots=True, init=False, eq=False)
class AdmittedTurnEvidence:
    """Source-admitted DATA for one fixed reconstruction, never current authority.

    Construction admits actual source bytes. There is no snapshot/graph field
    constructor, allocation token or caller-supplied admission flag.
    """

    snapshot: TurnEvidenceSnapshot
    _graph: ApplicationGraph | None
    _graph_lock: LockType
    _graph_taken: bool
    env_ref_names: frozenset[str]
    source_chunk: PrivateChunk
    sources: P4ApplicationSources
    recorder_chunk: PrivateChunk

    def __init__(
        self,
        source: TurnEvidenceSnapshot | PrivateChunk,
        *,
        env_ref_names: frozenset[str],
        mandatory_retention: bool = False,
        source_chunks: tuple[PrivateChunk, ...] = (),
    ) -> None:
        if type(env_ref_names) is not frozenset or any(type(name) is not str for name in env_ref_names):
            raise TypeError("evidence admission requires its closed environment-name set")
        if type(source) is PrivateChunk:
            if source.reference.codec != "composer.evidence.v2":
                raise AuditIntegrityError("evidence chunk has the wrong codec")
            if not 0 <= source.reference.byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES or (
                utf8_size(source.canonical, limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES) != source.reference.byte_length
            ):
                raise AuditIntegrityError("evidence source exceeds its original complete document bound")
            verify_private_chunk(source.reference, source.canonical)
            # The original verifier proved strict canonical JSON. The owned
            # model below separately requires its complete object schema.
            try:
                snapshot = TurnEvidenceSnapshot.model_validate_json(source.canonical)
            except (ValidationError, ValueError, TypeError) as exc:
                raise AuditIntegrityError("evidence chunk violates its closed owned codec") from exc
            if canonical_json(snapshot.model_dump(mode="json")) != source.canonical:
                raise AuditIntegrityError("evidence chunk changed fields during its owned decode")
        elif type(source) is TurnEvidenceSnapshot:
            snapshot = source
        else:
            raise TypeError("evidence admission requires its actual snapshot or private chunk source")
        sources = P4ApplicationSources(snapshot.application_entry, source_chunks)
        recorder_chunk = _select_recorder_chunk(snapshot, source_chunks)
        chunk, graph = _encode_turn_evidence_source(
            snapshot,
            env_ref_names=env_ref_names,
            mandatory_retention=mandatory_retention,
            sources=sources,
            recorder_chunk=recorder_chunk,
        )
        if type(source) is PrivateChunk and chunk != source:
            raise AuditIntegrityError("evidence complete admission changed its chunk")
        object.__setattr__(self, "snapshot", snapshot)
        object.__setattr__(self, "_graph", graph)
        object.__setattr__(self, "_graph_lock", Lock())
        object.__setattr__(self, "_graph_taken", False)
        object.__setattr__(self, "env_ref_names", env_ref_names)
        object.__setattr__(self, "source_chunk", source if type(source) is PrivateChunk else chunk)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "recorder_chunk", recorder_chunk)

    @property
    def dependency_chunks(self) -> tuple[PrivateChunk, ...]:
        """The one original recorder plus the unchanged four-role journal leaves."""
        return (self.recorder_chunk, *self.sources.chunks)

    @property
    def graph(self) -> ApplicationGraph | None:
        with self._graph_lock:
            if self._graph_taken:
                raise AuditIntegrityError("admitted graph was already transferred to its mutable owner")
            return self._graph

    def take_graph(self) -> ApplicationGraph | None:
        """Transfer once, after proving exposed nested values still match source."""
        from elspeth.web.composer.continuation_application_graph import admit_application_recorder_source, encode_application_graph

        with self._graph_lock:
            if self._graph_taken:
                raise AuditIntegrityError("admitted graph cannot be transferred twice")
            if canonical_json(self.snapshot.model_dump(mode="json")) != self.source_chunk.canonical:
                raise AuditIntegrityError("admitted evidence snapshot changed its original immutable source")
            if self._graph is not None:
                original = (
                    self.snapshot.joint_graph_json
                    if self.snapshot.application_entry is None
                    else self.snapshot.application_entry.graph_json
                )
                recorder_source = admit_application_recorder_source(self.recorder_chunk, env_ref_names=self.env_ref_names)
                if encode_application_graph(self._graph, env_ref_names=self.env_ref_names, recorder_source=recorder_source) != original:
                    raise AuditIntegrityError("admitted graph changed its original values or alias partition")
            object.__setattr__(self, "_graph_taken", True)
            return self._graph

    def __copy__(self) -> Never:
        raise TypeError("evidence copies require fresh source admission")

    def __deepcopy__(self, memo: dict[int, object]) -> Never:
        raise TypeError("evidence copies require fresh source admission")

    def __reduce_ex__(self, protocol: SupportsIndex) -> Never:
        raise TypeError("admitted evidence is process-local; serialize its owned source")


def admit_turn_evidence(
    snapshot: TurnEvidenceSnapshot,
    *,
    env_ref_names: frozenset[str],
    mandatory_retention: bool = False,
    source_chunks: tuple[PrivateChunk, ...] = (),
) -> AdmittedTurnEvidence:
    if type(snapshot) is not TurnEvidenceSnapshot:
        raise TypeError("evidence requires its owned closed snapshot")
    return AdmittedTurnEvidence(snapshot, env_ref_names=env_ref_names, mandatory_retention=mandatory_retention, source_chunks=source_chunks)


def require_admitted_turn_evidence(evidence: AdmittedTurnEvidence, *, env_ref_names: frozenset[str]) -> AdmittedTurnEvidence:
    if type(evidence) is not AdmittedTurnEvidence:
        raise TypeError("evidence reuse requires its actual source-admitted owner")
    if type(env_ref_names) is not frozenset or any(type(name) is not str for name in env_ref_names):
        raise TypeError("evidence reuse requires its closed environment-name set")
    if evidence.env_ref_names != env_ref_names:
        raise AuditIntegrityError("evidence reuse changed its admitted credential environment")
    return evidence


def encode_turn_evidence(
    evidence: AdmittedTurnEvidence,
    *,
    env_ref_names: frozenset[str] = frozenset(),
    mandatory_retention: bool = False,
) -> PrivateChunk:
    admitted = require_admitted_turn_evidence(evidence, env_ref_names=env_ref_names)
    # This serializes the admitted immutable source, never a subsequently
    # mutated live graph. Changed captures require a new source constructor.
    # Mandatory fresh retention was decided by that actual constructor before
    # it produced this chunk; stored decoding keeps its separate DATA boundary.
    return admitted.source_chunk


def _select_recorder_chunk(snapshot: TurnEvidenceFields, chunks: tuple[PrivateChunk, ...]) -> PrivateChunk:
    """Resolve the closed role, checking every conflicting occurrence first."""
    if type(chunks) is not tuple:
        raise TypeError("evidence recorder requires its original source cohort")
    available: dict[tuple[str, str], PrivateChunk] = {}
    for chunk in chunks:
        if type(chunk) is not PrivateChunk:
            raise AuditIntegrityError("evidence recorder cohort contains a foreign source")
        key = chunk.reference.codec, chunk.reference.digest
        previous = available.get(key)
        if previous is not None and previous != chunk:
            raise AuditIntegrityError("evidence dependency has conflicting original bytes or lengths before deduplication")
        available.setdefault(key, chunk)
    reference = snapshot.recorder
    if type(reference) is not PrivateChunkRef or reference.codec != "composer.audit-buffer.v1":
        raise AuditIntegrityError("evidence recorder lost its nominal closed original reference")
    actual = available.get((reference.codec, reference.digest))
    if actual is None or actual.reference != reference:
        raise AuditIntegrityError("evidence lost its original recorder dependency")
    return actual


def _require_recorder_chunk(
    snapshot: TurnEvidenceFields, chunk: PrivateChunk, *, env_ref_names: frozenset[str]
) -> ApplicationRecorderSource:
    from elspeth.web.composer.continuation_application_graph import admit_application_recorder_source

    if type(chunk) is not PrivateChunk or chunk.reference != snapshot.recorder or chunk.reference.codec != "composer.audit-buffer.v1":
        raise AuditIntegrityError("evidence recorder differs from its original closed source")
    return admit_application_recorder_source(chunk, env_ref_names=env_ref_names)


def require_evidence_fields_admission(
    snapshot: TurnEvidenceFields,
    *,
    env_ref_names: frozenset[str],
    recorder_chunk: PrivateChunk,
    sources: P4ApplicationSources | None = None,
) -> ApplicationGraph | None:
    """Admit actual shared evidence fields without encoding a transcript.

    Fresh capture supplies its validated current field value; stored encoding
    supplies the complete snapshot. Graph/application and credential checks are
    the same source-owned operations for both, never a stored-source substitute.
    """
    if type(snapshot) not in (TurnEvidenceFields, TurnEvidenceSnapshot):
        raise TypeError("evidence field admission requires its actual complete owned value")
    from elspeth.web.composer.continuation_retention import validate_retained_model

    validate_retained_model(snapshot)
    recorder_source = _require_recorder_chunk(snapshot, recorder_chunk, env_ref_names=env_ref_names)
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

    graph = require_evidence_application_links(snapshot, env_ref_names=env_ref_names, sources=sources, recorder_source=recorder_source)
    _require_evidence_metadata(snapshot, env_ref_names=env_ref_names)
    return graph


def _encode_turn_evidence_source(
    snapshot: TurnEvidenceSnapshot,
    *,
    env_ref_names: frozenset[str] = frozenset(),
    mandatory_retention: bool = False,
    sources: P4ApplicationSources,
    recorder_chunk: PrivateChunk,
) -> tuple[PrivateChunk, ApplicationGraph | None]:
    if type(snapshot) is not TurnEvidenceSnapshot:
        raise TypeError("evidence requires its owned closed snapshot")
    from elspeth.contracts.bounded_serialization import bounded_canonical_json_size
    from elspeth.web.composer.continuation_retention import (
        bind_retained_json_encoding,
        require_retention_capacity,
        retained_model_json_view,
    )

    measured = bounded_canonical_json_size(retained_model_json_view(snapshot), limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)
    graph = require_evidence_fields_admission(snapshot, env_ref_names=env_ref_names, sources=sources, recorder_chunk=recorder_chunk)
    from elspeth.web.composer.continuation_message_shelf import require_message_shelf_integrity

    require_message_shelf_integrity(snapshot.transcript, env_ref_names=env_ref_names)
    if mandatory_retention:
        # A complete shelf has its own first-party limit. Evidence may then
        # exceed independently through complete states, journals and recorder.
        require_retention_capacity(
            bounded_canonical_json_size(retained_model_json_view(snapshot.transcript), limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES),
            reason="retained_history_bytes",
        )
        expected_bytes = require_retention_capacity(measured, reason="retained_chunk_bytes")
    else:
        if measured.admitted_bytes is None:
            raise AuditIntegrityError("evidence source exceeds its original complete document bound")
        expected_bytes = measured.admitted_bytes
    # This is one explicit two-document unit, independent of the selected
    # frame's reference occurrence. It is not a complete-closure/work bound.
    if expected_bytes + recorder_chunk.reference.byte_length > CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES:
        if mandatory_retention:
            from elspeth.web.composer.protocol import ComposerContextUnsupported

            raise ComposerContextUnsupported("retained_chunk_bytes")
        raise AuditIntegrityError("evidence and original recorder exceed their complete pair bound")
    encoded = canonical_json(snapshot.model_dump(mode="json"))
    bind_retained_json_encoding(encoded, expected_bytes=expected_bytes)
    try:
        checked = TurnEvidenceSnapshot.model_validate_json(encoded)
    except (ValidationError, ValueError, TypeError) as error:
        raise AuditIntegrityError("evidence capture violates its complete owned DATA contract") from error
    if canonical_json(checked.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("evidence capture changed fields during complete admission")
    # Graph, receipt, transcript and recorder admission stays with its actual
    # complete owner; this projection only admits the remaining metadata.
    chunk = encode_private_chunk(
        codec="composer.evidence.v2",
        canonical=encoded,
        env_ref_names=env_ref_names,
    )
    return chunk, graph


def decode_turn_evidence(
    chunk: PrivateChunk, *, env_ref_names: frozenset[str], source_chunks: tuple[PrivateChunk, ...] = ()
) -> AdmittedTurnEvidence:
    if type(chunk) is not PrivateChunk:
        raise AuditIntegrityError("evidence chunk has the wrong codec")
    return AdmittedTurnEvidence(chunk, env_ref_names=env_ref_names, source_chunks=source_chunks)
