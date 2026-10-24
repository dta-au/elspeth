"""Complete pre-P4 evidence graph capture and current-read-owned reconstruction.

No public constructor accepts decoded DATA as current authority. Committed
history/application-ledger cuts are refused until their actual producer is retained.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.prompt_guard import PromptGuardContentPart
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer import continuation_evidence as ce
from elspeth.web.composer.advisor_abandonment import AdvisorAbandonmentReview, AdvisorEvidenceRecord
from elspeth.web.composer.advisor_output import AdvisorAbandonmentResponse
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptOwner
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation import LogicalRequestBinding
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, encode_recorder_snapshot
from elspeth.web.composer.continuation_batch_data import RestoredBatchData, RestoredPreparedBatchData
from elspeth.web.composer.continuation_dispatch_audit import PortableDispatchAudit
from elspeth.web.composer.continuation_evidence_application_values import PortableP4ApplicationEntry
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.transcript_blob_reader import CurrentTranscriptBlobReader
from elspeth.web.composer.turn_effects import (
    BlobDeliveryAttempt,
    DeliveryScope,
    PersistedBatchEffects,
    PersistedTurnEffects,
    TurnEvidence,
    _QuotedInspectionProjection,
    _RegisteredBlobPage,
    _RegisteredInspection,
    _RegisteredSourceInspection,
)

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers


@dataclass(frozen=True, slots=True)
class RestoredTurnEvidence:
    evidence: TurnEvidence
    messages: list[dict[str, Any]]
    recorder: BufferingRecorder
    drained: RecorderDrainCursors
    batch: RestoredBatchData | None = None
    applications: PortableP4ApplicationEntry | None = None
    selected_audit: PortableDispatchAudit | None = None
    selected_dialect: ToolContractDialect | None = None


def capture_turn_evidence(
    owner: TurnEvidence,
    messages: Sequence[Mapping[str, Any]],
    *,
    logical: LogicalRequestBinding,
    recorder: BufferingRecorder,
    drained: RecorderDrainCursors,
    env_ref_names: frozenset[str],
    data: RestoredBatchData | RestoredPreparedBatchData | None = None,
    materialization: CurrentContinuationMessageProducers | None = None,
) -> ce.TurnEvidenceSnapshot:
    if type(owner) is not TurnEvidence or type(recorder) is not BufferingRecorder:
        raise TypeError("evidence capture requires its actual live owners")
    actor = owner._continuation_actor_user_id
    epoch = owner._continuation_current_epoch
    if type(logical) is not LogicalRequestBinding:
        raise TypeError("evidence capture requires its complete logical binding")
    if (
        logical.session_id != owner.session_id
        or logical.original_user_message_id != owner.request_id
        or logical.base_state_id != owner.effects.initial_state_id
    ):
        raise AuditIntegrityError("evidence capture changed its logical request/base")
    reader = owner.transcript._blob_reader
    if reader is not None:
        if type(reader) is not CurrentTranscriptBlobReader:
            raise AuditIntegrityError("cold evidence requires current blob-reader materialization from its actual owned reader")
        reader.require_for_evidence(owner)
    from elspeth.web.composer.continuation_evidence_application_data import capture_joint_evidence_graph, journal_effect_locators

    joint_graph_json, application_entry = capture_joint_evidence_graph(
        owner=owner, recorder=recorder, drained=drained, data=data, env_ref_names=env_ref_names
    )
    if application_entry is None and (owner.effects.batches or owner.effects.committed_revision_ids):
        raise AuditIntegrityError("cold evidence requires actual retained P4 provenance for committed history")
    if logical is None or actor is None or owner.session_id is None or owner.operation_id is None or epoch is None:
        raise AuditIntegrityError("continuation evidence lacks its admitted authenticated binding")
    pages: list[_RegisteredInspection] = []

    def page_index(page: _RegisteredInspection) -> int:
        if type(page) not in (_RegisteredBlobPage, _RegisteredSourceInspection):
            raise TypeError("evidence page has no owned continuation variant")
        for index, retained in enumerate(pages):
            if retained is page:
                return index
        pages.append(page)
        return len(pages) - 1

    registered = tuple(ce.RetainedPageLink(key=key, page_index=page_index(page)) for key, page in owner._pages.items())
    quoted = []
    for key, projection in owner._quoted_inspections.items():
        if projection.owner is not owner or projection.message_hash != key:
            raise AuditIntegrityError("quoted inspection lost its actual producer owner")
        quoted.append(ce.RetainedPageLink(key=key, page_index=page_index(projection.page)))
    historical = tuple(ce.RetainedPageLink(key=key, page_index=page_index(page)) for key, page in owner._historical_pages.items())
    deliveries = []
    scopes: tuple[DeliveryScope, ...] = ("primary", "planner")
    for scope in scopes:
        attempt = owner._pending_deliveries.get(scope)
        if attempt is not None and (type(attempt) is not BlobDeliveryAttempt or attempt.owner is not owner or attempt.scope != scope):
            raise AuditIntegrityError("pending delivery lost its actual evidence owner")
        deliveries.append(
            ce.RetainedDeliveryScope(
                scope=scope,
                generation=owner._delivery_generations[scope],
                current_call=owner._current_calls[scope],
                current_page_indexes=tuple(page_index(page) for page in owner._current_pages[scope]),
                pending=ce.RetainedDeliveryAttempt(
                    generation=attempt.generation,
                    registration_revision=attempt.registration_revision,
                    messages_hash=attempt.messages_hash,
                    tools_hash=attempt.tools_hash,
                    previous_call_ids=tuple(sorted(attempt.previous_call_ids)),
                    page_indexes=tuple(page_index(page) for page in attempt.pages),
                )
                if attempt is not None
                else None,
            )
        )
    reviews: list[AdvisorAbandonmentReview] = []
    decisions: list[AdvisorAbandonmentResponse] = []

    def decision_value(value: tuple[AdvisorAbandonmentReview, AdvisorAbandonmentResponse]) -> ce.RetainedAbandonmentDecision:
        review, decision = value
        if type(review) is not AdvisorAbandonmentReview or type(decision) is not AdvisorAbandonmentResponse:
            raise TypeError("abandonment evidence requires its actual owned decision")
        decision_index = next((index for index, prior in enumerate(decisions) if prior is decision), None)
        if decision_index is None:
            decision_index = len(decisions)
            decisions.append(decision)
        for index, existing in enumerate(reviews):
            if existing is review:
                return ce.RetainedAbandonmentDecision(review_index=index, decision_index=decision_index)
        reviews.append(review)
        return ce.RetainedAbandonmentDecision(review_index=len(reviews) - 1, decision_index=decision_index)

    current_review = decision_value(owner._abandonment_review) if owner._abandonment_review is not None else None
    limitations = tuple(
        ce.RetainedLimitation(binding=key, value=decision_value(value)) for key, value in owner._abandonment_limitations.items()
    )
    review_shelf = []
    for review in reviews:
        if any(part.trust is not None for record in review.records for part in record.parts):
            raise AuditIntegrityError("abandonment evidence requires current producer reconstruction for trusted partitions")
        review_shelf.append(
            ce.RetainedAbandonmentReview(
                binding=review.binding,
                request_scope=review.request_scope,
                request_hash=review.request_hash,
                graph_hash=review.graph_hash,
                policy_hash=review.policy_hash,
                registration_revision=review.registration_revision,
                records=tuple(
                    ce.RetainedAbandonmentRecord(
                        label=record.label,
                        content=record.content,
                        parts=tuple(
                            ce.RetainedUntrustedPart(text=part.text, inspection_text=part.inspection_text) for part in record.parts
                        ),
                    )
                    for record in review.records
                ),
                transcript_binding=review.transcript_binding,
            )
        )
    page_shelf: list[ce.RetainedBlobPage | ce.RetainedSourceInspection] = []
    for page in pages:
        if type(page) is _RegisteredBlobPage:
            page_shelf.append(
                ce.RetainedBlobPage(
                    tool_call_id=page.tool_call_id,
                    producer_call_id=page.producer_call_id,
                    envelope_hash=page.envelope_hash,
                    blob_id=page.blob_id,
                    content_hash=page.content_hash,
                    total_chars=page.total_chars,
                    start_char=page.start_char,
                    end_char=page.end_char,
                )
            )
        elif type(page) is _RegisteredSourceInspection:
            page_shelf.append(
                ce.RetainedSourceInspection(
                    tool_call_id=page.tool_call_id,
                    producer_call_id=page.producer_call_id,
                    envelope_hash=page.envelope_hash,
                    blob_id=page.blob_id,
                    total_size_bytes=page.total_size_bytes,
                    byte_range_inspected=page.byte_range_inspected,
                    source_kind=page.source_kind,
                )
            )
        else:
            raise TypeError("evidence page has no owned continuation variant")
    effects = owner.effects
    transcript_snapshot = owner.transcript.snapshot_for_continuation(messages, materialization=materialization)
    snapshot = ce.TurnEvidenceSnapshot(
        logical=logical,
        actor_user_id=actor,
        session_id=owner.session_id,
        physical_operation_id=owner.operation_id,
        physical_operation_epoch=epoch,
        request_id=owner.request_id,
        request_content_hash=owner.request_hash,
        effects=ce.RetainedTurnEffects(
            initial_state_json=canonical_json(effects.initial_state.to_dict()),
            initial_state_id=effects.initial_state_id,
            latest_verified_state_json=canonical_json(effects.latest_verified_state.to_dict()),
            latest_verified_state_id=effects.latest_verified_state_id,
            latest_committed_state_id=effects.latest_committed_state_id,
            verification=effects.verification,
            initial_authored_state_hash=effects.initial_authored_state_hash,
            latest_authored_state_hash=effects.latest_authored_state_hash,
            net_changed_fields=effects.net_changed_fields,
            batches=tuple(
                ce.RetainedBatchEffects(
                    assistant_message_id=batch.assistant_message_id,
                    state_id=batch.state_id,
                    version=batch.version,
                    tool_call_ids=batch.tool_call_ids,
                    inserted_state_ids=batch.inserted_state_ids,
                    authored_state_hash=batch.authored_state_hash,
                )
                for batch in effects.batches
            ),
            committed_revision_ids=effects.committed_revision_ids,
        ),
        transcript=transcript_snapshot,
        retained_history=owner._retained_history,
        registration_revision=owner._registration_revision,
        page_shelf=tuple(page_shelf),
        registered_pages=registered,
        quoted_inspections=tuple(quoted),
        historical_pages=historical,
        conflicting_page_ids=tuple(sorted(owner._conflicting_page_ids)),
        deliveries=tuple(deliveries),
        review_shelf=tuple(review_shelf),
        abandonment_review=current_review,
        abandonment_transcript_binding=owner._abandonment_transcript_binding,
        abandonment_limitations=limitations,
        effect_locators=() if application_entry is None else journal_effect_locators(application_entry, env_ref_names=env_ref_names),
        initial_is_latest=effects.initial_state is effects.latest_verified_state,
        recorder_json=encode_recorder_snapshot(recorder.snapshot_for_continuation(drained=drained)).canonical,
        historical_application_count=0 if application_entry is None else len(application_entry.applications),
        joint_graph_json=joint_graph_json,
        application_entry=application_entry,
        decision_shelf=tuple(decisions),
        requires_blob_reader=reader is not None,
    )
    ce.encode_turn_evidence(snapshot, env_ref_names=env_ref_names)
    return snapshot


def _restore_current_evidence(
    snapshot: ce.TurnEvidenceSnapshot,
    *,
    context: SessionOperationContext,
    reader: CurrentTranscriptBlobReader | None = None,
    env_ref_names: frozenset[str] = frozenset(),
    materialization: CurrentContinuationMessageProducers | None = None,
) -> RestoredTurnEvidence:
    """Internal graph construction, called only after the actual selected SQL read."""
    owner, initial, latest = _begin_current_evidence(snapshot, context=context)
    request = snapshot.transcript.request
    owner.transcript, messages = AdvisorTranscriptOwner.for_continuation(
        snapshot.transcript,
        session_id=snapshot.session_id,
        actor_user_id=snapshot.actor_user_id,
        request_id=snapshot.request_id,
        request=request,
        materialization=materialization,
    )
    return _finish_current_evidence(
        snapshot,
        context=context,
        owner=owner,
        initial=initial,
        latest=latest,
        messages=messages,
        reader=reader,
        env_ref_names=env_ref_names,
    )


def _begin_current_evidence(
    snapshot: ce.TurnEvidenceSnapshot, *, context: SessionOperationContext
) -> tuple[TurnEvidence, CompositionState, CompositionState]:
    request = snapshot.transcript.request
    if snapshot.session_id != context.fence.session_id or stable_hash(request) != snapshot.request_content_hash:
        raise AuditIntegrityError("cold evidence changed request content/current session")
    retained = snapshot.effects
    initial = ce.state_from_evidence_json(retained.initial_state_json)
    latest = initial if snapshot.initial_is_latest else ce.state_from_evidence_json(retained.latest_verified_state_json)
    owner = TurnEvidence(
        session_id=snapshot.session_id,
        session_operation_context=context,
        request_id=snapshot.request_id,
        request=request,
        initial_state_id=retained.initial_state_id,
        initial_state=initial,
        actor_user_id=snapshot.actor_user_id,
    )
    return owner, initial, latest


def _finish_current_evidence(
    snapshot: ce.TurnEvidenceSnapshot,
    *,
    context: SessionOperationContext,
    owner: TurnEvidence,
    initial: CompositionState,
    latest: CompositionState,
    messages: list[dict[str, Any]],
    reader: CurrentTranscriptBlobReader | None,
    env_ref_names: frozenset[str],
) -> RestoredTurnEvidence:
    retained = snapshot.effects
    if snapshot.requires_blob_reader:
        if type(reader) is not CurrentTranscriptBlobReader or reader.context is not context:
            raise AuditIntegrityError("cold evidence lost its required fresh current reader")
        reader.require_for_evidence(owner)
        owner.transcript.use_blob_reader(reader)
    elif reader is not None:
        raise AuditIntegrityError("cold evidence added an unretained reader capability")
    pages: list[_RegisteredInspection] = []
    for page in snapshot.page_shelf:
        if type(page) is ce.RetainedBlobPage:
            pages.append(
                _RegisteredBlobPage(
                    page.tool_call_id,
                    page.producer_call_id,
                    page.envelope_hash,
                    page.blob_id,
                    page.content_hash,
                    page.total_chars,
                    page.start_char,
                    page.end_char,
                )
            )
        elif type(page) is ce.RetainedSourceInspection:
            pages.append(
                _RegisteredSourceInspection(
                    page.tool_call_id,
                    page.producer_call_id,
                    page.envelope_hash,
                    page.blob_id,
                    page.total_size_bytes,
                    page.byte_range_inspected,
                    page.source_kind,
                )
            )
        else:
            raise AuditIntegrityError("cold evidence has an unsupported retained page variant")
    owner._pages = {link.key: pages[link.page_index] for link in snapshot.registered_pages}
    owner._quoted_inspections = {
        link.key: _QuotedInspectionProjection(owner, pages[link.page_index], link.key) for link in snapshot.quoted_inspections
    }
    owner._historical_pages = {link.key: pages[link.page_index] for link in snapshot.historical_pages}
    owner._conflicting_page_ids = set(snapshot.conflicting_page_ids)
    owner._registration_revision = snapshot.registration_revision
    owner._delivery_generations = {entry.scope: entry.generation for entry in snapshot.deliveries}
    owner._current_pages = {entry.scope: tuple(pages[index] for index in entry.current_page_indexes) for entry in snapshot.deliveries}
    owner._current_calls = {entry.scope: entry.current_call for entry in snapshot.deliveries}
    owner._pending_deliveries = {
        entry.scope: BlobDeliveryAttempt(
            owner,
            entry.scope,
            entry.pending.generation,
            entry.pending.registration_revision,
            entry.pending.messages_hash,
            entry.pending.tools_hash,
            frozenset(entry.pending.previous_call_ids),
            tuple(pages[index] for index in entry.pending.page_indexes),
        )
        for entry in snapshot.deliveries
        if entry.pending is not None
    }
    reviews = [
        AdvisorAbandonmentReview(
            binding=review.binding,
            request_scope=review.request_scope,
            request_hash=review.request_hash,
            graph_hash=review.graph_hash,
            policy_hash=review.policy_hash,
            registration_revision=review.registration_revision,
            records=tuple(
                AdvisorEvidenceRecord(
                    record.label,
                    record.content,
                    tuple(PromptGuardContentPart(part.text, inspection_text=part.inspection_text) for part in record.parts),
                )
                for record in review.records
            ),
            transcript_binding=review.transcript_binding,
        )
        for review in snapshot.review_shelf
    ]
    current = snapshot.abandonment_review
    owner._abandonment_review = (
        (reviews[current.review_index], snapshot.decision_shelf[current.decision_index]) if current is not None else None
    )
    owner._abandonment_transcript_binding = snapshot.abandonment_transcript_binding
    owner._abandonment_limitations = {
        entry.binding: (reviews[entry.value.review_index], snapshot.decision_shelf[entry.value.decision_index])
        for entry in snapshot.abandonment_limitations
    }
    owner._retained_history = snapshot.retained_history
    owner.effects = PersistedTurnEffects(
        initial_state=initial,
        initial_state_id=retained.initial_state_id,
        latest_verified_state=latest,
        latest_verified_state_id=retained.latest_verified_state_id,
        latest_committed_state_id=retained.latest_committed_state_id,
        verification=retained.verification,
        initial_authored_state_hash=retained.initial_authored_state_hash,
        latest_authored_state_hash=retained.latest_authored_state_hash,
        net_changed_fields=retained.net_changed_fields,
        batches=tuple(
            PersistedBatchEffects(
                batch.assistant_message_id,
                batch.state_id,
                batch.version,
                batch.tool_call_ids,
                batch.inserted_state_ids,
                batch.authored_state_hash,
            )
            for batch in retained.batches
        ),
        committed_revision_ids=retained.committed_revision_ids,
    )
    from elspeth.web.composer.continuation_evidence_application_data import require_evidence_application_links

    graph = require_evidence_application_links(snapshot, env_ref_names=env_ref_names)
    if graph is None:
        recorder, drained = BufferingRecorder.for_continuation(ce.recorder_from_evidence_json(snapshot.recorder_json))
        return RestoredTurnEvidence(owner, messages, recorder, drained)
    owner.effects = graph.effects
    recorder, drained = BufferingRecorder.for_continuation(graph.recorder)
    if snapshot.application_entry is not None:
        owner._portable_p4_graph = graph
        owner._portable_p4_entry = snapshot.application_entry
    return RestoredTurnEvidence(
        owner, messages, recorder, drained, graph.selected, snapshot.application_entry, graph.selected_audit, graph.selected_dialect
    )
