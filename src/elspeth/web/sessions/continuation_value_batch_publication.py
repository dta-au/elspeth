"""Pure publication plan for one original, complete V4 tool-call batch.

The existing metadata publisher deliberately admits one successful tool and
four rows.  This module plans the ordinary P4 row fanout for an original
multi-call cohort; SQL custody and insertion belong to the value-batch writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import JsonValue, TypeAdapter

from elspeth.contracts.composer_mutation import ComposerMutationRevocation
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
from elspeth.web.composer.continuation_codec import owned_json
from elspeth.web.composer.continuation_p4_outcome import (
    HistoricalCheckpointData,
    RetainedAuditOutcome,
    decode_audit_outcome_data,
    historical_checkpoint_columns_json,
)
from elspeth.web.composer.continuation_prepared_p4 import PreparedP4Members, prepared_p4_members
from elspeth.web.composer.continuation_tool_batch import _dump_owned
from elspeth.web.composer.continuation_value_batch_preparation import PreparedValueBatch, require_prepared_value_batch
from elspeth.web.composer.withheld_replies import withheld_reply_envelope
from elspeth.web.coordination.contracts import SessionOperationContext
from elspeth.web.sessions._persist_payload import RedactedToolRow, RejectionRecord, StatePayload
from elspeth.web.sessions.content_publication import prepare_content_publication
from elspeth.web.sessions.continuation_value_batch_transitions import PreparedValueBatchTransitions
from elspeth.web.sessions.protocol import ChatMessageRecord, ChatMessageRole, CompositionStateData, CompositionStateRecord

if TYPE_CHECKING:
    from elspeth.web.required_work import RequiredPreviewProduced

_REVOCATION = TypeAdapter(ComposerMutationRevocation)


def _message_object(value: object) -> dict[str, JsonValue]:
    """Retain only a complete finite JSON object from an owned P4 producer."""
    projected = owned_json(value)
    if type(projected) is not dict:
        raise AuditIntegrityError("value publication envelope is not a complete JSON object")
    return projected


@dataclass(frozen=True, slots=True)
class ValueBatchPublicationShape:
    message_count: int
    state_count: int
    rejection_count: int


@dataclass(frozen=True, slots=True)
class PlannedValueRejection:
    id: UUID
    record: RejectionRecord
    step_index: int
    composition_state_id: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ValueBatchPublicationSource:
    session_id: str
    operation_id: str
    operation_epoch: int
    selected_checkpoint_id: str
    original_response_codec: str
    original_response_digest: str
    tool_call_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedValueBatchPublication:
    batch: PreparedValueBatch
    transitions: PreparedValueBatchTransitions
    context: SessionOperationContext
    current_state: CompositionStateRecord | None
    locked_refusal: ComposerMutationRevocation | None
    effective_refusal: ComposerMutationRevocation | None
    created_at: datetime
    base_sequence: int
    message_ids: tuple[UUID, ...]
    state_ids: tuple[UUID, ...]
    rejection_ids: tuple[UUID, ...]
    env_ref_names: frozenset[str]
    source_identity: ValueBatchPublicationSource
    creator_operation_id: str
    messages: tuple[ChatMessageRecord, ...]
    states: tuple[CompositionStateRecord, ...]
    state_inputs: tuple[CompositionStateData, ...]
    rejections: tuple[PlannedValueRejection, ...]
    published_rows: tuple[RedactedToolRow, ...]
    published_assistant_content: str
    retained_outcome: RetainedAuditOutcome
    retained_outcome_json: str
    message_payload_bytes: tuple[tuple[int, int, int], ...]
    issued_preview: RequiredPreviewProduced | None = None

    def require_original(self) -> None:
        expected = plan_value_batch_publication(
            batch=self.batch,
            transitions=self.transitions,
            context=self.context,
            current_state=self.current_state,
            locked_refusal=self.locked_refusal,
            created_at=self.created_at,
            base_sequence=self.base_sequence,
            message_ids=self.message_ids,
            state_ids=self.state_ids,
            rejection_ids=self.rejection_ids,
            env_ref_names=self.env_ref_names,
            issued_preview=self.issued_preview,
        )
        if self != expected:
            raise AuditIntegrityError("value-batch publication differs from its original source")


def _source(batch: PreparedValueBatch, env_ref_names: frozenset[str]) -> tuple[ValueBatchPublicationSource, PreparedP4Members]:
    members = _publication_members(batch, env_ref_names)
    frames = batch.selected.stack.frames
    top = frames[-1]
    if sum(type(frame) is ComposeFrame for frame in frames) != 1 or type(top) is not ToolBatchFrame or top.phase != "tool_ready":
        raise AuditIntegrityError("value-batch publication lost its one original Compose frame and ready batch")
    return _source_identity(batch, members, top)


def _publication_members(batch: PreparedValueBatch, env_ref_names: frozenset[str]) -> PreparedP4Members:
    require_prepared_value_batch(batch)
    if type(env_ref_names) is not frozenset or env_ref_names != batch.inventory.names:
        raise AuditIntegrityError("value-batch publication changed its credential environment")
    return prepared_p4_members(batch.prepared, env_ref_names=env_ref_names)


def _issued_preview_source(
    batch: PreparedValueBatch, env_ref_names: frozenset[str], produced: RequiredPreviewProduced
) -> tuple[ValueBatchPublicationSource, PreparedP4Members]:
    from elspeth.web.composer.continuation_value_batch_preparation import require_prepared_issued_preview_batch
    from elspeth.web.sessions.continuation_preview_result import CompletePreviewReturn, preview_return_fields

    require_prepared_issued_preview_batch(batch, produced)
    if type(produced.returned) is not CompletePreviewReturn:
        raise AuditIntegrityError("issued preview publication has no complete original returned result")
    source, _result, _tolerant = preview_return_fields(produced.returned)
    members = _publication_members(batch, env_ref_names)
    root, calls = batch.selected.stack, batch.requested_calls
    top = root.frames[-1]
    if (
        len(root.frames) != 2
        or type(root.frames[0]) is not ComposeFrame
        or type(top) is not ToolBatchFrame
        or top.phase != "effect_reconciling"
        or top.accumulator.codec != "composer.tool-batch.v1"
        or top.next_tool_index != 0
        or top.settled_effect_ids
        or root.frames[0].pending_response != top.response
        or root.frames[0].slice_sequence != source.sequence
        or batch.inventory.running.claim.segment_sequence != source.sequence
        or batch.inventory.running.claim.ownership_generation != source.generation
        or batch.inventory.running.claim.attempt != source.claim_attempt
        or root.pending_owner_results
        or root.pending_settlement_ids
        or root.active_action_json is not None
        or root.active_action_source is not None
        or root.logical.session_id != source.session_id
        or root.logical.operation_id != source.operation_id
        or root.policy_hash != source.policy_sha256
        or root.cursor != source.cursor + 1
        or batch.selected.checkpoint_id == source.checkpoint_id
        or top.response != source.response
        or len(calls) != 1
        or calls[0].function.name != "preview_pipeline"
        or calls[0].id != source.tool_call_id
        or sha256(calls[0].function.arguments.encode("utf-8")).hexdigest() != source.raw_arguments_sha256
        or batch.prepared.creator_operation_id != source.creator_operation_id
        or batch.prepared.creator_operation_epoch != source.creator_operation_epoch
    ):
        raise AuditIntegrityError("issued preview publication lost its original dispatched source")
    frontier = tuple(item for item in root.effect_frontier if item.effect_id == f"tool:{source.response.digest}:0")
    if len(frontier) != 1 or frontier[0].disposition != "dispatched" or frontier[0].receipt_id is not None:
        raise AuditIntegrityError("issued preview publication lost its original dispatched effect")
    return _source_identity(batch, members, top)


def _source_identity(
    batch: PreparedValueBatch, members: PreparedP4Members, top: ToolBatchFrame
) -> tuple[ValueBatchPublicationSource, PreparedP4Members]:
    calls = members.redacted_calls
    ids = tuple(call["id"] for call in calls)
    if (
        not ids
        or len(ids) != len(batch.requested_calls)
        or len(ids) != len(batch.steps)
        or ids != tuple(call.id for call in batch.requested_calls)
        or tuple(call["function"]["name"] for call in calls) != tuple(call.function.name for call in batch.requested_calls)
        or tuple(step.index for step in batch.steps) != tuple(range(len(ids)))
    ):
        raise AuditIntegrityError("value-batch publication lost original call order or a completed step")
    selected = batch.selected
    return (
        ValueBatchPublicationSource(
            session_id=batch.prepared.session_id,
            operation_id=batch.prepared.creator_operation_id,
            operation_epoch=batch.prepared.creator_operation_epoch,
            selected_checkpoint_id=selected.checkpoint_id,
            original_response_codec=top.response.codec,
            original_response_digest=top.response.digest,
            tool_call_ids=ids,
        ),
        members,
    )


def _selected_refusal(members: PreparedP4Members, locked_refusal: ComposerMutationRevocation | None) -> ComposerMutationRevocation | None:
    if locked_refusal is not None and type(locked_refusal) is not ComposerMutationRevocation:
        raise AuditIntegrityError("value-batch publication received an unowned mutation refusal")
    prepared_refusal = members.authority_revocation
    if prepared_refusal is not None and locked_refusal is not None and prepared_refusal != locked_refusal:
        raise AuditIntegrityError("value-batch publication changed the mutation refusal")
    selected = prepared_refusal if locked_refusal is None else locked_refusal
    if selected is not None and selected.ceiling != members.mutation_ceiling:
        raise AuditIntegrityError("value-batch refusal differs from the prepared mutation ceiling")
    return selected


def value_batch_publication_shape(
    batch: PreparedValueBatch,
    *,
    locked_refusal: ComposerMutationRevocation | None,
    env_ref_names: frozenset[str],
    issued_preview: RequiredPreviewProduced | None = None,
) -> ValueBatchPublicationShape:
    _, members = _source(batch, env_ref_names) if issued_preview is None else _issued_preview_source(batch, env_ref_names, issued_preview)
    refusal = _selected_refusal(members, locked_refusal)
    return _shape(members, refusal)


def _shape(members: PreparedP4Members, refusal: ComposerMutationRevocation | None) -> ValueBatchPublicationShape:
    if any(row.composition_state_payload is not None for row in members.redacted_rows) and members.mutation_ceiling is None:
        raise AuditIntegrityError("value-batch graph publication lacks its original mutation ceiling")
    rows = members.revoked_rows if refusal is not None else members.redacted_rows
    count = len(rows)
    return ValueBatchPublicationShape(
        message_count=1 + 2 * count + len(members.private_tool_failures) + len(members.invocations) + len(members.withheld_replies),
        state_count=sum(row.composition_state_payload is not None for row in rows),
        rejection_count=len(members.rejection_records),
    )


def _message_bytes(message: ChatMessageRecord) -> tuple[int, int, int]:
    return (
        len(message.content.encode("utf-8")),
        0 if message.raw_content is None else len(message.raw_content.encode("utf-8")),
        0 if message.tool_calls is None else len(canonical_json(deep_thaw(message.tool_calls)).encode("utf-8")),
    )


def plan_value_batch_publication(
    *,
    batch: PreparedValueBatch,
    transitions: PreparedValueBatchTransitions,
    context: SessionOperationContext,
    current_state: CompositionStateRecord | None,
    locked_refusal: ComposerMutationRevocation | None,
    created_at: datetime,
    base_sequence: int,
    message_ids: tuple[UUID, ...],
    state_ids: tuple[UUID, ...],
    env_ref_names: frozenset[str],
    rejection_ids: tuple[UUID, ...] = (),
    issued_preview: RequiredPreviewProduced | None = None,
) -> PreparedValueBatchPublication:
    source, members = (
        _source(batch, env_ref_names) if issued_preview is None else _issued_preview_source(batch, env_ref_names, issued_preview)
    )
    if (
        type(transitions) is not PreparedValueBatchTransitions
        or transitions.prepared is not batch
        or transitions.original is not batch.selected
        or type(context) is not SessionOperationContext
        or context is not batch.inventory.running.session_operation_context
    ):
        raise AuditIntegrityError("value-batch publication lacks original transition custody")
    transitions.require_original()
    if (
        source.session_id != str(context.fence.session_id)
        or source.operation_id != str(context.fence.operation_id)
        or source.operation_epoch != context.fence.operation_epoch
        or batch.prepared.crash_pending
    ):
        raise AuditIntegrityError("value-batch publication differs from its operation fence")
    if type(created_at) is not datetime or created_at.tzinfo is None or created_at.utcoffset() is None:
        raise AuditIntegrityError("value-batch publication needs an aware creation time")
    created_at = created_at.astimezone(UTC)
    if type(base_sequence) is not int or base_sequence < 1:
        raise AuditIntegrityError("value-batch publication needs an allocated sequence range")
    if (
        type(message_ids) is not tuple
        or type(state_ids) is not tuple
        or type(rejection_ids) is not tuple
        or any(type(value) is not UUID for value in (*message_ids, *state_ids, *rejection_ids))
    ):
        raise AuditIntegrityError("value-batch publication needs exact allocated row identities")
    refusal = _selected_refusal(members, locked_refusal)
    rows = members.revoked_rows if refusal is not None else members.redacted_rows
    shape = _shape(members, refusal)
    if (
        len(message_ids) != shape.message_count
        or len(state_ids) != shape.state_count
        or len(rejection_ids) != shape.rejection_count
        or len({*message_ids, *state_ids, *rejection_ids}) != len(message_ids) + len(state_ids) + len(rejection_ids)
        or base_sequence + shape.message_count - 1 > 2**63 - 1
    ):
        raise AuditIntegrityError("value-batch publication row identities differ from its exact fanout")
    if current_state is not None and (
        type(current_state) is not CompositionStateRecord or str(current_state.session_id) != source.session_id
    ):
        raise AuditIntegrityError("value-batch publication inherited another session state")
    current_id = None if current_state is None else str(current_state.id)
    if current_state is not None and current_state.id in state_ids:
        raise AuditIntegrityError("value-batch publication reused its inherited state identity")
    if batch.prepared.expected_current_state_id != current_id:
        raise AuditIntegrityError("value-batch publication changed its original current-state fence")

    messages: list[ChatMessageRecord] = []
    states: list[CompositionStateRecord] = []
    state_inputs: list[CompositionStateData] = []
    rejections: list[PlannedValueRejection] = []
    session_id = UUID(source.session_id)
    assistant_id = message_ids[0]
    assistant_content = "" if refusal is not None else batch.prepared.assistant_content

    def append_message(
        role: ChatMessageRole,
        content: str,
        *,
        raw_content: str | None = None,
        tool_calls: tuple[dict[str, JsonValue], ...] | None = None,
        tool_call_id: str | None = None,
        state_id: str | None = None,
        parent_assistant_id: UUID | None = None,
    ) -> None:
        offset = len(messages)
        messages.append(
            ChatMessageRecord(
                id=message_ids[offset],
                session_id=session_id,
                role=role,
                content=content,
                created_at=created_at,
                writer_principal="compose_loop",
                sequence_no=base_sequence + offset,
                raw_content=raw_content,
                tool_calls=tool_calls,
                composition_state_id=None if state_id is None else UUID(state_id),
                tool_call_id=tool_call_id,
                parent_assistant_id=parent_assistant_id,
            )
        )

    append_message(
        "assistant",
        assistant_content,
        raw_content=batch.prepared.raw_content,
        tool_calls=tuple(_message_object(call) for call in members.redacted_calls),
        state_id=batch.prepared.parent_composition_state_id,
    )
    private_by_id = {item.tool_call_id: item for item in members.private_tool_evidence}
    rejected_by_id = {item.tool_call_id: item for item in members.rejection_records}
    next_version = 1 if current_state is None else current_state.version + 1
    for step_index, row in enumerate(rows):
        payload: StatePayload | None = row.composition_state_payload
        if payload is not None:
            if payload.derived_from_state_id is not None and payload.derived_from_state_id != current_id:
                raise AuditIntegrityError("value-batch graph state has a nonsequential parent")
            state_id = state_ids[len(states)]
            published = prepare_content_publication(payload.data, session_id=session_id, state_id=state_id, state_version=next_version)
            state = CompositionStateRecord(
                id=state_id,
                session_id=session_id,
                version=next_version,
                sources=published.sources,
                nodes=published.nodes,
                edges=published.edges,
                outputs=published.outputs,
                metadata_=published.metadata_,
                is_valid=published.is_valid,
                validation_errors=published.validation_errors,
                composer_meta=published.composer_meta,
                created_at=created_at,
                derived_from_state_id=None if current_id is None else UUID(current_id),
            )
            states.append(state)
            state_inputs.append(published)
            current_id = str(state_id)
            next_version += 1
        visible = None if row.visible_event is None else row.visible_event.with_parent(str(assistant_id))
        append_message(
            "tool",
            row.content,
            tool_calls=None if visible is None else (_message_object(visible.to_envelope()),),
            tool_call_id=row.tool_call_id,
            state_id=None if payload is None else current_id,
            parent_assistant_id=assistant_id,
        )
        private = private_by_id[row.tool_call_id]
        append_message(
            "audit",
            "Private credential-filtered tool evidence",
            tool_calls=(_message_object(private.to_envelope()),),
            state_id=current_id,
        )
        rejection = rejected_by_id.get(row.tool_call_id)
        if rejection is not None:
            rejections.append(PlannedValueRejection(rejection_ids[len(rejections)], rejection, step_index, current_id, created_at))
    for failure in members.private_tool_failures:
        append_message(
            "audit",
            "Private credential-filtered failure facts",
            tool_calls=(_message_object(failure.to_envelope()),),
            state_id=current_id,
        )
    revoked_ids = (
        {row.tool_call_id for row in members.redacted_rows if row.composition_state_payload is not None} if refusal is not None else set()
    )
    for invocation in members.invocations:
        content, envelope = redacted_tool_invocation_content_and_envelope(
            invocation, authority_revoked=invocation.tool_call_id in revoked_ids
        )
        append_message(
            "audit",
            content,
            tool_calls=(_message_object(envelope),),
            state_id=current_id,
        )
    for withheld in members.withheld_replies:
        append_message(
            "audit",
            withheld.content,
            tool_calls=(_message_object(withheld_reply_envelope(withheld.origin, withheld.content)),),
            state_id=current_id,
        )
    if len(messages) != shape.message_count or len(states) != shape.state_count or len(rejections) != shape.rejection_count:
        raise AuditIntegrityError("value-batch publication did not produce its exact row fanout")
    checkpoint = None
    if states:
        last = states[-1]
        checkpoint = HistoricalCheckpointData(
            id=last.id,
            session_id=last.session_id,
            version=last.version,
            original_columns_json=historical_checkpoint_columns_json(last),
            is_valid=last.is_valid,
            created_at=last.created_at,
            derived_from_state_id=last.derived_from_state_id,
            review_projection=None,
            creator_operation_id=source.operation_id,
            inserted_state_ids=tuple(state.id for state in states),
        )
    outcome = RetainedAuditOutcome(
        assistant_id=str(assistant_id),
        unwind_audit_failed=False,
        checkpoint_disposition="committed" if states else ("empty" if current_id is None else "no_new_checkpoint"),
        checkpoint=checkpoint,
        current_state_id=current_id,
        authority_revocation_json=None if refusal is None else _dump_owned(_REVOCATION, refusal),
        published_tool_rows_json=batch.prepared.revoked_rows_json if refusal is not None else batch.prepared.redacted_rows_json,
        published_assistant_content=assistant_content,
    )
    outcome_json = canonical_json(outcome.model_dump(mode="json"))
    decode_audit_outcome_data(outcome_json, prepared=batch.prepared, env_ref_names=env_ref_names)
    return PreparedValueBatchPublication(
        batch=batch,
        transitions=transitions,
        context=context,
        current_state=current_state,
        locked_refusal=locked_refusal,
        effective_refusal=refusal,
        created_at=created_at,
        base_sequence=base_sequence,
        message_ids=message_ids,
        state_ids=state_ids,
        rejection_ids=rejection_ids,
        env_ref_names=env_ref_names,
        source_identity=source,
        creator_operation_id=source.operation_id,
        messages=tuple(messages),
        states=tuple(states),
        state_inputs=tuple(state_inputs),
        rejections=tuple(rejections),
        published_rows=tuple(rows),
        published_assistant_content=assistant_content,
        retained_outcome=outcome,
        retained_outcome_json=outcome_json,
        message_payload_bytes=tuple(_message_bytes(message) for message in messages),
        issued_preview=issued_preview,
    )
