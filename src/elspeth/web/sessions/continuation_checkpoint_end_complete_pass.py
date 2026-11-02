"""Original successful END pass audit inside its paid consequence transaction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.advisor_audit import (
    AdvisorCheckpointPassAuditEnvelope,
    AdvisorCheckpointPassRecord,
    advisor_checkpoint_pass_audit_envelope,
)
from elspeth.web.composer.advisor_checkpoint import checkpoint_pass_record
from elspeth.web.composer.continuation_checkpoint_end_cold_consequence import (
    ColdCheckpointEndGate,
    require_original_cold_checkpoint_end_gate,
)
from elspeth.web.composer.continuation_checkpoint_end_computation import EndGateComputation
from elspeth.web.composer.continuation_checkpoint_end_physical import CheckpointEndPhysicalSource
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.visible_tool_event import validate_visible_audit_row
from elspeth.web.sessions.continuation_checkpoint_end_owed import VerifiedCheckpointEndOwed
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.time_normalization import restore_utc


@dataclass(frozen=True, slots=True)
class PreparedEndCompletePass:
    message_id: str
    record: AdvisorCheckpointPassRecord
    envelope: AdvisorCheckpointPassAuditEnvelope
    content: str


def prepare_end_complete_pass(
    *, physical: CheckpointEndPhysicalSource, computation: EndGateComputation, owed: VerifiedCheckpointEndOwed | None = None
) -> PreparedEndCompletePass:
    """Reproduce the original one- or two-attempt model pass before SQL writes."""
    if type(physical) is not CheckpointEndPhysicalSource or type(computation) is not EndGateComputation:
        raise AuditIntegrityError("END pass audit requires its original model verdict")
    ordinal = 1 if physical.phase_owner.phase_source.completed_children else 0
    if ordinal == 0:
        if not computation.verdict.ok:
            raise AuditIntegrityError("END first malformed pass requires its retained retry")
        schema_valid = computation.verdict.response_schema_valid
    elif owed is not None and owed.retry_history is not None and owed.retry_ordinal == 1:
        schema_valid = owed.retry_history.marker.first_attempt_schema_valid
    else:
        raise AuditIntegrityError("END second pass lost its original first paid attempt")
    verdict = computation.verdict
    record = checkpoint_pass_record(
        verdict,
        phase="end",
        source="model",
        state=physical.detached.live.state,
        pass_index=physical.gate_locals.pass_index,
        provider_attempts=ordinal + 1,
        first_attempt_schema_valid=schema_valid,
        first_attempt_accepted=ordinal == 0,
        format_reprompt_sent=ordinal == 1,
    )
    envelope = advisor_checkpoint_pass_audit_envelope(record)
    content = json.dumps(envelope, sort_keys=True)
    if len(content.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("END pass audit exceeds its unchanged evidence bound")
    return PreparedEndCompletePass(str(uuid4()), record, envelope, content)


def prepare_first_cold_end_complete_pass(*, gate: ColdCheckpointEndGate) -> PreparedEndCompletePass:
    """Prepare the exact bounded model-pass row from a proved cold H."""
    require_original_cold_checkpoint_end_gate(gate)
    source, computation = gate.source, gate.computation
    ordinal = source.owed.retry_ordinal
    if source.end_input.pass_index != source.owed.pass_index:
        raise AuditIntegrityError("cold END pass audit changed its original pass index")
    if ordinal == 0:
        if not computation.verdict.ok:
            raise AuditIntegrityError("cold END first malformed pass requires its retained retry")
        schema_valid = computation.verdict.response_schema_valid
    elif ordinal == 1 and source.owed.retry_history is not None:
        schema_valid = source.owed.retry_history.marker.first_attempt_schema_valid
    else:
        raise AuditIntegrityError("cold END model pass lost its bounded retry history")
    verdict = computation.verdict
    record = checkpoint_pass_record(
        verdict,
        phase="end",
        source="model",
        state=source.runtime.live.state,
        pass_index=source.end_input.pass_index,
        provider_attempts=ordinal + 1,
        first_attempt_schema_valid=schema_valid,
        first_attempt_accepted=ordinal == 0,
        format_reprompt_sent=ordinal == 1,
    )
    envelope = advisor_checkpoint_pass_audit_envelope(record)
    content = json.dumps(envelope, sort_keys=True)
    if len(content.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("cold END pass audit exceeds its unchanged evidence bound")
    return PreparedEndCompletePass(str(uuid4()), record, envelope, content)


def write_end_complete_pass_on_connection(
    conn: Connection,
    *,
    service: object,
    context: SessionOperationContext,
    prepared: PreparedEndCompletePass,
    created_at: datetime,
) -> int:
    """Insert the one legacy pass envelope under the original session lock."""
    from elspeth.web.sessions.service import SessionServiceImpl

    if (
        type(service) is not SessionServiceImpl
        or type(context) is not SessionOperationContext
        or context.operation_kind is not SessionOperationKind.COMPOSE
        or type(prepared) is not PreparedEndCompletePass
        or type(created_at) is not datetime
        or advisor_checkpoint_pass_audit_envelope(prepared.record) != prepared.envelope
        or json.dumps(prepared.envelope, sort_keys=True) != prepared.content
    ):
        raise AuditIntegrityError("END pass audit changed its original fenced evidence")
    sid = context.fence.session_id
    service._assert_session_write_lock_held(conn, sid, caller="end_complete_pass")
    validate_visible_audit_row(
        envelopes=(prepared.envelope,),
        content=prepared.content,
        role="audit",
        writer_principal="compose_loop",
        session_id=sid,
        context=context,
    )
    sequence = service._reserve_sequence_range(conn, sid, count=1)
    inserted = service._insert_chat_message(
        conn,
        session_id=sid,
        role="audit",
        content=prepared.content,
        raw_content=None,
        tool_calls=[prepared.envelope],
        sequence_no=sequence,
        writer_principal="compose_loop",
        composition_state_id=None,
        tool_call_id=None,
        parent_assistant_id=None,
        created_at=created_at,
        session_operation_context=context,
        message_id=prepared.message_id,
    )
    if inserted != prepared.message_id:
        raise AuditIntegrityError("END pass audit changed its preallocated identity")
    with service._session_mutations(conn, session_id=sid, session_operation_context=context) as mutations:
        mutations.mark_session_updated(updated_at=created_at)
    require_end_complete_pass_on_connection(conn, session_id=sid, prepared=prepared, sequence_no=sequence, created_at=created_at)
    return sequence


def require_end_complete_pass_on_connection(
    conn: Connection,
    *,
    session_id: str,
    prepared: PreparedEndCompletePass,
    sequence_no: int,
    created_at: datetime,
) -> None:
    """Cold-read the original model pass and its exact audit row shape."""
    if type(prepared) is not PreparedEndCompletePass or type(sequence_no) is not int or sequence_no < 1:
        raise AuditIntegrityError("END pass audit cold source is not exact")
    row = conn.execute(select(messages).where(messages.c.session_id == session_id, messages.c.id == prepared.message_id)).one_or_none()
    if (
        row is None
        or row.sequence_no != sequence_no
        or row.role != "audit"
        or row.content != prepared.content
        or row.raw_content is not None
        or row.tool_calls != [prepared.envelope]
        or row.writer_principal != "compose_loop"
        or row.composition_state_id is not None
        or row.tool_call_id is not None
        or row.parent_assistant_id is not None
        or restore_utc(row.created_at) != restore_utc(created_at)
    ):
        raise AuditIntegrityError("END pass audit lost its original successful model pass")
    duplicates = tuple(
        item.id
        for item in conn.execute(
            select(messages.c.id, messages.c.tool_calls).where(
                messages.c.session_id == session_id,
                messages.c.role == "audit",
                messages.c.content == prepared.content,
                messages.c.sequence_no >= sequence_no,
            )
        )
        if item.tool_calls == [prepared.envelope]
    )
    if duplicates != (prepared.message_id,):
        raise AuditIntegrityError("END pass audit duplicated its original model pass")
