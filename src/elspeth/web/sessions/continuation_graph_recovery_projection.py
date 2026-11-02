"""Original graph recovery selection and terminal SQL projection checks.

The selected recovery is DATA.  Its target must first be rederived from the
original admitted context before the terminal writer applies it.  This module
checks the resulting rows on that same connection; it does not grant a
provider, a graph, or publication authority by itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.engine import Connection

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.graph_recovery import GraphRecoveryContext
from elspeth.web.composer.no_tool_policy import compose_graph_recovery_refusal_message, visible_message_segments
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.sessions.content_publication import prepare_content_publication, read_content_publication
from elspeth.web.sessions.graph_recovery import GraphRecoverySettlement, graph_recovery_notice
from elspeth.web.sessions.models import chat_messages_table, composition_graph_parks_table, composition_states_table
from elspeth.web.sessions.protocol import ChatMessageRecord, CompositionStateRecord
from elspeth.web.sessions.routes._helpers import _message_response
from elspeth.web.sessions.schemas import ChatMessageResponse, ChatMessageSegmentResponse, MessageWithStateResponse

if TYPE_CHECKING:
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    from elspeth.web.sessions.service import SessionServiceImpl


@dataclass(frozen=True, slots=True)
class GraphRecoveryProjectionInput:
    """Original source values, never a transferable publication permission."""

    original_result: ComposerResult
    original_context: GraphRecoveryContext
    recovery: GraphRecoverySettlement

    def __post_init__(self) -> None:
        if (
            type(self.original_result) is not ComposerResult
            or type(self.original_context) is not GraphRecoveryContext
            or type(self.recovery) is not GraphRecoverySettlement
            or self.original_result.graph_recovery_intent is None
            or self.original_result.graph_recovery_intent != self.recovery.intent
            or self.original_context.malformed_head != self.recovery.malformed_head
        ):
            raise AuditIntegrityError("graph recovery projection lost its original owned source")


def require_graph_recovery_postconditions_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    service: SessionServiceImpl,
    *,
    original_result: ComposerResult,
    publication_result: ComposerResult,
    assistant: ChatMessageRecord,
    outcome: MessageWithStateResponse,
    projection: GraphRecoveryProjectionInput,
    recovery_prior_state: CompositionStateRecord | None = None,
) -> None:
    """Recheck exact recovery writes and public response after fenced settlement.

    The caller must separately prove the original paid recovery and Stop and
    replay the selected recovery from its original context before DML.
    """
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    from elspeth.web.sessions.service import SessionServiceImpl

    if (
        type(conn) is not Connection
        or type(running) is not ComposerOperationRunning
        or type(service) is not SessionServiceImpl
        or type(original_result) is not ComposerResult
        or type(publication_result) is not ComposerResult
        or type(assistant) is not ChatMessageRecord
        or type(outcome) is not MessageWithStateResponse
        or type(projection) is not GraphRecoveryProjectionInput
        or projection.original_result != original_result
        or publication_result != original_result
    ):
        raise AuditIntegrityError("graph recovery projection changed its original terminal source")
    recovery = projection.recovery
    sid = str(running.claim.session_id)
    if recovery.session_id != running.claim.session_id or recovery.operation_id != running.session_operation_context.fence.operation_id:
        raise AuditIntegrityError("graph recovery projection changed its original operation")
    if recovery.receipt_id is None:
        raise AuditIntegrityError("graph recovery projection lost its receipt identity")
    receipt_row = conn.execute(
        select(chat_messages_table).where(
            chat_messages_table.c.session_id == sid,
            chat_messages_table.c.id == str(recovery.receipt_id),
        )
    ).one_or_none()
    audit_row = conn.execute(
        select(chat_messages_table).where(
            chat_messages_table.c.session_id == sid,
            chat_messages_table.c.id == str(uuid.uuid5(recovery.receipt_id, "audit")),
        )
    ).one_or_none()
    if receipt_row is None or audit_row is None:
        raise AuditIntegrityError("graph recovery projection lost its actual receipt pair")
    receipt = service._row_to_chat_message_record(receipt_row)
    audit = service._row_to_chat_message_record(audit_row)
    expected_public_notice = ChatMessageResponse(
        id=str(receipt.id),
        session_id=str(receipt.session_id),
        operation_id=running.claim.operation_id,
        role=receipt.role,
        content=receipt.content,
        raw_content=None,
        segments=[
            ChatMessageSegmentResponse(kind=segment.kind, content=segment.content)
            for segment in visible_message_segments(content=receipt.content, raw_content=None)
        ],
        created_at=receipt.created_at,
        composition_state_id=str(receipt.composition_state_id) if receipt.composition_state_id is not None else None,
        sequence_no=receipt.sequence_no,
    )
    bound_state_id = recovery.state_id if recovery.state_id is not None else recovery.expected_state_id
    expected_audit = canonical_json(
        {
            "event": "graph_recovery",
            "action": recovery.intent.action,
            "target_state_id": str(recovery.target_state_id) if recovery.target_state_id is not None else None,
            "state_id": str(recovery.state_id) if recovery.state_id is not None else None,
            "empty_fallback": recovery.empty_fallback,
            "park_id": str(recovery.park_id) if recovery.park_id is not None else None,
            "displaced_candidate_captured": False,
            "failure_reason": recovery.failure_reason,
            "malformed_head": recovery.malformed_head,
        }
    )
    if (
        assistant.session_id != running.claim.session_id
        or assistant.role != "assistant"
        or assistant.writer_principal != "compose_loop"
        or receipt.role != "system"
        or receipt.writer_principal != "compose_loop"
        or receipt.content != graph_recovery_notice(recovery)
        or receipt.raw_content is not None
        or receipt.tool_calls is not None
        or receipt.composition_state_id != bound_state_id
        or audit.role != "audit"
        or audit.writer_principal != "compose_loop"
        or audit.content != expected_audit
        or audit.raw_content is not None
        or audit.tool_calls is not None
        or audit.composition_state_id != bound_state_id
        or audit.created_at != receipt.created_at
        or type(receipt.sequence_no) is not int
        or audit.sequence_no != receipt.sequence_no + 1
        or outcome.recovery_notice != expected_public_notice
        or outcome.message != _message_response(assistant).model_copy(update={"operation_id": running.claim.operation_id})
    ):
        raise AuditIntegrityError("graph recovery projection changed its actual receipt or public response")
    from elspeth.web.sessions.composer_turn import _graph_recovery_response

    proposals = service._list_composition_proposals_on_connection(conn, session_id=running.claim.session_id)
    if recovery.failure_reason is not None:
        public_state = None
    else:
        current_row = conn.execute(
            select(composition_states_table)
            .where(composition_states_table.c.session_id == sid)
            .order_by(composition_states_table.c.version.desc())
            .limit(1)
        ).one_or_none()
        public_state = service._row_to_state_record(current_row) if current_row is not None else None
        if public_state is not None:
            from elspeth.web.execution.completion_gates import project_committed_graph_review

            review = service._graph_review_on_connection(conn, session_id=public_state.session_id, stored=public_state)
            if review is not None:
                public_state = project_committed_graph_review(public_state, review)
    expected_response = _graph_recovery_response(assistant, proposals, public_state)
    expected_response = expected_response.model_copy(
        update={
            "recovery_notice": expected_public_notice,
            "message": expected_response.message.model_copy(update={"operation_id": running.claim.operation_id}),
        }
    )
    expected_response = MessageWithStateResponse.model_validate(expected_response.model_dump(mode="python"), strict=True)
    if outcome != expected_response:
        raise AuditIntegrityError("graph recovery projection changed its complete original public response")
    if recovery.failure_reason is not None:
        if (
            recovery.state_id is not None
            or recovery.data is not None
            or recovery.park_id is not None
            or outcome.state is not None
            or assistant.content != compose_graph_recovery_refusal_message()
            or assistant.raw_content != ""
        ):
            raise AuditIntegrityError("graph recovery refusal claimed a committed graph or model success")
        return
    if recovery.state_id is None or recovery.data is None or outcome.state is None:
        raise AuditIntegrityError("successful graph recovery lost its actual checkpoint")
    state_row = conn.execute(
        select(composition_states_table).where(
            composition_states_table.c.session_id == sid,
            composition_states_table.c.id == str(recovery.state_id),
        )
    ).one_or_none()
    if state_row is None:
        raise AuditIntegrityError("graph recovery projection lost its actual state row")
    state = service._row_to_state_record(state_row)
    retained_head = recovery.intent.action == "park" and recovery.state_id == recovery.expected_state_id
    if retained_head:
        if (
            type(recovery_prior_state) is not CompositionStateRecord
            or recovery_prior_state.id != recovery.expected_state_id
            or recovery_prior_state.session_id != running.claim.session_id
            or recovery_prior_state.version != recovery.expected_version
            or state != recovery_prior_state
        ):
            raise AuditIntegrityError("graph recovery park changed its complete original state")
        read_content_publication(recovery_prior_state)
        read_content_publication(state)
    elif recovery_prior_state is not None:
        raise AuditIntegrityError("new graph recovery checkpoint claimed a retained prior state")
    expected_state = prepare_content_publication(
        recovery.data,
        session_id=running.claim.session_id,
        state_id=recovery.state_id,
        state_version=state.version,
    )
    if (
        state.session_id != running.claim.session_id
        or state.id != recovery.state_id
        or state.sources != expected_state.sources
        or state.nodes != expected_state.nodes
        or state.edges != expected_state.edges
        or state.outputs != expected_state.outputs
        or state.metadata_ != expected_state.metadata_
        or (not retained_head and state.is_valid != expected_state.is_valid)
        or (not retained_head and state.validation_errors != expected_state.validation_errors)
        or (not retained_head and state.composer_meta != expected_state.composer_meta)
        or assistant.content != recovery.intent.message
        or assistant.raw_content is not None
        or assistant.composition_state_id != recovery.state_id
        or outcome.state.id != str(recovery.state_id)
    ):
        raise AuditIntegrityError("graph recovery projection substituted its actual state or assistant")
    if recovery.state_id != recovery.expected_state_id or recovery.intent.action != "park":
        if (
            state.version != recovery.expected_version + 1
            or state_row.provenance != "graph_recovery"
            or state_row.creator_operation_id != recovery.operation_id
            or state.derived_from_state_id != recovery.target_state_id
        ):
            raise AuditIntegrityError("graph recovery projection lost its original checkpoint lineage")
    elif state.version != recovery.expected_version:
        raise AuditIntegrityError("graph recovery park changed its original head")
    if recovery.park_id is not None:
        park = conn.execute(
            select(composition_graph_parks_table).where(
                composition_graph_parks_table.c.session_id == sid,
                composition_graph_parks_table.c.id == str(recovery.park_id),
            )
        ).one_or_none()
        if (
            park is None
            or park.state_id != str(recovery.state_id)
            or park.operation_id != recovery.operation_id
            or park.created_at != receipt_row.created_at
        ):
            raise AuditIntegrityError("graph recovery projection lost its actual park bookmark")
    elif recovery.intent.action == "park":
        raise AuditIntegrityError("graph recovery park lost its original bookmark")
