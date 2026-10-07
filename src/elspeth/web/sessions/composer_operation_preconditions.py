"""Atomic pre-provider guards for an immutable composer admission."""

from __future__ import annotations

from typing import get_args
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import Connection, select

from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.composer_operations import ComposerOperationError, ComposerOperationPreconditionRefused, ComposerOperationRecord
from elspeth.web.sessions.models import (
    chat_messages_table,
    composition_proposals_table,
    composition_states_table,
    proposal_events_table,
    sessions_table,
)
from elspeth.web.sessions.proposal_decoder import (
    _classify_authoritative_composition_proposal,
    _proposal_event_record_from_row,
    _proposal_record_from_row,
)
from elspeth.web.sessions.schemas import RecomposeRequest


def _refuse(job: ComposerOperationRecord, *, status: int, detail: str, error_type: str | None = None, flat: bool = False) -> None:
    body: dict[str, JsonValue] = {"detail": detail}
    if error_type is not None:
        body["error_type"] = error_type
        body["request_id"] = job.request_id
    raise ComposerOperationPreconditionRefused(
        error=ComposerOperationError(
            http_status=status,
            failure_code="http_error",
            error_type=error_type,
            body=body if flat or error_type is None else {"detail": body},
            diagnostic_id=None,
        )
    )


def check_composer_operation_preconditions_on_connection(
    conn: Connection, job: ComposerOperationRecord, *, auth_provider_type: str
) -> UUID | None:

    if type(job) is not ComposerOperationRecord:
        raise TypeError("job must be ComposerOperationRecord")
    if type(auth_provider_type) is not str or auth_provider_type not in get_args(AuthProviderType):
        raise ValueError("auth_provider_type must name a browser authentication provider")
    sid = str(job.session_id)
    session = conn.execute(
        select(sessions_table.c.user_id, sessions_table.c.auth_provider_type, sessions_table.c.archived_at)
        .where(sessions_table.c.id == sid)
        .limit(1)
    ).one_or_none()
    if (
        session is None
        or session.archived_at is not None
        or session.user_id != job.actor_user_id
        or session.auth_provider_type != auth_provider_type
    ):
        _refuse(job, status=404, detail="Session not found")
    if job.base_state_id is not None:
        base = conn.execute(
            select(composition_states_table.c.id)
            .where(composition_states_table.c.id == str(job.base_state_id), composition_states_table.c.session_id == sid)
            .limit(1)
        ).one_or_none()
        if base is None:
            _refuse(job, status=404, detail="State not found")
    current = conn.execute(
        select(composition_states_table.c.id)
        .where(composition_states_table.c.session_id == sid)
        .order_by(composition_states_table.c.version.desc())
        .limit(1)
    ).one_or_none()
    if (current.id if current is not None else None) != (str(job.base_state_id) if job.base_state_id is not None else None):
        _refuse(
            job,
            status=409,
            error_type="stale_compose_state",
            flat=True,
            detail="The session changed before this request started. Review the current pipeline and send again.",
        )
    if job.kind != "compose_recompose":
        return None
    if job.request_json is None:
        raise AuditIntegrityError("start preconditions require the admitted request")
    request = RecomposeRequest.model_validate_json(job.request_json)
    # Exact parity with _composer_conversation_messages: all tool/audit rows
    # are excluded, including physical LLM audit and orphan tool envelopes.
    history = conn.execute(
        select(chat_messages_table.c.id, chat_messages_table.c.role, chat_messages_table.c.tool_calls)
        .where(chat_messages_table.c.session_id == sid, chat_messages_table.c.role.not_in(("tool", "audit")))
        .order_by(chat_messages_table.c.sequence_no)
    ).all()
    latest_index = next((i for i in range(len(history) - 1, -1, -1) if history[i].role == "user"), None)
    if latest_index is None:
        _refuse(job, status=400, detail="No messages to recompose from")
        raise AssertionError("refusal must raise")
    user = history[latest_index]
    if user.id != str(request.expected_user_message_id):
        _refuse(
            job,
            status=409,
            error_type="recompose_user_message_mismatch",
            detail="The latest user message changed. Refresh the session before retrying composition.",
        )
    if any(row.role == "assistant" and not row.tool_calls for row in history[latest_index + 1 :]):
        _refuse(
            job,
            status=409,
            error_type="recompose_already_completed",
            detail="This user turn already has a saved response. Refresh the session to retrieve it.",
        )
    proposals = conn.execute(
        select(composition_proposals_table).where(
            composition_proposals_table.c.session_id == sid,
            composition_proposals_table.c.user_message_id == user.id,
            composition_proposals_table.c.status.in_(("pending", "committed")),
        )
    ).all()
    for row in proposals:
        events = conn.execute(
            select(proposal_events_table).where(
                proposal_events_table.c.session_id == sid,
                proposal_events_table.c.proposal_id == row.id,
                proposal_events_table.c.event_type == "proposal.created",
            )
        ).all()
        if len(events) != 1:
            raise AuditIntegrityError("composition proposal must have exactly one creation event")
        authority = _classify_authoritative_composition_proposal(
            row=_proposal_record_from_row(row), creation_event=_proposal_event_record_from_row(events[0])
        )
        if authority.pipeline is not None:
            _refuse(
                job,
                status=409,
                error_type="recompose_saved_proposal",
                detail="This user turn already has a saved pipeline proposal. Refresh the session to review it.",
            )
    return UUID(user.id)
