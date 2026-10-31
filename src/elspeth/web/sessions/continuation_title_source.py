"""Current inserted title source; no bootstrap publication or dispatch grant."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import InitializeFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.state import CompositionState
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_ingress import ContinuationIngressCheckpoint, _receipt, prepare_ingress_on_connection
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.graph_recovery import GraphRecoveryAdmission
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table as sessions
from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.sessions.routes._helpers import _initial_composition_state
from elspeth.web.sessions.schemas import SendMessageRequest
from elspeth.web.sessions.titles import is_default_session_title

if TYPE_CHECKING:
    from elspeth.web.sessions.service import SessionServiceImpl


@dataclass(frozen=True, slots=True)
class ContinuationTitleSource:
    selected: StoredContinuationSnapshot
    request: SendMessageRequest
    actor_user_id: str
    records: tuple[ChatMessageRecord, ...]
    original_session_title: str
    recovery: GraphRecoveryAdmission
    initial_state: CompositionState | None
    eligible: bool


def read_title_source_on_connection(
    conn: Connection,
    service: SessionServiceImpl,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
) -> ContinuationTitleSource:
    from elspeth.web.sessions.service import SessionServiceImpl

    if type(service) is not SessionServiceImpl or conn.engine is not service._engine:
        raise AuditIntegrityError("title source requires the actual sessions database owner")
    if type(running) is not ComposerOperationRunning or type(selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("title source requires actual current claim and selected owners")
    root = decode_continuation_snapshot(selected.payload_json)
    if len(root.frames) != 1 or type(root.frames[0]) is not InitializeFrame or root.frames[0].ingress_phase != "inserted":
        raise AuditIntegrityError("title source requires the closed inserted initializer")
    frame = root.frames[0]
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    try:
        request = SendMessageRequest.model_validate_json(parent.request_json, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("initializer parent request violates its admitted owned shape") from error
    # Actual ingress owner checks current lease/claim/segment/actor/request/base,
    # complete selected root, immutable receipt, original predecessor and all
    # original transcript fields/private custody before exposing this source.
    ingress = prepare_ingress_on_connection(
        conn,
        running,
        selected=selected,
        content=request.content,
        requested_state_id=request.state_id,
        composition_state_id=request.state_id,
    )
    if type(ingress) is not ContinuationIngressCheckpoint or frame.ingress_receipt_json is None:
        raise AuditIntegrityError("title source lost its actual immutable inserted ingress")
    receipt = _receipt(frame.ingress_receipt_json)
    current_title = conn.execute(select(sessions.c.title).where(sessions.c.id == sid)).scalar_one()
    if current_title != receipt.original_session_title:
        raise AuditIntegrityError("title source cannot infer an outcome from changed session title")
    # An empty selected frontier does not establish absence of already paid or
    # prepared work. Check every physical fence recorded for this logical job.
    prior_fences = select(attempts.c.session_operation_id).where(attempts.c.session_id == sid, attempts.c.operation_id == operation)
    if (
        conn.execute(select(effects.c.effect_key).where(effects.c.session_id == sid, effects.c.operation_id == operation)).first()
        is not None
        or conn.execute(
            select(physical.c.attempt_id).where(physical.c.session_id == sid, physical.c.operation_id.in_(prior_fences))
        ).first()
        is not None
    ):
        raise AuditIntegrityError("title source refuses unresolved prior provider/effect work")
    recovery = service._load_graph_recovery_context_on_connection(
        conn, running.claim.session_id, turn_start_state_id=request.state_id, session_operation_context=running.session_operation_context
    )
    state = (
        None
        if recovery.context.malformed_head
        else _initial_composition_state()
        if recovery.record is None
        else state_from_record(recovery.record)
    )
    eligible = (
        not recovery.context.malformed_head and len(ingress.transcript) == 1 and is_default_session_title(receipt.original_session_title)
    )
    return ContinuationTitleSource(
        selected, request, parent.actor_user_id, ingress.transcript, receipt.original_session_title, recovery, state, eligible
    )
